from __future__ import annotations

"""
二次摘要业务编排（Level2）。

职责：
- 每小时整点触发一次，按“过去 1 小时窗口”汇总未二次摘要的 level1 记录
- 将多个 level1.summary 拼接成 prompt 调用 LLM
- 写入 summary_level2，并将涉及的 level1 记录标记为已二次摘要（幂等）

注意：
- 仍按 source 独立处理：twitter / binance_square 不合并
- 若过去 1 小时没有可处理的 level1，则记录日志并跳过
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol, Sequence
from zoneinfo import ZoneInfo

import psycopg2
from loguru import logger

from db.connection import Database
from llm.ollama_client import OllamaClient


class Level1Repo(Protocol):
    """summary_level1 表读取/标记协议。"""

    def fetch_unsummarized_for_period(
        self,
        conn: psycopg2.extensions.connection,
        source: str,
        period_start: datetime,
        period_end: datetime,
    ) -> list: ...

    def mark_summarized_l2(
        self, conn: psycopg2.extensions.connection, ids: Sequence[int]
    ) -> int: ...


class Level2Repo(Protocol):
    """summary_level2 表写入协议。"""

    def insert(
        self,
        conn: psycopg2.extensions.connection,
        source: str,
        summary: str,
        level1_ids: Sequence[int],
        level1_count: int,
        period_start: datetime,
        period_end: datetime,
        created_at: datetime,
    ) -> int: ...


@dataclass(frozen=True)
class Level2Service:
    """
    单个 source 的二次摘要服务。

    按“业务时区”计算整点窗口：
    - period_end：当前时间向下取整到小时（minute/second=0）
    - period_start：period_end 往前 1 小时
    """

    db: Database
    source: str
    level1_repo: Level1Repo
    level2_repo: Level2Repo
    ollama: OllamaClient
    prompt_path: Path
    timezone: ZoneInfo

    def run_hourly(self) -> None:
        # 计算“上一小时窗口”的边界。窗口是 [period_start, period_end)。
        now = datetime.now(self.timezone)
        period_end = now.replace(minute=0, second=0, microsecond=0)
        period_start = period_end - timedelta(hours=1)

        # Step 1: 取出过去一小时内未做二次摘要的 level1。
        try:
            with self.db.get_conn() as conn:
                level1_rows = self.level1_repo.fetch_unsummarized_for_period(
                    conn=conn,
                    source=self.source,
                    period_start=period_start,
                    period_end=period_end,
                )
                conn.commit()
        except Exception as e:
            logger.error("[{}] 拉取一次摘要失败：{}", self.source, e)
            return

        if not level1_rows:
            logger.info(
                "[{}] 过去一小时无可二次摘要数据：{} ~ {}",
                self.source,
                period_start,
                period_end,
            )
            return

        # Step 2: 构造 prompt。模板文件里使用 {items} 占位符。
        template = self.prompt_path.read_text(encoding="utf-8")
        items = []
        for idx, s in enumerate(level1_rows, start=1):
            summary = (getattr(s, "summary", "") or "").strip()
            items.append(f"{idx}. {summary}".strip())

        prompt = template.format(items="\n\n".join(items))
        try:
            # Step 3: 调用 LLM 生成二次摘要。失败则不落库/不标记，下一小时仍可重试。
            l2_summary = self.ollama.chat(prompt)
        except Exception as e:
            logger.error("[{}] 二次摘要失败：{}", self.source, e)
            return

        level1_ids = [int(getattr(s, "id")) for s in level1_rows]
        try:
            with self.db.get_conn() as conn:
                try:
                    # Step 4: 同一事务内写入 level2 并标记 level1，保证一致性。
                    created_at = datetime.now(self.timezone)
                    level2_id = self.level2_repo.insert(
                        conn=conn,
                        source=self.source,
                        summary=l2_summary,
                        level1_ids=level1_ids,
                        level1_count=len(level1_ids),
                        period_start=period_start,
                        period_end=period_end,
                        created_at=created_at,
                    )
                    updated = self.level1_repo.mark_summarized_l2(conn, level1_ids)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        except Exception as e:
            logger.error("[{}] 写入二次摘要失败：{}", self.source, e)
            return

        if updated != len(level1_ids):
            logger.warning(
                "[{}] 一次摘要标记条数不一致：期望 {}，实际 {}",
                self.source,
                len(level1_ids),
                updated,
            )

        logger.info(
            "[{}] 二次摘要完成：level2_id={} level1_count={} period={}~{}",
            self.source,
            level2_id,
            len(level1_ids),
            period_start,
            period_end,
        )
