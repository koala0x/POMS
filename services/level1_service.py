from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, Sequence
from zoneinfo import ZoneInfo

import psycopg2
from loguru import logger

from db.connection import Database
from llm.ollama_client import OllamaClient


class RawRepo(Protocol):
    def count_unsummarized(self, conn: psycopg2.extensions.connection) -> int: ...

    def fetch_oldest_unsummarized(
        self, conn: psycopg2.extensions.connection, limit: int
    ) -> list: ...

    def mark_summarized(
        self, conn: psycopg2.extensions.connection, ids: Sequence[int]
    ) -> int: ...


class Level1Repo(Protocol):
    def insert(
        self,
        conn: psycopg2.extensions.connection,
        source: str,
        summary: str,
        raw_ids: Sequence[int],
        raw_count: int,
        created_at: datetime,
    ) -> int: ...


@dataclass(frozen=True)
class Level1Service:
    db: Database
    source: str
    batch_size: int
    raw_repo: RawRepo
    level1_repo: Level1Repo
    ollama: OllamaClient
    prompt_path: Path
    timezone: ZoneInfo

    def run_once(self) -> None:
        try:
            with self.db.get_conn() as conn:
                cnt = self.raw_repo.count_unsummarized(conn)
                conn.commit()
        except Exception as e:
            logger.error("[{}] 轮询失败：{}", self.source, e)
            return

        if cnt < self.batch_size:
            logger.info("[{}] 未处理数据不足：{}/{}", self.source, cnt, self.batch_size)
            return

        try:
            with self.db.get_conn() as conn:
                posts = self.raw_repo.fetch_oldest_unsummarized(conn, self.batch_size)
                conn.commit()
        except Exception as e:
            logger.error("[{}] 拉取原始数据失败：{}", self.source, e)
            return

        if len(posts) < self.batch_size:
            logger.warning(
                "[{}] 实际拉取到的未处理数据不足：{}/{}",
                self.source,
                len(posts),
                self.batch_size,
            )
            return

        template = self.prompt_path.read_text(encoding="utf-8")
        items = []
        for idx, p in enumerate(posts, start=1):
            author = getattr(p, "author", None) or ""
            posted_at = getattr(p, "posted_at", None)
            meta = f"{author}".strip()
            if posted_at is not None:
                meta = f"{meta} {posted_at}".strip()
            content = (getattr(p, "content", "") or "").strip()
            items.append(f"{idx}. {meta}\n{content}".strip())

        prompt = template.format(items="\n\n".join(items))
        try:
            summary = self.ollama.chat(prompt)
        except Exception as e:
            logger.error("[{}] 一次摘要失败：{}", self.source, e)
            return

        raw_ids = [int(getattr(p, "id")) for p in posts]
        try:
            with self.db.get_conn() as conn:
                try:
                    now = datetime.now(self.timezone)
                    level1_id = self.level1_repo.insert(
                        conn=conn,
                        source=self.source,
                        summary=summary,
                        raw_ids=raw_ids,
                        raw_count=len(raw_ids),
                        created_at=now,
                    )
                    updated = self.raw_repo.mark_summarized(conn, raw_ids)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        except Exception as e:
            logger.error("[{}] 写入/更新数据库失败：{}", self.source, e)
            return

        if updated != len(raw_ids):
            logger.warning(
                "[{}] 原始数据标记条数不一致：期望 {}，实际 {}",
                self.source,
                len(raw_ids),
                updated,
            )

        logger.info(
            "[{}] 一次摘要完成：level1_id={} raw_count={}",
            self.source,
            level1_id,
            len(raw_ids),
        )
