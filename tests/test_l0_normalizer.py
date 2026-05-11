from __future__ import annotations

"""
NormalizerService 集成测试（Task 4.5，对应 requirements.md Req 1.1~1.8, 2.2）。

测试策略：
- SQLite in-memory + `Base.metadata.create_all(engine)`，所有表结构共用 ORM 定义
- 真 ORM SELECT（_scan_twitter / _scan_binance / _scan_discord）在 SQLite 上直接运行
- 仅替换 `NormalizedMessagesRepo.insert`：生产版依赖 PG 专有的
  `INSERT ... ON CONFLICT DO NOTHING RETURNING id`，SQLite 不支持
  → 用一个继承子类 `_SqliteFriendlyNormalizedMessagesRepo`，通过
    `session.add` + `session.flush` + 捕获 `IntegrityError` 实现等价语义
- 其他 repo 方法（fetch_recent_simhashes、mark_l1_processed 等）在本任务不被触发，
  不需要处理

覆盖 Req：
- Req 1.1, 1.4：三源扫描（twitter / binance / discord）→ 写入 normalized_messages
- Req 1.2, 1.3：author 归一化（含 Discord "#channel @user" 格式）
- Req 1.5：空内容跳过（strip 后长度 0）
- Req 1.6, 1.7：INSERT ... ON CONFLICT DO NOTHING 的幂等（两次 run_once 不产生两行）
- Req 1.8：绝不触碰原始表的 is_summarized（回归保护）
- Req 8.5：无新数据时 run_once 返回 False

零 LLM 验证：本文件不 import llm.ollama_client，NormalizerService 源码也不 import，
Task 8.5 会在端到端测试里用 mock 断言 `OllamaClient.chat.call_count == 0`。
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from db.models import (
    Base,
    BinanceSquarePost,
    DiscordMessage,
    NormalizedMessage,
    TwitterPost,
)
from db.repositories.normalized_messages_repo import NormalizedMessagesRepo
from services.l0_dedup import Deduplicator
from services.l0_normalizer import NormalizerService


# ---------------------------------------------------------------------------
# 工具：SQLite 版 Database + 兼容 SQLite 的 NormalizedMessagesRepo
# ---------------------------------------------------------------------------


@dataclass
class _SqliteDatabase:
    """模拟生产 `Database` 的最小接口：只提供 `get_session()` contextmanager。

    生产版走 PG 连接池，这里用 SQLite in-memory 的 StaticPool 替代，
    保证同一 engine 内的所有 session 共享一份数据（内存 DB 跨连接不共享）。
    """

    session_factory: sessionmaker[Session]

    @contextmanager
    def get_session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


class _SqliteFriendlyNormalizedMessagesRepo(NormalizedMessagesRepo):
    """
    测试专用 repo：把 `insert` 从 "PG on_conflict_do_nothing + RETURNING" 改写为
    "session.add + flush + 捕获 IntegrityError"，在 SQLite 上等价实现。

    关键补丁：SQLite 把 `BIGINT PRIMARY KEY` 当普通 BIGINT 而不是 auto-rowid，
    所以 autoincrement 不生效，`obj.id` 必须显式赋值。这里在进程内维护一个
    单调递增计数器，和生产版 PG 的 BIGSERIAL 行为等价。

    其他方法（fetch_* / mark_l1_processed）继承自父类，在 SQLite 上用得到时
    仍直接走标准 SQL。
    """

    _id_counter: int = 0

    def insert(
        self,
        session: Session,
        *,
        raw_source: str,
        raw_id: int,
        text: str,
        author: Optional[str],
        ts: datetime,
        engagement: int = 0,
        author_weight: float = 1.0,
        simhash: Optional[int] = None,
        is_duplicate: bool = False,
        dup_of: Optional[int] = None,
    ) -> Optional[int]:
        # 自增 id（SQLite 不会自动给 BigInteger 主键分配 rowid）
        type(self)._id_counter += 1
        obj = NormalizedMessage(
            id=type(self)._id_counter,
            raw_source=raw_source,
            raw_id=raw_id,
            text=text,
            author=author,
            author_weight=author_weight,
            ts=ts,
            engagement=engagement,
            simhash=simhash,
            is_duplicate=is_duplicate,
            dup_of=dup_of,
        )
        # 用 savepoint 吸收 UNIQUE 冲突，只让冲突那一条失败，不污染外层事务
        # （service 整批最后才 commit；savepoint 需要外层有活动事务，这里用
        #  session.begin() 启动一个顶层事务——SQLAlchemy 2.0 下 session 默认
        #  是 autobegin，begin_nested 在没有活动事务时会自动 begin 外层）
        try:
            with session.begin_nested():
                session.add(obj)
                session.flush()
        except IntegrityError:
            return None
        return int(obj.id)


# ---------------------------------------------------------------------------
# pytest fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def db() -> _SqliteDatabase:
    """每个测试用例独立一个 in-memory SQLite + 新 sessionmaker。"""
    # StaticPool 必须用命名数据库 + check_same_thread=False；
    # 这里直接用默认 in-memory 即可，因为 create_engine 会产生一个单连接池。
    # 注意：sqlite:///:memory: 在不同连接之间数据隔离，
    # 必须用 StaticPool 保证所有 session 共用同一条连接。
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    # 不用 `Base.metadata.create_all`：summary_level1 / summary_level2 用 ARRAY(BigInteger)
    # 无法在 SQLite 上渲染。只建本任务会碰到的 4 张表。
    _test_tables = [
        TwitterPost.__table__,
        BinanceSquarePost.__table__,
        DiscordMessage.__table__,
        NormalizedMessage.__table__,
    ]
    Base.metadata.create_all(engine, tables=_test_tables)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return _SqliteDatabase(session_factory=factory)


@pytest.fixture()
def service(db: _SqliteDatabase) -> NormalizerService:
    """构造一个 NormalizerService，绑定测试版 repo + 新鲜 Deduplicator。"""
    # 每个测试用例独立 id 计数器（类变量需要显式重置，否则跨用例污染）
    _SqliteFriendlyNormalizedMessagesRepo._id_counter = 0
    return NormalizerService(
        db=db,
        normalized_repo=_SqliteFriendlyNormalizedMessagesRepo(),
        dedup=Deduplicator(hamming_threshold=3, window_hours=24),
        batch_size=100,
        timezone=ZoneInfo("UTC"),
    )


def _utc_now() -> datetime:
    """带 tz 的 now，避免 SQLite 存入后读出变成 naive 造成比较异常。"""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------


def test_normalizer_ingests_twitter_post(
    db: _SqliteDatabase, service: NormalizerService
) -> None:
    """
    Req 1.1, 1.4：Twitter 单条原始记录通过 run_once 后应写入一条 NormalizedMessage，
    字段映射正确，author 原样透传。
    """
    now = _utc_now()
    with db.get_session() as s:
        s.add(TwitterPost(id=1, content="$BTC 突破 73000", author="@alice", posted_at=now))
        s.commit()

    assert service.run_once() is True

    with db.get_session() as s:
        rows = list(s.scalars(select(NormalizedMessage)).all())
    assert len(rows) == 1
    nm = rows[0]
    assert nm.raw_source == "twitter"
    assert nm.text == "$BTC 突破 73000"
    assert nm.author == "@alice"
    assert nm.is_duplicate is False
    assert nm.dup_of is None
    assert nm.simhash is not None  # 应该算了 SimHash


def test_normalizer_discord_author_format(
    db: _SqliteDatabase, service: NormalizerService
) -> None:
    """
    Req 1.3：Discord 归一化后 author 必须是 `#<channel> @<username>`。
    """
    now = _utc_now()
    with db.get_session() as s:
        s.add(
            DiscordMessage(
                id=1,
                channel_name="alpha-calls",
                username="ye",
                content="早盘观察：$ETH 强势",
                posted_at=now,
            )
        )
        s.commit()

    assert service.run_once() is True

    with db.get_session() as s:
        nm = s.scalar(
            select(NormalizedMessage).where(NormalizedMessage.raw_source == "discord")
        )
    assert nm is not None
    assert nm.author == "#alpha-calls @ye", f"Discord author 格式错误，实际：{nm.author!r}"


def test_normalizer_skips_empty_content(
    db: _SqliteDatabase, service: NormalizerService
) -> None:
    """
    Req 1.5：strip 后长度为 0 的记录跳过，normalized_messages 不新增。
    """
    now = _utc_now()
    with db.get_session() as s:
        # strip 后全空：多种典型场景
        s.add(TwitterPost(id=1, content="   ", author="@a", posted_at=now))
        s.add(TwitterPost(id=2, content="\n\t  ", author="@b", posted_at=now + timedelta(seconds=1)))
        # 非空对照组，验证其他逻辑不被影响
        s.add(TwitterPost(id=3, content="real content here", author="@c", posted_at=now + timedelta(seconds=2)))
        s.commit()

    assert service.run_once() is True  # 因为非空那条被写入

    with db.get_session() as s:
        rows = list(s.scalars(select(NormalizedMessage)).all())
    # 只有非空那一条入库
    assert len(rows) == 1
    assert rows[0].text == "real content here"


def test_normalizer_idempotent(
    db: _SqliteDatabase, service: NormalizerService
) -> None:
    """
    Req 1.6, 1.7：同一条原始记录跑两次 run_once 只产生一条 normalized_messages。

    第二次 run_once 时，LEFT JOIN NormalizedMessage 过滤后应为空（返回 False）。
    """
    now = _utc_now()
    with db.get_session() as s:
        s.add(TwitterPost(id=1, content="unique content for idempotency test", author="@a", posted_at=now))
        s.commit()

    # 第一轮：写入 1 条
    assert service.run_once() is True
    with db.get_session() as s:
        count_after_1 = s.scalar(
            select(func.count()).select_from(NormalizedMessage)
        )
    assert count_after_1 == 1

    # 第二轮：扫描应找不到"未归一化"的行（LEFT JOIN 命中），返回 False
    assert service.run_once() is False
    with db.get_session() as s:
        count_after_2 = s.scalar(
            select(func.count()).select_from(NormalizedMessage)
        )
    assert count_after_2 == 1, "第二次 run_once 不应新增 normalized_messages"


def test_normalizer_does_not_touch_raw_is_summarized(
    db: _SqliteDatabase, service: NormalizerService
) -> None:
    """
    Req 1.8（硬约束，handoff.md §3.2 强调的回归保护）：
    NormalizerService 绝不能触碰三源原始表的 `is_summarized` 字段。
    """
    now = _utc_now()
    with db.get_session() as s:
        s.add(TwitterPost(id=1, content="tweet", author="@a", posted_at=now))
        s.add(BinanceSquarePost(id=1, content="bn-post", author="b-author", posted_at=now))
        s.add(
            DiscordMessage(
                id=1,
                channel_name="ch", username="u", content="dc-msg", posted_at=now
            )
        )
        s.commit()

    assert service.run_once() is True

    # 三源的 is_summarized 必须全部保持 False（默认值）
    with db.get_session() as s:
        tw = s.scalar(select(TwitterPost))
        bn = s.scalar(select(BinanceSquarePost))
        dc = s.scalar(select(DiscordMessage))

    assert tw is not None and tw.is_summarized is False, "Twitter is_summarized 被动过"
    assert bn is not None and bn.is_summarized is False, "Binance is_summarized 被动过"
    assert dc is not None and dc.is_summarized is False, "Discord is_summarized 被动过"


def test_normalizer_returns_false_when_no_new_data(
    db: _SqliteDatabase, service: NormalizerService
) -> None:
    """
    Req 8.5：空库调用 run_once() 返回 False（worker 看到 False 会 sleep）。
    """
    # 三源都空
    assert service.run_once() is False
