from __future__ import annotations

"""
L2 实时告警触发服务（Phase 2 Task 2.4 新增）。

定位（与整点告警链路的关键差异）：
- ★ 紧跟 EntityExtractor 之后通过 `notify(n_added)` hook **同步触发**：
  EntityExtractor 写完一批新提及就立刻通知本服务做一次轻量榜计算，命中阈值
  的 entity 直接推 Telegram，不等下一个 :00/:15/:30/:45 整点。
- ★ **不进 worker 主循环**：本服务不实现 `run_once()`，不被 scheduler/jobs
  调度；唯一入口是 `notify()`，由 EntityExtractor 在 worker 线程内同步调用。
- ★ **不写 hotness_snapshots 表**：实时榜只在内存算 Top-K 直接推送，避免
  分钟级时间戳（如 10:23:47）污染整点对齐的主表（Phase 2.1 三窗口都对齐
  到 :00/:15/:30/:45 的核心约束依然成立）。
- ★ **共享 AlertRecord 冷却 dict**：本服务的 `shared_alert_records` 与
  AlertTriggerService 的 `_alert_records` 是 **同一个 dict 对象引用**
  （main.py Step 5e 显式传引用注入）；同一 entity 在 60 分钟内不会被实时
  和整点链路重复推送。

延迟拆解：worker poll 间隔（5s）+ EntityExtractor 写库（< 1s）+
`_trigger_immediate` 内存计算（< 2s 预算）+ Telegram 网络往返（< 2s）≈
最坏 1~2 分钟，对比 Phase 2.2 整点最坏 14~15 分钟。

零 LLM：本模块绝不 `import llm.ollama_client`（Phase 1 / 2.x 硬约束延续）。
不阻塞主流程：`notify()` / `_trigger_immediate()` 全程 try/except 兜底，
异常只 log.error 不向上抛（由 Task 1.2 / 1.3 在方法体里落实）。

本文件状态：
- Task 1.1（当前）：仅 dataclass 字段骨架 + 占位方法（pass）。
- Task 1.2：实现 `notify(n_added)` 方法体（_pending_count 累积 + 阈值触发）。
- Task 1.3：实现 `_trigger_immediate()` 方法体（候选筛选 + 决策 + 推送）。
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from loguru import logger

from db.connection import Database
from db.repositories.entity_mentions_repo import EntityMentionsRepo
from notifications.telegram_client import TelegramClient
from services.l2_alert_trigger import AlertRecord, decide_alert  # 复用 dataclass + 决策函数
from services.l2_sliding_counter import SlidingCounter


# 默认消息模板（与 config/_alerts.py 的 _DEFAULT_TEMPLATE 保持同步）。
# 与整点告警共用同一份模板字符串，alert_type 渲染时由 _trigger_immediate
# 自带 "[实时]" 前缀做区分（如 "[实时][首次]" / "[实时][升级 → growth ×2.0]"）。
_DEFAULT_TEMPLATE = (
    "🔥 {alert_type}\n"
    "实体: {entity} ({entity_type})\n"
    "增长: {growth_rate}x（基于过去 7 天基线）\n"
    "提及: {count_short} 次 / 1h\n"
    "跨源: {cross_source}\n"
    "{is_new_entity_mark}"
    "rank: #{rank} @ {window_end}"
)


@dataclass
class RealtimeAlertService:
    """
    实时告警触发器。

    与整点告警的协同（design §3.1.1 / §3.3）：
    - 共享 `sliding_counter`（与 EntityExtractor / HotnessService 同一实例）
    - 共享 `telegram_client`（与 AlertTriggerService 同一实例）
    - 共享 `shared_alert_records`（与 AlertTriggerService._alert_records
      同一 dict 对象引用，跨服务冷却防刷屏）

    单 worker 串行调度下，`notify()` 在 EntityExtractor 内同步触发
    `_trigger_immediate()`，此时 AlertTriggerService 还没轮到，所以读写
    `shared_alert_records` 无并发。CPython GIL 保证 dict 单 key set/get 原子；
    未来拆多 worker 时再加 `threading.Lock`（详见 design §3.3.3 / §8.1）。
    """

    # =========================================================================
    # 依赖（共享单实例，main.py Step 5e 注入）
    # =========================================================================

    db: Database
    mentions_repo: EntityMentionsRepo
    # 与 EntityExtractor / HotnessService 共享同一实例，否则短窗口计数对不上
    sliding_counter: SlidingCounter
    # 与 AlertTriggerService 共享同一实例，避免双链路各开一个 HTTP session
    telegram_client: TelegramClient

    # =========================================================================
    # ★ 共享冷却 dict（最关键的字段）
    # -------------------------------------------------------------------------
    # main.py 必须传 `alert_service._alert_records` 同一引用，不允许 deepcopy /
    # 新建空 dict。两个服务读写同一个 dict 对象，才能让"实时已推 → 整点扫到
    # 同 entity 时被冷却拦下"的语义成立。
    #
    # 默认 default_factory=dict 仅为：
    #   1) 单测可省略此参数；
    #   2) main.py 在 AlertTriggerService 未启用时不传 → RealtimeAlertService
    #      也不会被构造（启动条件看 main.py Step 5e），保留默认值只是兜底。
    # =========================================================================

    shared_alert_records: dict[str, AlertRecord] = field(default_factory=dict)

    # =========================================================================
    # 触发参数（design §3.1.1）
    # =========================================================================

    # 累积多少新提及触发一次实时计算。
    # 50 是经验值：低流量期 5~10 轮（25~50 秒）攒满；高流量期单轮就触发。
    burst_threshold: int = 50

    # 两次实时触发的最小间隔（秒），防止极端情况下连续触发把 Telegram 灌死。
    # 30s 足够每次 _trigger_immediate 跑完（预算 < 2s）+ 留余量。
    min_seconds_between_triggers: int = 30

    # =========================================================================
    # 榜计算参数（与整点榜 1h 窗口公式完全对齐，详见 services/l2_hotness.py）
    # =========================================================================

    # Top-K 截取（实时榜只在内存排名，不写库；K=20 与整点对齐）
    top_k: int = 20

    # growth_rate ≥ 此值才进入告警决策。比整点 alert_growth_threshold(20) 严
    # 50%——分钟级窗口的 growth 抖动比整点榜大，30 倍是"明显异常"信号。
    growth_threshold: float = 30.0

    # count_short ≥ 此值。比整点 alert_min_count_short(3) 严，过滤"3 条偶然
    # 提及就触发"（分钟级窗口里 3 条可能是 KOL 转发同一话题）。
    min_count_short: int = 5

    # 平滑因子，避免 baseline_per_hour ≈ 0 时 growth_rate 爆炸。与整点一致。
    smoothing: float = 2.0

    # 基线统计天数（与整点 HotnessService.baseline_days 一致）
    baseline_days: int = 7

    # 基线统计的小时窗口宽度。等于 baseline_days*24 - short_hours(=1)。
    # 拆出独立字段避免 _trigger_immediate 内重复推算。整点榜是
    # `7*24 - 1 = 167` 小时（用过去 7 天 minus 当前 1h 短窗）。
    baseline_hours_window: int = 7 * 24 - 1

    # =========================================================================
    # 输出黑名单（与整点榜对齐）
    # -------------------------------------------------------------------------
    # 这些 entity 不会进入告警筛选；用于屏蔽 BTC/ETH/USDT 这种"提到很多但
    # growth ≈ 1"的常驻巨头，让实时告警聚焦"突然热"的新东西。
    # 默认空 tuple = 不屏蔽（保持向后兼容）。
    # =========================================================================

    exclude_entities: tuple[str, ...] = ()

    # =========================================================================
    # 智能冷却参数（4 路径决策树，与 AlertTriggerService 完全一致）
    # -------------------------------------------------------------------------
    # 决策由 services/l2_alert_trigger.decide_alert（Task 1.3 抽出）执行；
    # 本服务只负责把以下三参数原样传过去。
    # =========================================================================

    cooldown_minutes: int = 60
    escalation_growth_multiplier: float = 1.5
    heartbeat_hours: int = 6

    # =========================================================================
    # 消息渲染
    # =========================================================================

    # 模板字段与 AlertTriggerService 完全一致，alert_type 由调用处自带
    # "[实时]" 前缀（如 "[实时][首次]"）做实时 / 整点的区分。
    message_template: str = _DEFAULT_TEMPLATE

    # =========================================================================
    # 时区（用于 _trigger_immediate 取 now 与渲染 window_end）
    # =========================================================================

    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"))

    # =========================================================================
    # 运行时状态（不持久化；进程重启清零）
    # -------------------------------------------------------------------------
    # `_pending_count`：累积未触发的新提及数，达 burst_threshold 时
    #   `_trigger_immediate()` 跑完成功后清零（详见 Task 1.2 / 1.3）。
    # `_last_triggered_at`：上次实际触发 `_trigger_immediate` 的 UTC 时刻，
    #   配合 `min_seconds_between_triggers` 限频；None = 从未触发过。
    # =========================================================================

    _pending_count: int = 0
    _last_triggered_at: Optional[datetime] = None

    # =========================================================================
    # 公共 / 内部方法（Task 1.1 仅放占位，方法体由 Task 1.2 / 1.3 实现）
    # =========================================================================

    def notify(self, n_added: int) -> None:
        """
        EntityExtractor 写库成功后同步调用。

        累积新增提及数到 `_pending_count`，做双重限频判断后决定是否触发
        `_trigger_immediate()`：

        1. 计数门：`_pending_count >= burst_threshold` 才考虑触发；
        2. 时间门：距上次实际触发 ≥ `min_seconds_between_triggers` 秒；
           `_last_triggered_at is None`（首次触发）绕过时间门。

        全程 try/except 兜底——任何异常只打 log.error 不向上抛，确保
        EntityExtractor.run_once 不会因实时告警 hook 失败而中断主流程。

        触发后**不**清零 `_pending_count`：清零放在 `_trigger_immediate()`
        成功完成后做（详见 Task 1.3，对应 design §3.1.2 末尾"全成功才清零"
        策略，让推送失败时下一轮 burst 还能重试）。
        """
        try:
            # 防御 0 / 负数：上游 EntityExtractor 不会传，但兜底一下避免
            # 把无意义调用计入累积或触发"什么也不做"的 _trigger_immediate。
            if n_added <= 0:
                return

            self._pending_count += n_added

            # ── 计数门 ──
            if self._pending_count < self.burst_threshold:
                logger.debug(
                    "realtime accumulating: pending={} burst_threshold={}",
                    self._pending_count,
                    self.burst_threshold,
                )
                return

            # ── 时间门 ──
            now = datetime.now(timezone.utc)
            if self._last_triggered_at is not None:
                elapsed = (now - self._last_triggered_at).total_seconds()
                if elapsed < self.min_seconds_between_triggers:
                    logger.debug(
                        "realtime throttled: pending={} elapsed={:.1f}s < min={}s",
                        self._pending_count,
                        elapsed,
                        self.min_seconds_between_triggers,
                    )
                    return
            else:
                # 首次触发用 -1 标记，便于 INFO 日志一眼区分
                # "首次触发" vs "限频已过的常规触发"。
                elapsed = -1.0

            logger.info(
                "realtime trigger fired: pending={} elapsed={:.1f}s",
                self._pending_count,
                elapsed,
            )
            self._trigger_immediate()
        except Exception as e:
            # 异常隔离：不能让实时告警 hook 把 EntityExtractor.run_once 链路
            # 拖崩。所有异常一律吞掉，只留 ERROR 级日志便于事后排查。
            logger.error("realtime_trigger.notify 异常（已隔离）：{}", e)

    def _trigger_immediate(self) -> None:
        """
        实时榜计算 + 决策 + 推送（design §3.1.2）。

        阶段 1：候选筛选
        - `sliding_counter.active_entities("1h")` 拿短窗候选
        - 排除 exclude_entities 黑名单（大小写不敏感）
        - count_short < min_count_short → 跳过
        - 查 entity_mentions 拿 baseline_total / cross_source（任一异常只
          warning 跳过，不让单 entity DB 故障拖崩整轮）
        - 公式 `growth_rate = short_count / max(baseline_per_hour, smoothing)`
          与 HotnessService(1h) 完全一致
        - growth_rate < growth_threshold → 跳过

        阶段 2：决策 + 推送 + 更新共享冷却
        - decide_alert（共用 4 路径决策树）→ should_alert / alert_type
        - alert_type 自带 "[实时]" 前缀做实时 / 整点的区分
        - send_text 成功 → 写 shared_alert_records；失败 → 标记 any_failed

        清零策略：
        - 全成功 / 无合格 entity → `_pending_count = 0`
        - 任一 send_text 失败 → 保留 `_pending_count`，下一轮 burst 再试

        异常隔离：整个方法被 try/except 包裹，任何异常只 log.error 不向上抛；
        异常时也更新 `_last_triggered_at` 避免 notify 限频失效后反复触发。
        """
        try:
            now = datetime.now(timezone.utc)
            candidates = self.sliding_counter.active_entities("1h")
            if not candidates:
                # 候选集为空时也清零 + 标记触发时间，避免 notify 反复尝试
                self._pending_count = 0
                self._last_triggered_at = now
                return

            short_hours = 1  # 实时榜固定 1h 短窗
            short_start = now - timedelta(hours=short_hours)
            baseline_start = now - timedelta(days=self.baseline_days)
            baseline_hours = self.baseline_days * 24 - short_hours
            exclude_upper = {e.upper() for e in self.exclude_entities}

            # ============== 阶段 1：筛候选 ==============
            eligible: list[dict] = []
            for entity in candidates:
                # 黑名单过滤（大小写不敏感，与整点榜对齐）
                if entity.upper() in exclude_upper:
                    continue

                short_count = self.sliding_counter.count(entity, "1h")
                if short_count < self.min_count_short:
                    continue

                # 单 entity DB 查询失败只 warning 跳过，不阻断整轮
                try:
                    with self.db.get_session() as session:
                        baseline_total = self.mentions_repo.count_for_entity(
                            session,
                            entity,
                            start=baseline_start,
                            end=short_start,
                        )
                        cross_source = self.mentions_repo.count_sources_for_entity(
                            session,
                            entity,
                            start=short_start,
                            end=now,
                        )
                except Exception as e:
                    logger.warning(
                        "realtime entity={} count failed: {}", entity, e
                    )
                    continue

                # 与 HotnessService(1h) 同一公式
                growth_rate = short_count / max(
                    baseline_total / baseline_hours, self.smoothing
                )
                if growth_rate < self.growth_threshold:
                    continue

                eligible.append(
                    {
                        "entity": entity,
                        # 实时榜不查 entity_type（要查 normalized_messages 拖耗时）
                        "entity_type": None,
                        "count_short": short_count,
                        "growth_rate": growth_rate,
                        "cross_source": cross_source,
                        "is_new_entity": (
                            baseline_total == 0 and short_count >= 5
                        ),
                        "window_end": now,
                        "rank": 0,
                    }
                )

            # ============== 阶段 2：决策 + 推送 + 更新共享冷却 ==============
            sent, any_failed = 0, False
            for rec in eligible:
                should_alert, alert_type = decide_alert(
                    self.shared_alert_records.get(rec["entity"]),
                    rec,
                    now,
                    cooldown_minutes=self.cooldown_minutes,
                    escalation_growth_multiplier=self.escalation_growth_multiplier,
                    heartbeat_hours=self.heartbeat_hours,
                )
                if not should_alert:
                    continue

                # ★ 关键区分：alert_type 前缀 "[实时]"，与整点告警 "[首次]" 等做区分
                prefixed = f"[实时]{alert_type}"
                text = self._render_message(rec, prefixed)
                if self.telegram_client.send_text(text):
                    self.shared_alert_records[rec["entity"]] = AlertRecord(
                        last_alerted_at=now,
                        last_growth_rate=rec["growth_rate"],
                        last_cross_source=rec["cross_source"],
                    )
                    sent += 1
                    logger.info(
                        "alert sent: entity={} growth={:.1f} type={}",
                        rec["entity"],
                        rec["growth_rate"],
                        prefixed,
                    )
                else:
                    any_failed = True
                    logger.error(
                        "realtime alert send failed: entity={}", rec["entity"]
                    )

            logger.info(
                "realtime trigger done: candidates={} eligible={} alerts={}",
                len(candidates),
                len(eligible),
                sent,
            )

            # 清零策略：全成功 / 无合格 entity → 清零；任一失败 → 保留 pending
            if not any_failed:
                self._pending_count = 0
            self._last_triggered_at = now
        except Exception as e:
            logger.error("realtime _trigger_immediate failed: {}", e)
            # 异常时也更新 _last_triggered_at 防止 notify 反复触发
            self._last_triggered_at = datetime.now(timezone.utc)

    def _render_message(self, rec: dict, alert_type: str) -> str:
        """
        渲染消息正文（design §3.1.1 message_template）。

        与 AlertTriggerService._render_message 等价但独立实现：
        - rec 是 dict（而非 ORM 对象），字段访问走 dict[]
        - 模板缺字段时降级（KeyError）log.warning + 默认极简模板

        is_new_entity_mark 字段策略：
        - 命中 → "★ 新实体（基线为 0）\n"（自带换行）
        - 未命中 → 空串
        """
        is_new_mark = (
            "★ 新实体（基线为 0）\n" if rec.get("is_new_entity") else ""
        )
        try:
            return self.message_template.format(
                alert_type=alert_type,
                entity=rec["entity"],
                entity_type=rec.get("entity_type") or "<n/a>",
                growth_rate=f"{rec['growth_rate']:.1f}",
                count_short=rec["count_short"],
                cross_source=rec["cross_source"],
                is_new_entity_mark=is_new_mark,
                window_end=rec["window_end"].strftime("%Y-%m-%d %H:%M"),
                rank=rec.get("rank", 0),
            )
        except KeyError as e:
            logger.warning(
                "realtime alert template missing key: {}, 用默认模板", e
            )
            return (
                f"🔥 {alert_type} {rec['entity']} "
                f"growth={rec['growth_rate']:.1f}"
            )
