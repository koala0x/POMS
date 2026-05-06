from __future__ import annotations

"""
一次摘要业务编排(Level1)。

做什么:
- 轮询统计未处理数量,达到 batch_size 才触发
- 拉取一批最早的未处理数据(优先 created_at,其次 id)
- **预过滤**(services.prefilter):rule-based 把明显噪音剔除,LLM 只看有信号的内容
- 生成 prompt 调用 LLM 得到摘要
- 写入 summary_level1,并将本批原始数据(含被过滤的)全部标记为已处理(幂等)

注意:
- Twitter 与 币安广场 两个 source 全程独立处理(不合并)
- 失败时不更新 is_summarized,保证下次轮询可继续重试
- 整批被预过滤掉时:跳过 LLM 调用,但仍标记为已处理,返回 True 让 worker 继续推进
- DB 操作切换为 SQLAlchemy Session;LLM 调用前会关闭一次 Session,避免长时间占用连接
"""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, Sequence
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy.orm import Session

from db.connection import Database
from llm.ollama_client import OllamaClient
from services.prefilter import split as prefilter_split


class RawRepo(Protocol):
    """
    原始表仓储协议。

    业务层只依赖这三个能力:
    - 统计未处理数量
    - 拉取一批未处理数据
    - 批量标记为已处理
    """

    def count_unsummarized(self, session: Session) -> int: ...

    def fetch_oldest_unsummarized(
        self, session: Session, limit: int
    ) -> list: ...

    def mark_summarized(
        self, session: Session, ids: Sequence[int]
    ) -> int: ...


class Level1Repo(Protocol):
    """summary_level1 表写入协议。"""

    def insert(
        self,
        session: Session,
        source: str,
        summary: str,
        raw_ids: Sequence[int],
        raw_count: int,
        created_at: datetime,
    ) -> int: ...


@dataclass(frozen=True)
class Level1Service:
    """
    单个 source 的一次摘要服务。

    一般会实例化两份:
    - source="twitter"
    - source="binance_square"
    """

    db: Database
    source: str
    batch_size: int
    raw_repo: RawRepo
    level1_repo: Level1Repo
    ollama: OllamaClient
    prompt_path: Path
    timezone: ZoneInfo

    def run_once(self) -> bool:
        """
        执行一次摘要(若数据足够)。

        返回值:
        - True:本次真的处理了一批数据(走完了 LLM + 写库流程)
        - False:数据不足或出现可恢复异常,**未触发** LLM 调用 / 未写库

        worker 循环会用这个返回值决定:处理过 → 立刻进下一轮(可能还有积压);
        没处理过 → sleep poll_interval_seconds 后再 check。
        """
        # 先做轻量统计,避免每次轮询都拉取完整数据。
        try:
            with self.db.get_session() as session:
                cnt = self.raw_repo.count_unsummarized(session)
        except Exception as e:
            logger.error("[{}] 轮询失败:{}", self.source, e)
            return False

        if cnt < self.batch_size:
            logger.info("[{}] 未处理数据不足:{}/{}", self.source, cnt, self.batch_size)
            return False

        # 拉取一批最早的未处理数据(批大小固定为 batch_size)。
        # 这一段不写入,因此 Session 关闭即可,无需 commit。
        try:
            with self.db.get_session() as session:
                posts = self.raw_repo.fetch_oldest_unsummarized(session, self.batch_size)
        except Exception as e:
            logger.error("[{}] 拉取原始数据失败:{}", self.source, e)
            return False

        if len(posts) < self.batch_size:
            logger.warning(
                "[{}] 实际拉取到的未处理数据不足:{}/{}",
                self.source,
                len(posts),
                self.batch_size,
            )
            return False

        # 预过滤:rule-based 剔除明显噪音,LLM 只看有信号的内容。
        # raw_ids_all 记录本批所有原始 id(无论留下还是丢弃),最后都要标记为已处理,
        # 否则被过滤的帖子会被无限重新拉取。
        raw_ids_all = [int(getattr(p, "id")) for p in posts]
        kept, dropped = prefilter_split(posts)
        drop_reasons = Counter(reason for _, reason in dropped)
        logger.info(
            "[{}] 预过滤:{} → {}(丢弃 {}: {})",
            self.source,
            len(posts),
            len(kept),
            len(dropped),
            dict(drop_reasons),
        )

        # 整批均被过滤:跳过 LLM,直接标记并返回 True 让 worker 继续推进
        if not kept:
            try:
                with self.db.get_session() as session:
                    try:
                        self.raw_repo.mark_summarized(session, raw_ids_all)
                        session.commit()
                    except Exception:
                        session.rollback()
                        raise
            except Exception as e:
                logger.error("[{}] 噪音批次标记失败:{}", self.source, e)
                return False
            logger.info(
                "[{}] 整批均为噪音,跳过 LLM,标记 {} 条已处理",
                self.source,
                len(raw_ids_all),
            )
            return True

        # 构造 prompt:模板文件使用 {items} 占位符。
        # 这里直接通过 ORM 实例的属性访问;Session 已关闭但属性已加载,detached 仍可读。
        # 只把 kept 喂给 LLM,被过滤掉的不进 prompt。
        template = self.prompt_path.read_text(encoding="utf-8")
        items = []
        for idx, p in enumerate(kept, start=1):
            author = getattr(p, "author", None) or ""
            posted_at = getattr(p, "posted_at", None)
            meta = f"{author}".strip()
            if posted_at is not None:
                meta = f"{meta} {posted_at}".strip()
            content = (getattr(p, "content", "") or "").strip()
            items.append(f"{idx}. {meta}\n{content}".strip())

        prompt = template.format(items="\n\n".join(items))
        # 把 prompt 长度与开头片段记录到日志,方便排查"模型为什么这么回答"
        # logger.info(
        #     "[{}] 一次摘要 prompt 长度 {} 字符,前 200 字:\n{}",
        #     self.source,
        #     len(prompt),
        #     prompt[:200],
        # )
        try:
            # 调用 LLM。失败则不落库/不标记,等待下次轮询重试。
            summary = self.ollama.chat(prompt)
        except Exception as e:
            logger.error("[{}] 一次摘要失败:{}", self.source, e)
            return False

        # raw_ids 只记录真正进了 LLM 的(kept),保持 raw_count == len(raw_ids) 的不变量;
        # 但 mark_summarized 会标记全部(kept + dropped),否则被过滤的会被重复拉取。
        kept_ids = [int(getattr(p, "id")) for p in kept]
        try:
            with self.db.get_session() as session:
                try:
                    # 同一事务内完成"写入 level1 + 标记原始表",保证一致性。
                    now = datetime.now(self.timezone)
                    level1_id = self.level1_repo.insert(
                        session=session,
                        source=self.source,
                        summary=summary,
                        raw_ids=kept_ids,
                        raw_count=len(kept_ids),
                        created_at=now,
                    )
                    updated = self.raw_repo.mark_summarized(session, raw_ids_all)
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
        except Exception as e:
            logger.error("[{}] 写入/更新数据库失败:{}", self.source, e)
            return False

        if updated != len(raw_ids_all):
            logger.warning(
                "[{}] 原始数据标记条数不一致:期望 {},实际 {}",
                self.source,
                len(raw_ids_all),
                updated,
            )

        logger.info(
            "[{}] 一次摘要完成:level1_id={} kept={}/{}",
            self.source,
            level1_id,
            len(kept_ids),
            len(raw_ids_all),
        )
        return True
