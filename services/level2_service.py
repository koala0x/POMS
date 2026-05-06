from __future__ import annotations

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
    db: Database
    source: str
    level1_repo: Level1Repo
    level2_repo: Level2Repo
    ollama: OllamaClient
    prompt_path: Path
    timezone: ZoneInfo

    def run_hourly(self) -> None:
        now = datetime.now(self.timezone)
        period_end = now.replace(minute=0, second=0, microsecond=0)
        period_start = period_end - timedelta(hours=1)

        try:
            with self.db.get_conn() as conn:
                level1_rows = self.level1_repo.fetch_unsummarized_for_period(
                    conn=conn,
                    source=self.source,
                    period_start=period_start.replace(tzinfo=None),
                    period_end=period_end.replace(tzinfo=None),
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

        template = self.prompt_path.read_text(encoding="utf-8")
        items = []
        for idx, s in enumerate(level1_rows, start=1):
            summary = (getattr(s, "summary", "") or "").strip()
            items.append(f"{idx}. {summary}".strip())

        prompt = template.format(items="\n\n".join(items))
        try:
            l2_summary = self.ollama.chat(prompt)
        except Exception as e:
            logger.error("[{}] 二次摘要失败：{}", self.source, e)
            return

        level1_ids = [int(getattr(s, "id")) for s in level1_rows]
        try:
            with self.db.get_conn() as conn:
                try:
                    created_at = datetime.now(self.timezone).replace(tzinfo=None)
                    level2_id = self.level2_repo.insert(
                        conn=conn,
                        source=self.source,
                        summary=l2_summary,
                        level1_ids=level1_ids,
                        level1_count=len(level1_ids),
                        period_start=period_start.replace(tzinfo=None),
                        period_end=period_end.replace(tzinfo=None),
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
