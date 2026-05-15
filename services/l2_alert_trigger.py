from __future__ import annotations

"""
L2 Telegram 告警触发服务（Phase 2 Task 2.2 新增）。

职责（对应 requirements.md Req 2 / 6.2 / 7）：
- 每轮 worker 触发一次（紧跟 HotnessService 之后，确保最新榜单已写入）
- 取最新 (window_end, window_type='1h') 的 hotness_snapshots
- 筛 growth/count_short/cross_source 三道门槛
- 走"智能冷却"4 路径决策树（首次/心跳/升级/跨源升级/重新触发）
- 推送给 TelegramClient.send_text；推送成功才更新进程内告警记录

关键状态字段（进程内，不持久化；重启后冷却失效，单实体最多多发 1 次告警）：
- `_last_processed_window_end`：上次扫描过的 window_end，避免同一整点重复处理
- `_alert_records: dict[entity, AlertRecord]`：每个实体上次告警时的快照
  （含 last_alerted_at / last_growth_rate / last_cross_source）

零 LLM：本模块绝不 `import llm.ollama_client`（Phase 1 硬约束延续到 Phase 2）。
不阻塞主流程：TelegramClient 自带优雅降级，本模块也不抛异常给 worker（worker
本身的 Jobs 异常隔离机制兜底）。
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from loguru import logger

from db.connection import Database
from db.repositories.hotness_snapshots_repo import HotnessSnapshotsRepo
from notifications.telegram_client import TelegramClient


# 默认消息模板（与 config/_alerts.py 保持同步；Service 构造时若未传 message_template
# 走这个默认值，避免单测必须显式传模板）
_DEFAULT_TEMPLATE = (
    "🔥 {alert_type}\n"
    "实体: {entity} ({entity_type})\n"
    "增长: {growth_rate}x（基于过去 7 天基线）\n"
    "提及: {count_short} 次 / 1h\n"
    "跨源: {cross_source}\n"
    "{is_new_entity_mark}"
    "rank: #{rank} @ {window_end}"
)


# Phase 2.7 Task 6 新增：briefing 渲染窗口（小时）
# alert 查询"该 entity 在 [now - N 小时, +∞)"区间内最新一条 briefing；
# 取不到 → 走原模板，告警照常发（spec Req 8.3：告警永远不等 briefing）。
# 1h 是经验值：briefing 每 15min 整点对齐生成；持续热点连续 4 个 window 都该
# 有简报，1h 内一定能命中"上一轮"的 briefing。调长 → 老 briefing 也凑合用，
# 但语境可能过期；调短 → 第一两次告警都没 briefing，等到第三轮才有。
_BRIEFING_LOOKBACK_HOURS = 1


@dataclass(frozen=True)
class AlertRecord:
    """
    每个 entity 上次告警时的快照，用于判断本次是否构成"质变"。

    设计要点：
    - last_alerted_at 用 aware UTC datetime；与 _decide_alert 里的 now() 对齐
    - 不存 entity 名字本身（外层 dict 的 key 已经是）
    - frozen 保证不可变，每次更新都是替换整个对象（语义清晰）
    """

    last_alerted_at: datetime
    last_growth_rate: float
    last_cross_source: int


# =============================================================================
# 模块级决策函数（Phase 2.4 §3.3.2 抽出，实时 / 整点链路共用）
# -----------------------------------------------------------------------------
# 把 4 路径决策树（首次 / 心跳 / growth 升级 / 跨源升级 / cooldown 内静默 /
# 重新触发）抽到模块顶层，让 RealtimeAlertService 不必依赖 AlertTriggerService
# 的实例就能复用同一份决策逻辑。
#
# AlertTriggerService._decide_alert 改成 thin wrapper 调本函数（保持原签名
# 不变 → 现有 16 个用例 0 回归）。
# =============================================================================


def decide_alert(
    last: Optional[AlertRecord],
    current: dict,
    now: datetime,
    *,
    cooldown_minutes: int,
    escalation_growth_multiplier: float,
    heartbeat_hours: int,
    growth_delta_pct: float = 0.0,
) -> tuple[bool, str]:
    """
    4 路径决策树（与 Phase 2.2 §3.2.1 等价）。返回 (是否告警, 触发类型标签)。

    `current` 至少含 `growth_rate` / `cross_source` 两个字段；本函数只读这两
    个字段，其它字段（entity / count_short 等）由调用方自行处理。

    决策树（按优先级，**顺序不可换**）：
        1. 首次告警（last is None）         → True,  "[首次]"
        2. 心跳（elapsed >= heartbeat_hours）→ True,  "[持续 Nh]"
        3. growth 翻倍升级（× ≥ multiplier） → True,  "[升级 → growth ×X.X]"
        4. 跨源升级（cross_source 增加）     → True,  "[跨源升级 +N]"
        5. growth 软门槛升级（涨幅 ≥ growth_delta_pct，Phase 2.8 新增）
                                            → True,  "[growth +X%]"
        6. cooldown 内 + 无质变             → False, ""
        7. cooldown 外 + 仍达阈值           → True,  "[重新触发]"

    ★ 心跳必须在 growth 升级之前判断：否则一个持续 6 小时但 growth 没大变
    的热点会落到"60min 内 + 无质变"分支被错过（handoff §4.1）。

    ★ Phase 2.8 新增路径 5（软门槛）解决低流量场景下 growth 难翻倍的问题：
    传 growth_delta_pct=0.3 即"涨 30% 也升级"。默认 0.0 = 关闭，保持 Phase 2.2
    行为 100% 等价（旧测试 0 回归）。
    """
    # 路径 1：首次告警
    if last is None:
        return True, "[首次]"

    elapsed = now - last.last_alerted_at

    # 路径 2：心跳提醒（必须在 growth/cross 升级之前判断）
    if elapsed >= timedelta(hours=heartbeat_hours):
        hours = int(elapsed.total_seconds() // 3600)
        return True, f"[持续 {hours}h]"

    # 路径 3：growth 翻倍升级
    # 防 last_growth_rate=0 除零（理论上不会发生，因为入冷却的都过了 threshold）
    if (
        last.last_growth_rate > 0
        and current["growth_rate"]
        >= last.last_growth_rate * escalation_growth_multiplier
    ):
        ratio = current["growth_rate"] / last.last_growth_rate
        return True, f"[升级 → growth ×{ratio:.1f}]"

    # 路径 4：跨源升级
    if current["cross_source"] > last.last_cross_source:
        delta = current["cross_source"] - last.last_cross_source
        return True, f"[跨源升级 +{delta}]"

    # 路径 5：growth 软门槛升级（Phase 2.8 新增）
    # 低流量场景下 growth 翻 1.5 倍很难，但涨 30% 仍是有意义的"持续走高"信号
    if (
        growth_delta_pct > 0.0
        and last.last_growth_rate > 0
        and current["growth_rate"]
        >= last.last_growth_rate * (1.0 + growth_delta_pct)
    ):
        pct = (
            current["growth_rate"] / last.last_growth_rate - 1.0
        ) * 100
        return True, f"[growth +{pct:.0f}%]"

    # 路径 6：常规冷却内 + 无质变 → 不告警
    if elapsed < timedelta(minutes=cooldown_minutes):
        return False, ""

    # 路径 7：cooldown 外 + 仍达阈值但无明显升级 → 重新触发
    return True, "[重新触发]"


@dataclass
class AlertTriggerService:
    """
    Telegram 告警触发器。

    ★ 与 HotnessService 一前一后跑：HotnessService 写完 hotness_snapshots，
    本服务紧接着读最新窗口决定是否推 Telegram。两者不共享状态，靠
    "最新 window_end" 这一个 DB 字段自然衔接。

    默认值与 config/_alerts.py 的 AlertSettings 对应字段保持一致；main.py
    构造本服务时显式传所有参数（不依赖默认值），单测则可以省略部分参数走默认。
    """

    db: Database
    hotness_repo: HotnessSnapshotsRepo
    telegram_client: TelegramClient

    # ----- 触发阈值（基础门槛，三条全满足才进入冷却判断）-----
    growth_threshold: float = 20.0
    min_count_short: int = 3
    min_cross_source: int = 1

    # ----- 智能冷却参数（4 路径决策树）-----
    cooldown_minutes: int = 60
    escalation_growth_multiplier: float = 1.5
    heartbeat_hours: int = 6

    # ----- Phase 2.8 决策树软门槛 -----
    # cooldown 内 growth 涨幅 ≥ 此值即升级（0.0 = 关闭，保持 Phase 2.2 行为）。
    # 默认 0.0 让本字段对老调用方完全透明；main.py 显式传配置值生效。
    growth_delta_pct: float = 0.0

    # ----- Phase 2.8 多窗口告警支持 -----
    # 本实例对应 hotness_snapshots 哪个 window_type；默认 '1h' 保持 Phase 2.2
    # 行为 100% 等价。main.py 在多窗口告警时分别构造 1h / 6h / 24h 三个实例。
    window_type: str = "1h"

    # ----- Phase 2.8 显示时区 -----
    # window_end 渲染时显示哪个时区。默认 UTC 保持向后兼容（早期版本行为等价）；
    # main.py 显式传 settings.timezone（默认 Asia/Shanghai）让用户看本地时间
    display_timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"))

    # ----- Phase 2.8 告警黑名单 -----
    # 即便上榜也不推 Telegram 的实体集合（与 hotness 表黑名单解耦）。
    # 默认空 tuple → 行为等价 Phase 2.2；main.py 显式传 settings.alert_exclude_entities
    # 屏蔽 BTC/ETH 等大币，避免"宏观信号被当成 [首次] 告警 push"。
    # 比较时统一 .upper() 大小写不敏感
    exclude_entities: tuple[str, ...] = ()

    # ----- 消息渲染 -----
    message_template: str = _DEFAULT_TEMPLATE

    # ----- Phase 2.7 Task 6 可选 briefing 集成 -----
    # 默认 None（向后兼容 Phase 2.2 行为；Phase 2.2 测试无需改）。
    # main.py Step 5d 会在 BriefingService 启用后注入 BriefingsRepo() 实例，
    # 让告警消息附加 LLM 简报字段（narrative / catalyst）。
    # 取不到 briefing 时优雅降级：走原模板，不阻塞告警发送（spec Req 8.3）。
    briefing_repo: Optional[object] = None  # BriefingsRepo | None，避免 import 循环

    # ----- 运行时状态（不持久化）-----
    _last_processed_window_end: Optional[datetime] = None
    _alert_records: dict[str, AlertRecord] = field(default_factory=dict)

    # =========================================================================
    # 公共 API
    # =========================================================================

    def run_once(self) -> bool:
        """
        执行一轮告警扫描。

        返回值：
        - True：本轮至少推送了 1 条告警
        - False：没新窗口 / 无合格 records / 推送全失败 / 当前窗口已处理过

        跳过 / False 场景：
        1. 最新 window_end 为 None（hotness_snapshots 表为空）
        2. 当前 window_end <= `_last_processed_window_end`（已处理过）
        3. 所有合格 records 都在冷却内且无质变
        4. 所有 send_text 都失败（已成功的部分仍计入 sent，但全失败时返回 False）
        """
        # ------ Step 1：取最新窗口 ------
        with self.db.get_session() as session:
            latest = self.hotness_repo.fetch_latest_window_end(
                session, self.window_type
            )

        if latest is None:
            # hotness_snapshots 还没数据（冷启动 / 基线不足导致 hotness 一直跳过）
            return False

        if (
            self._last_processed_window_end is not None
            and latest <= self._last_processed_window_end
        ):
            # 同一整点已扫过，避免每轮 worker 重复推送
            return False

        # ------ Step 2：读这个窗口的 Top-100 records ------
        # 取 100 是为了把候选集做大；实际能告警的实体很少（被 threshold 过滤）
        with self.db.get_session() as session:
            records = self.hotness_repo.fetch_top_k(
                session,
                window_end=latest,
                window_type=self.window_type,
                k=100,
            )

        # ------ Step 3：筛选 + 智能冷却 + 推送 ------
        sent = 0
        now = datetime.now(timezone.utc)
        for rec in records:
            if not self._is_eligible(rec):
                continue

            should_alert, alert_type = self._decide_alert(rec, now)
            if not should_alert:
                logger.info(
                    "alert skipped: entity={} 60min 内无质变",
                    rec.entity,
                )
                continue

            text = self._render_message(rec, alert_type)
            if self.telegram_client.send_text(text):
                # 推送成功才更新告警记录（失败时下一轮可重试，不进冷却）
                self._alert_records[rec.entity] = AlertRecord(
                    last_alerted_at=now,
                    last_growth_rate=rec.growth_rate,
                    last_cross_source=rec.cross_source,
                )
                sent += 1
                logger.info(
                    "alert sent: entity={} growth={:.1f} type={}",
                    rec.entity,
                    rec.growth_rate,
                    alert_type,
                )
            else:
                # send_text 已经在自身 log.error 过了，这里只记一行简短上下文
                logger.error(
                    "alert send failed (will retry next round): entity={}",
                    rec.entity,
                )

        # ------ Step 4：标记本窗口已处理 ------
        # 不论是否真的发出告警都要标记，否则下一轮会反复扫描同一窗口刷日志
        # （只是 send_text 失败的 entity 不进冷却，下一轮窗口刷新后仍会重试）
        self._last_processed_window_end = latest
        return sent > 0

    # =========================================================================
    # 内部：筛选 / 决策 / 渲染
    # =========================================================================

    def _is_eligible(self, rec) -> bool:
        """
        基础三道门槛（Req 2.3）：
        - growth_rate >= growth_threshold
        - count_short >= min_count_short
        - cross_source >= min_cross_source

        任一为 None 视为不合格（理论上 hotness_snapshots 应总是非空，防御性判断）。

        Phase 2.8 增强：检查 `exclude_entities` 黑名单（大小写不敏感）。
        命中黑名单的 entity 即便突破阈值也不会被告警，避免 BTC/ETH 这种
        "宏观信号"被推成 [首次] 通知打扰用户。
        """
        # 黑名单优先短路：避免对常驻大币每轮都做完整三段判断
        if self.exclude_entities:
            if rec.entity.upper() in {e.upper() for e in self.exclude_entities}:
                return False

        return (
            rec.growth_rate is not None
            and rec.growth_rate >= self.growth_threshold
            and rec.count_short is not None
            and rec.count_short >= self.min_count_short
            and rec.cross_source is not None
            and rec.cross_source >= self.min_cross_source
        )

    def _decide_alert(self, rec, now: datetime) -> tuple[bool, str]:
        """
        智能冷却决策（Req 2.4）。返回 (是否告警, 触发类型标签)。

        Phase 2.4 重构：决策树本体抽到模块级 `decide_alert` 函数，本方法
        改成 thin wrapper 让 RealtimeAlertService 也能复用同一份逻辑。
        保持方法签名不变 → 现有 16 个用例 0 回归。

        rec 是 ORM 对象（HotnessSnapshot），转成 dict 喂给 decide_alert
        的两个关键字段：growth_rate / cross_source。
        """
        return decide_alert(
            self._alert_records.get(rec.entity),
            {"growth_rate": rec.growth_rate, "cross_source": rec.cross_source},
            now,
            cooldown_minutes=self.cooldown_minutes,
            escalation_growth_multiplier=self.escalation_growth_multiplier,
            heartbeat_hours=self.heartbeat_hours,
            growth_delta_pct=self.growth_delta_pct,
        )

    def _render_message(self, rec, alert_type: str) -> str:
        """
        渲染消息正文（Req 3 + Phase 2.7 Task 6 briefing 集成）。

        is_new_entity_mark 字段策略：
        - 命中 → "★ 新实体（基线为 0）\n"（自带换行）
        - 未命中 → 空串
        模板里这个占位符后**不**带 \n，避免空串时多一行空白。

        模板缺字段时降级（Req 3.4）：log.warning + 用极简降级模板。

        ★ Phase 2.7 Task 6：briefing 集成
        - 启用条件：self.briefing_repo is not None（main.py 注入了）
        - 查询：fetch_latest_for_entity(entity, since=window_end - 1h)
          （查最近 1h 内任何一条 briefing，不限当前窗口；详见 _BRIEFING_LOOKBACK_HOURS）
        - 命中 → 在原模板基础上追加 "📰 {narrative} | {catalyst}" 一行
        - 未命中 / 异常 → 走原模板（优雅降级）
        - **永远不抛异常**：briefing 查询任何错误都被吞掉，告警照常发
        """
        is_new_mark = "★ 新实体（基线为 0）\n" if rec.is_new_entity else ""
        # 时区转换：rec.window_end 来自 PG TIMESTAMPTZ，可能 aware（带 UTC tz）或 naive
        # 统一兜底：naive 当 UTC 处理；aware 按 self.display_timezone astimezone
        we = rec.window_end
        if we.tzinfo is None:
            we = we.replace(tzinfo=timezone.utc)
        we_local = we.astimezone(self.display_timezone)
        try:
            base = self.message_template.format(
                alert_type=alert_type,
                entity=rec.entity,
                entity_type=rec.entity_type or "<n/a>",
                growth_rate=f"{rec.growth_rate:.1f}",
                count_short=rec.count_short,
                cross_source=rec.cross_source,
                is_new_entity_mark=is_new_mark,
                window_end=we_local.strftime("%Y-%m-%d %H:%M"),
                rank=rec.rank,
            )
        except KeyError as e:
            logger.warning("alert template missing key: {}, 用默认模板", e)
            base = (
                f"🔥 {alert_type} {rec.entity} "
                f"growth={rec.growth_rate:.1f} rank={rec.rank}"
            )

        # ★ briefing 追加（可选；任何异常都不影响告警）
        suffix = self._render_briefing_suffix(rec)
        if suffix:
            return f"{base}\n{suffix}"
        return base

    def _render_briefing_suffix(self, rec) -> str:
        """
        查最新 briefing 并渲染成 "📰 narrative | catalyst" 格式。

        没有 briefing_repo / 查不到 / DB 异常 → 返回空串（调用方走原模板）。
        narrative 和 catalyst 至少有一个非空才追加（避免推送一行空 "📰  | "）。
        """
        if self.briefing_repo is None:
            return ""

        try:
            since = rec.window_end - timedelta(hours=_BRIEFING_LOOKBACK_HOURS)
            with self.db.get_session() as session:
                briefing = self.briefing_repo.fetch_latest_for_entity(
                    session, entity=rec.entity, since=since
                )
        except Exception as e:
            # 查询失败不能影响告警发送；只 log warning
            logger.warning(
                "alert briefing fetch failed (优雅降级): entity={} err={}",
                rec.entity,
                e,
            )
            return ""

        if briefing is None:
            return ""

        # 渲染：narrative 优先，其次 catalyst；都为空就不追加
        narrative = (briefing.narrative or "").strip()
        catalyst = (briefing.catalyst or "").strip()
        if not narrative and not catalyst:
            return ""
        if narrative and catalyst:
            return f"📰 {narrative} | {catalyst}"
        # 只有一个有
        return f"📰 {narrative or catalyst}"
