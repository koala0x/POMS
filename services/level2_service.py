from __future__ import annotations

"""
二次摘要业务编排(Level2)。

做什么:
- 由 worker 串行循环触发(与 level1 共用同一个线程,避免多个 LLM 请求并发把
  Ollama 上的模型反复 swap)
- 当 summary_level1 中某个 source 的未二次摘要条数 >= threshold 时启动一批
- 拉取最早的 threshold 条 level1,拼接 summary 调 LLM 得到二次摘要
- 写入 summary_level2,并将涉及的 level1 标记为已二次摘要(幂等)

注意:
- 仍按 source 独立处理:twitter / binance_square 不合并
- period_start / period_end 取本批 level1 的 created_at min / max,用来表达
  "本批数据覆盖的时间范围",字段语义不再绑定整点窗口
- 失败时不写库/不标记,下一轮 worker 仍会重试
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, Sequence
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy.orm import Session

from db.connection import Database
from llm.ollama_client import OllamaClient


class Level1Repo(Protocol):
    """summary_level1 表读取/标记协议(level2 视角)。"""

    def count_unsummarized_l2(self, session: Session, source: str) -> int: ...

    def fetch_oldest_unsummarized_l2(
        self, session: Session, source: str, limit: int
    ) -> list: ...

    def mark_summarized_l2(
        self, session: Session, ids: Sequence[int]
    ) -> int: ...


class Level2Repo(Protocol):
    """summary_level2 表写入协议。"""

    def insert(
        self,
        session: Session,
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

    阈值触发(threshold):summary_level1 中未二次摘要的同 source 记录累计达到该值,
    才会真正调 LLM。worker 会用 run_once() 的返回值决定是否立刻进下一轮。
    """

    db: Database
    source: str
    threshold: int
    level1_repo: Level1Repo
    level2_repo: Level2Repo
    ollama: OllamaClient
    prompt_path: Path
    timezone: ZoneInfo

    def run_once(self) -> bool:
        """
        执行一次二次摘要(若数据足够)。

        返回值:
        - True:本次真的处理了一批 level1(调了 LLM + 写库)
        - False:数据不足或可恢复异常,**未触发** LLM 调用 / 未写库
        """
        # 先做轻量计数,避免每轮都拉完整数据。
        try:
            with self.db.get_session() as session:
                cnt = self.level1_repo.count_unsummarized_l2(session, self.source)
        except Exception as e:
            logger.error("[{}] level2 轮询失败:{}", self.source, e)
            return False

        if cnt < self.threshold:
            logger.info(
                "[{}] 未二次摘要的 level1 不足:{}/{}", self.source, cnt, self.threshold
            )
            return False

        # 拉最早的 threshold 条 level1。这一段不写入,Session 关闭即可。
        try:
            with self.db.get_session() as session:
                level1_rows = self.level1_repo.fetch_oldest_unsummarized_l2(
                    session, self.source, self.threshold
                )
        except Exception as e:
            logger.error("[{}] 拉取 level1 失败:{}", self.source, e)
            return False

        if len(level1_rows) < self.threshold:
            logger.warning(
                "[{}] 实际拉取到的 level1 不足:{}/{}",
                self.source,
                len(level1_rows),
                self.threshold,
            )
            return False

        # 构造 prompt:模板使用 {items} 占位符。
        # Session 已关闭但属性已加载,detached 实例仍可读 summary / created_at。
        template = self.prompt_path.read_text(encoding="utf-8")
        items = []
        for idx, s in enumerate(level1_rows, start=1):
            summary = (getattr(s, "summary", "") or "").strip()
            items.append(f"{idx}. {summary}".strip())

        prompt = template.format(items="\n\n".join(items))
        # logger.info(
        #     "[{}] 二次摘要 prompt 长度 {} 字符,前 200 字:\n{}",
        #     self.source,
        #     len(prompt),
        #     prompt[:200],
        # )
        try:
            l2_summary = self.ollama.chat(prompt)
        except Exception as e:
            logger.error("[{}] 二次摘要失败:{}", self.source, e)
            return False

        # 用本批 level1 的 created_at min/max 作为 period 边界,代表"本批数据时间范围"。
        created_at_list = [getattr(s, "created_at") for s in level1_rows]
        period_start = min(created_at_list)
        period_end = max(created_at_list)

        level1_ids = [int(getattr(s, "id")) for s in level1_rows]
        try:
            with self.db.get_session() as session:
                try:
                    # 同事务写 level2 + 标记 level1,保证一致性。
                    created_at = datetime.now(self.timezone)
                    level2_id = self.level2_repo.insert(
                        session=session,
                        source=self.source,
                        summary=l2_summary,
                        level1_ids=level1_ids,
                        level1_count=len(level1_ids),
                        period_start=period_start,
                        period_end=period_end,
                        created_at=created_at,
                    )
                    updated = self.level1_repo.mark_summarized_l2(session, level1_ids)
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
        except Exception as e:
            logger.error("[{}] 写入二次摘要失败:{}", self.source, e)
            return False

        if updated != len(level1_ids):
            logger.warning(
                "[{}] 一次摘要标记条数不一致:期望 {},实际 {}",
                self.source,
                len(level1_ids),
                updated,
            )

        logger.info(
            "[{}] 二次摘要完成:level2_id={} level1_count={} period={}~{}",
            self.source,
            level2_id,
            len(level1_ids),
            period_start,
            period_end,
        )
        return True
