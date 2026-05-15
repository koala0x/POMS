from __future__ import annotations

"""
L2 定期热榜 Digest 推送服务（Phase 2.8 新增）。

背景（为什么要有这个 service）：
- Phase 1 的 Level1Service / Level2Service（老链路）已淘汰；它们承担了
  "周期性把热点摘要推 Telegram" 的输出通道
- Phase 2 的 AlertTriggerService 是**事件触发型**：只有 entity 通过
  growth/count/cross 三道门槛 + 通过冷却决策树才会推
- 结果：低流量场景下用户大量 entity 落进"首次 → 然后再无质变"分支被静默，
  用户视角看不到"全貌"，只看到零星 [首次] / [跨源升级]

DigestPusher 补回这条通道：
- 每 15 分钟整点对齐触发一次（与 HotnessService 同款 align_to_quarter）
- 直接读 hotness_snapshots（绕过 AlertTriggerService 的冷却 / 阈值）
- 把 1h/6h/24h 三窗口的最新 Top-N 拼成一条 Markdown 消息推 Telegram
- 提供"看全貌"的稳定输出，与 AlertTriggerService 的"看突变"互补

关键设计点：
- ★ **只读 DB，不改任何状态**：不写表、不影响 alert 冷却、不影响 hotness
  计算。失败仅 log error，不重试（下个整点再来一份新的）
- ★ **零 LLM**：本服务不 import llm.ollama_client；榜单数据本身就是答案
- ★ **空榜也推**：哪怕某个窗口冷启动期还没数据，也明确告诉用户"24h 榜
  暂未生成"，比"什么都没有"对运维更友好
- ★ **Markdown 渲染**：用 Telegram 的 Markdown parse_mode，让榜单更易读；
  实体名走代码块包裹避免特殊字符破坏 Markdown
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

from loguru import logger

from db.connection import Database
from db.repositories.hotness_snapshots_repo import HotnessSnapshotsRepo
from notifications.telegram_client import TelegramClient
from services.l2_hotness import align_to_quarter


# 默认推送的窗口列表（按 1h → 3h → 6h → 24h 顺序拼到同一条消息里）
_DEFAULT_WINDOW_TYPES: tuple[str, ...] = ("1h", "3h", "6h", "24h")

# 单条 Telegram 消息渲染窗口标题 + 行模板
_SECTION_HEADER = {
    "1h": "🔥 1h 榜（短期突变）",
    "3h": "🌅 3h 榜（中短期热度）",
    "6h": "📈 6h 榜（中期趋势）",
    "24h": "🌐 24h 榜（宏观叙事）",
}


def _escape_markdown(text: str) -> str:
    """
    转义 Telegram 旧版 Markdown 的特殊字符（_ * ` [）。

    实体名在词典里大多是字母/数字/下划线，少量带 `$`/`-`，老 Markdown 里
    `_` 会被解释成斜体起止；包到 `code` 里反而最稳。本函数留作备用，
    当前实现走代码块包裹（更省心）。
    """
    return (
        text.replace("\\", "\\\\")
        .replace("_", "\\_")
        .replace("*", "\\*")
        .replace("`", "\\`")
        .replace("[", "\\[")
    )


@dataclass
class DigestPusherService:
    """
    定期热榜 Digest 推送器。

    与 AlertTriggerService 的关系：
    - 两者**互不依赖、互不共享状态**
    - DigestPusher 不读 _alert_records、不写 send 失败重试队列
    - DigestPusher 失败也不影响 AlertTriggerService 的下一轮工作

    与 HotnessService 的关系：
    - 调度顺序：必须排在所有 HotnessService 实例之后（保证最新榜单已写入）
    - 不依赖 HotnessService 的进程内状态；只读 DB（hotness_snapshots 表）
    """

    db: Database
    hotness_repo: HotnessSnapshotsRepo
    telegram_client: TelegramClient

    # 推送哪些窗口（按数组顺序拼接成同一条消息）
    window_types: tuple[str, ...] = _DEFAULT_WINDOW_TYPES

    # 每个窗口取 Top 多少（默认 10，太多消息会超 Telegram 4096 字符上限）
    top_n: int = 10

    # 推送间隔（整刻钟数）：1=每 15min 推一次，4=每小时 :00 整点推一次。
    # 默认 4 = 每小时一次，避免刷屏；用户嫌少可改 2（半小时）/ 1（每 15min）。
    push_every_quarters: int = 4

    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"))

    # 运行时状态：上次成功扫描过的 window_end，避免同窗口重复推送
    _last_pushed_window_end: Optional[datetime] = None

    # ----------------------------------------------------------------------
    # 公共 API
    # ----------------------------------------------------------------------

    def run_once(self) -> bool:
        """
        执行一轮 digest 推送决策。

        返回值：
        - True：本轮成功推送了一条 digest
        - False：跳过（未到对齐时刻 / 同窗口已推 / Telegram 推送失败）

        跳过场景：
        1. 当前 window_end 未到 push_every_quarters 边界
           （比如 push_every_quarters=4 时只在每小时 :00 推送）
        2. 当前 window_end == _last_pushed_window_end：同窗口已推
        3. send_text 失败：log error，下一窗口再试
        """
        now = datetime.now(self.timezone)
        window_end = align_to_quarter(now)

        # 跳过场景 1：未到对齐边界
        # push_every_quarters=4 → 只在 minute % 60 == 0 时推（即整点）
        # push_every_quarters=2 → 只在 minute % 30 == 0 时推（每半小时）
        # push_every_quarters=1 → 每个 15min 整刻钟都推
        align_minutes = self.push_every_quarters * 15
        if window_end.minute % align_minutes != 0:
            return False

        # 跳过场景 2：同窗口已推
        if (
            self._last_pushed_window_end is not None
            and window_end <= self._last_pushed_window_end
        ):
            return False

        # 拉每个窗口的 Top-N
        sections: list[str] = []
        try:
            with self.db.get_session() as session:
                for wt in self.window_types:
                    section = self._render_window_section(session, window_type=wt)
                    sections.append(section)
                # 数据源命中率统计（过去 1h 的 normalized_messages vs entity_mentions）
                source_stats = self._fetch_source_stats(session)
        except Exception as e:
            # DB 读失败：log error 后跳过本轮；不更新 _last_pushed_window_end
            # 让下一轮还能重试（同一 window_end 再来一次）
            logger.error("digest fetch failed: {}", e)
            return False

        # 头部加一个总体时间戳，便于用户对照"这是哪个 15min 整点的快照"
        # 显式转 self.timezone：window_end 来自 datetime.now(tz)，本来就有 tz；
        # 但 align_to_quarter 用 .replace(...) 不破坏 tzinfo，保险起见再转一次
        header = f"📊 *热榜快照* @ {self._fmt_local(window_end)}\n"
        body = header + "\n\n".join(sections)

        # 追加数据源命中率
        if source_stats:
            body += "\n\n" + source_stats

        # 推 Telegram；用 Markdown parse_mode 让标题加粗 + 实体名走 code 块
        ok = self.telegram_client.send_text(body, parse_mode="Markdown")
        if ok:
            self._last_pushed_window_end = window_end
            logger.info(
                "digest pushed: window_end={} sections={} total_chars={}",
                window_end,
                len(sections),
                len(body),
            )
            return True
        # 推送失败：log 一行；不更新 _last_pushed_window_end，下一窗口（15min 后）再试
        # 注意：telegram_client.send_text 失败时自己已经 log.error 过详细原因
        logger.warning(
            "digest send failed (will retry next window): window_end={}",
            window_end,
        )
        return False

    # ----------------------------------------------------------------------
    # 内部：渲染单个窗口的 section
    # ----------------------------------------------------------------------

    def _render_window_section(self, session, *, window_type: str) -> str:
        """
        渲染单个窗口的 Markdown section。

        - 取 hotness_snapshots 最新 window_end 的 Top-N
        - 没数据 → 返回 "<header>\n（暂未生成）" 让用户感知服务状态
        - 每行：rank. `entity` (entity_type) growth=X.Yx count=N src=M [新]
        """
        header = _SECTION_HEADER.get(window_type, f"窗口 {window_type}")

        latest = self.hotness_repo.fetch_latest_window_end(session, window_type)
        if latest is None:
            return f"*{header}*\n_（暂未生成）_"

        records = self.hotness_repo.fetch_top_k(
            session, window_end=latest, window_type=window_type, k=self.top_n
        )
        if not records:
            return f"*{header}*\n_（最新窗口为空）_"

        lines = [f"*{header}*  _（@ {self._fmt_local(latest, time_only=True)}）_"]
        for rec in records:
            lines.append(self._format_record_line(rec))
        return "\n".join(lines)

    def _fetch_source_stats(self, session) -> str:
        """
        统计过去 1h 每个数据源的消息总数 vs 命中实体的消息数，渲染成 Markdown 段落。

        "命中"定义：该 raw_source 的 normalized_message 在 entity_mentions 里
        至少有 1 条记录（即 prefilter keep + 词典/正则抽到了实体）。

        返回空字符串表示查不到数据（不追加到消息里）。
        """
        from sqlalchemy import text

        try:
            rows = session.execute(
                text(
                    """
                    SELECT
                      nm.raw_source,
                      COUNT(*) AS total,
                      COUNT(DISTINCT em.msg_id) AS hit
                    FROM normalized_messages nm
                    LEFT JOIN entity_mentions em
                      ON em.msg_id = nm.id
                      AND em.ts >= NOW() - INTERVAL '1 hour'
                    WHERE nm.created_at >= NOW() - INTERVAL '1 hour'
                      AND nm.is_duplicate = FALSE
                    GROUP BY nm.raw_source
                    ORDER BY total DESC
                    """
                )
            ).all()
        except Exception as e:
            logger.warning("digest source stats query failed: {}", e)
            return ""

        if not rows:
            return ""

        lines = ["📡 *数据源概况*（过去 1h）"]
        for r in rows:
            total = int(r.total)
            hit = int(r.hit)
            pct = f"{100.0 * hit / total:.1f}%" if total > 0 else "-"
            lines.append(f"  {r.raw_source:<18} {total:>4} 条 → 命中 {hit} 条（{pct}）")
        return "\n".join(lines)

    def _fmt_local(self, dt, *, time_only: bool = False) -> str:
        """
        统一时区渲染。

        - dt 可能是 aware (带 tzinfo) 或 naive (从 PG TIMESTAMPTZ 取出后偶发为 naive UTC)
        - aware → astimezone(self.timezone) 转到业务时区
        - naive → 假定是 UTC，先 replace 再转

        time_only=True 只显示 HH:MM；False 显示完整 YYYY-MM-DD HH:MM
        """
        from datetime import timezone as _utc_tz

        if dt.tzinfo is None:
            # SQLAlchemy 偶发把 TIMESTAMPTZ 拿成 naive，按 UTC 假定（PG 默认存 UTC）
            dt = dt.replace(tzinfo=_utc_tz.utc)
        local = dt.astimezone(self.timezone)
        if time_only:
            return local.strftime("%H:%M")
        return local.strftime("%Y-%m-%d %H:%M")

    @staticmethod
    def _format_record_line(rec) -> str:
        """
        格式化一行：

            1. `BTC` (ticker)  growth=12.3x  提及=42  跨源=2  ★

        - entity 用反引号包裹，避免特殊字符（如 _）触发 Markdown 解析
        - growth/count/cross 用空格对齐感弱化数字层级，便于横向扫描
        - is_new_entity → 行尾加 ★ 标记，眼睛立刻能扫到"今天才出现的新词"
        """
        entity_type = rec.entity_type or "n/a"
        new_mark = " ★" if rec.is_new_entity else ""
        growth = rec.growth_rate if rec.growth_rate is not None else 0.0
        return (
            f"{rec.rank}. `{rec.entity}` ({entity_type})  "
            f"growth={growth:.1f}x  提及={rec.count_short}  "
            f"跨源={rec.cross_source}{new_mark}"
        )


__all__ = ["DigestPusherService"]
