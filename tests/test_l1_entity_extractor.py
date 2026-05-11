from __future__ import annotations

"""
EntityExtractor 集成测试（Task 6.2，对应 requirements.md Req 4.5~4.8, 8.5）。

测试策略：
- SQLite in-memory + `Base.metadata.create_all(tables=[...])`，只建本任务涉及的表
- 真 ORM 查询（fetch_unprocessed_for_l1 / mark_l1_processed）直接跑
- 仅替换 `EntityMentionsRepo.bulk_upsert`：生产版走 PG 的
  `on_conflict_do_nothing`，SQLite 不支持
  → 子类 `_SqliteFriendlyMentionsRepo` 用"先查已存在，再 add 新的"模拟幂等
- SQLite 不给 BIGINT 主键分配 rowid，测试里手动递增

覆盖 8 个用例：
- test_entity_extractor_writes_mentions                 —— Req 4.5
- test_entity_extractor_dedup_regex_and_dict            —— Req 4.4 集成
- test_entity_extractor_zero_entities_still_marks_processed —— Req 4.7
- test_entity_extractor_idempotent                      —— Req 4.8
- test_entity_extractor_skips_duplicates                —— Req 2.4 回归
- test_entity_extractor_updates_sliding_counter         —— Req 4.6
- test_entity_extractor_kol_flag                        —— Req 4.5 KOL 打标
- test_entity_extractor_returns_false_on_empty          —— Req 8.5

零 LLM：本文件不 import llm.ollama_client；被测模块也不 import
（Task 8.5 会用 mock 断言 call_count==0，那是端到端测）。
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Iterator, Optional
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from db.models import (
    Base,
    EntityMention,
    NormalizedMessage,
)
from db.repositories.entity_mentions_repo import EntityMentionsRepo
from db.repositories.normalized_messages_repo import NormalizedMessagesRepo
from dictionaries import get_dictionaries
from dictionaries.loader import DictionaryEntry, Dictionaries
from services.l1_entity_extractor import EntityExtractor
from services.l2_sliding_counter import SlidingCounter


# ---------------------------------------------------------------------------
# 工具：SQLite Database + 方言兼容的 repo 子类
# ---------------------------------------------------------------------------


@dataclass
class _SqliteDatabase:
    """最小 Database mock：只提供 `get_session()` contextmanager。"""

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


class _SqliteFriendlyMentionsRepo(EntityMentionsRepo):
    """
    测试版：把 `bulk_upsert` 从 PG 方言改写成 SQLite 兼容实现。

    策略：先查出已存在的 `(msg_id, entity)` 组合，把 rows 里命中的过滤掉，
    剩余的批量 `session.add_all`。等价于 `ON CONFLICT (msg_id, entity) DO NOTHING`
    的语义，且不需要 savepoint（rows 过滤后不会触发 UNIQUE 冲突）。

    同时手动给每个 EntityMention 分配 id（SQLite BigInteger 主键不自增）。
    """

    _id_counter: int = 0

    def bulk_upsert(self, session: Session, rows: list[dict]) -> int:
        if not rows:
            return 0

        # 查已存在的 (msg_id, entity) 组合
        msg_ids = {int(r["msg_id"]) for r in rows}
        existing_stmt = select(EntityMention.msg_id, EntityMention.entity).where(
            EntityMention.msg_id.in_(msg_ids)
        )
        existing = {
            (int(r[0]), str(r[1])) for r in session.execute(existing_stmt).all()
        }

        inserted = 0
        for r in rows:
            key = (int(r["msg_id"]), str(r["entity"]))
            if key in existing:
                continue  # 相当于 ON CONFLICT DO NOTHING
            type(self)._id_counter += 1
            session.add(
                EntityMention(
                    id=type(self)._id_counter,
                    **r,
                )
            )
            inserted += 1
            existing.add(key)  # 防止 rows 内部自带重复

        session.flush()
        return inserted


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db() -> _SqliteDatabase:
    """每个测试独立 in-memory SQLite + 建需要的 3 张表。"""
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    # 只建本任务碰到的表，跳过 summary_level1/2（ARRAY 在 SQLite 上不可渲染）
    Base.metadata.create_all(
        engine,
        tables=[
            NormalizedMessage.__table__,
            EntityMention.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return _SqliteDatabase(session_factory=factory)


@pytest.fixture()
def sliding_counter() -> SlidingCounter:
    return SlidingCounter()


@pytest.fixture()
def service(db: _SqliteDatabase, sliding_counter: SlidingCounter) -> EntityExtractor:
    """构造 EntityExtractor。每个用例独立 id 计数器，避免跨用例污染。"""
    _SqliteFriendlyMentionsRepo._id_counter = 0
    return EntityExtractor(
        db=db,
        normalized_repo=NormalizedMessagesRepo(),
        mentions_repo=_SqliteFriendlyMentionsRepo(),
        sliding_counter=sliding_counter,
        batch_size=100,
    )


def _utc_now() -> datetime:
    """
    测试用 now：返回 **本地 naive** datetime。

    生产用的是 `datetime.now(timezone.utc)`（带 tz），PG TIMESTAMPTZ 完美支持。
    SQLite 对 `DateTime(timezone=True)` 只是"标注类型"，实际存入后 tzinfo
    丢失；再加上 Python naive datetime `.timestamp()` 永远按本地时区解释，
    导致"写入 UTC aware → 读回 naive → .timestamp() 退化 8 小时"的错位。

    解决：测试用 `datetime.now()`（本地 naive），写读两端都不带 tz，
    `.timestamp()` 与 `time.time()` 严格一致——这正是 SlidingCounter 依赖的
    时间基准。业务逻辑不关心绝对的"UTC 语义"，只关心 `ts.timestamp()` 在
    当前进程视角下是正确的 Unix 秒。
    """
    return datetime.now()


def _insert_normalized_msg(
    db: _SqliteDatabase,
    *,
    msg_id: int,
    text: str,
    raw_source: str = "twitter",
    author: Optional[str] = None,
    ts: Optional[datetime] = None,
    is_duplicate: bool = False,
    l1_processed_at: Optional[datetime] = None,
) -> int:
    """测试辅助：直接往 normalized_messages 插一条；绕过业务 repo 的 PG 专属 SQL。"""
    ts = ts or _utc_now()
    with db.get_session() as s:
        s.add(
            NormalizedMessage(
                id=msg_id,
                raw_source=raw_source,
                raw_id=msg_id,  # 单测里 raw_id 和 msg_id 简单等价
                text=text,
                author=author,
                ts=ts,
                is_duplicate=is_duplicate,
                l1_processed_at=l1_processed_at,
            )
        )
        s.commit()
    return msg_id


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------


def test_entity_extractor_writes_mentions(
    db: _SqliteDatabase, service: EntityExtractor
) -> None:
    """
    Req 4.5：注入一条含 `$BTC` 的消息，跑 run_once 后 entity_mentions 多一条。
    BTC 在真实 tickers.yaml 里，走词典路径，confidence=1.0。
    """
    _insert_normalized_msg(db, msg_id=1, text="$BTC heating up on ETF inflows")

    assert service.run_once() is True

    with db.get_session() as s:
        mentions = list(s.scalars(select(EntityMention)).all())

    # 只有 BTC 一条
    btc_mentions = [m for m in mentions if m.entity == "BTC"]
    assert len(btc_mentions) == 1
    em = btc_mentions[0]
    assert em.msg_id == 1
    assert em.entity_type == "ticker"
    assert em.raw_source == "twitter"
    assert em.confidence == 1.0  # 词典命中
    assert em.is_kol_mention is False  # kols.yaml 是空


def test_entity_extractor_dedup_regex_and_dict(
    db: _SqliteDatabase, service: EntityExtractor
) -> None:
    """
    Req 4.4 在 L1 的集成回归：'$BTC 比特币' 正则+中文别名双命中，
    classify 层已去重成 1 条 BTC，落库也应只 1 条（非 2 条）。
    """
    _insert_normalized_msg(db, msg_id=1, text="$BTC 比特币 稳了")

    assert service.run_once() is True

    with db.get_session() as s:
        mentions = list(s.scalars(select(EntityMention)).all())

    btc_mentions = [m for m in mentions if m.entity == "BTC"]
    assert len(btc_mentions) == 1, f"BTC 必须去重，实际：{mentions}"
    assert btc_mentions[0].confidence == 1.0


def test_entity_extractor_zero_entities_still_marks_processed(
    db: _SqliteDatabase, service: EntityExtractor
) -> None:
    """
    Req 4.7：消息没抽到任何实体时，entity_mentions 不写入，
    但 normalized_messages.l1_processed_at 必须被设置，下轮不再重捞。
    """
    # 纯跑题长文本，无 ticker/合约/词典命中
    _insert_normalized_msg(
        db,
        msg_id=1,
        text="今天天气不错，适合出去骑车兜风，顺便去超市买点水果和蔬菜",
    )

    assert service.run_once() is True

    with db.get_session() as s:
        mentions = list(s.scalars(select(EntityMention)).all())
        msg = s.get(NormalizedMessage, 1)

    assert mentions == [], f"无实体不应写 entity_mentions，实际：{mentions}"
    assert msg is not None
    assert msg.l1_processed_at is not None, "即便没实体也应标记已处理"


def test_entity_extractor_idempotent(
    db: _SqliteDatabase, service: EntityExtractor
) -> None:
    """
    Req 4.8：同一条消息被 run_once 两次（第二次通过人为把 l1_processed_at 清空模拟
    老环境下的幂等场景），entity_mentions 不重复。

    真实场景下 `fetch_unprocessed_for_l1` 会过滤已处理消息，所以第二次是空批；
    这里通过把 `l1_processed_at` 重置来强制触发二次处理，验证 DB 层的
    UNIQUE(msg_id, entity) 兜底（子类 repo 模拟了相同语义）。
    """
    _insert_normalized_msg(db, msg_id=1, text="$BTC 加油")

    # 第一轮
    assert service.run_once() is True
    with db.get_session() as s:
        first_count = len(list(s.scalars(select(EntityMention)).all()))
    assert first_count == 1

    # 把 l1_processed_at 清空，强制第二轮再次处理同一条消息
    with db.get_session() as s:
        msg = s.get(NormalizedMessage, 1)
        msg.l1_processed_at = None
        s.commit()

    assert service.run_once() is True
    with db.get_session() as s:
        second_count = len(list(s.scalars(select(EntityMention)).all()))
    # 第二轮不能翻倍
    assert second_count == 1, f"UNIQUE(msg_id, entity) 应防止重复，实际 {second_count}"


def test_entity_extractor_skips_duplicates(
    db: _SqliteDatabase, service: EntityExtractor
) -> None:
    """
    Req 2.4 回归：`is_duplicate=TRUE` 的消息不被 fetch_unprocessed_for_l1 扫到，
    即便含有实体也不应产生 entity_mentions。
    """
    _insert_normalized_msg(db, msg_id=1, text="$BTC 突破", is_duplicate=True)
    _insert_normalized_msg(db, msg_id=2, text="$ETH 强势", is_duplicate=False)

    assert service.run_once() is True

    with db.get_session() as s:
        mentions = list(s.scalars(select(EntityMention)).all())

    # 只有 ETH，没有 BTC（BTC 的那条是 duplicate）
    entity_names = {m.entity for m in mentions}
    assert "ETH" in entity_names
    assert "BTC" not in entity_names, f"duplicate 消息不应被处理，实际实体：{entity_names}"


def test_entity_extractor_updates_sliding_counter(
    db: _SqliteDatabase,
    service: EntityExtractor,
    sliding_counter: SlidingCounter,
) -> None:
    """
    Req 4.6：落库成功后同步 `sliding_counter.add(entity, ts)`，
    调用方 count('BTC', '1h') 应返回 ≥1。
    """
    now = _utc_now()
    _insert_normalized_msg(db, msg_id=1, text="$BTC pump", ts=now)

    assert service.run_once() is True

    # 1h 窗口应该能查到 BTC（ts 是 now）
    assert sliding_counter.count("BTC", "1h") >= 1
    # 15min 也应该在
    assert sliding_counter.count("BTC", "15min") >= 1


def test_entity_extractor_kol_flag(
    db: _SqliteDatabase,
    service: EntityExtractor,
    monkeypatch,
) -> None:
    """
    Req 4.5 KOL 打标：author（小写）在 `dicts.kols` 中时 is_kol_mention=True。

    kols.yaml 当前是空的 `{}`，不能期望真实词典里有 KOL。
    这里通过 monkeypatch 替换 `services.l1_entity_extractor.get_dictionaries`，
    注入一个带 kol 的假 Dictionaries，不影响 prefilter 走的真实词典（它用的是
    `services.prefilter.get_dictionaries`，路径不同）。
    """
    # 拿真实词典做底（tickers/chains/narratives 都保留，只替换 kols）
    real = get_dictionaries()
    fake_kol = DictionaryEntry(
        name="cz_binance",
        entity_type="kol",
        category="ceo",
        aliases=("cz_binance",),
        weight=3.0,
    )
    fake_dicts = Dictionaries(
        tickers=real.tickers,
        chains=real.chains,
        narratives=real.narratives,
        kols=MappingProxyType({"cz_binance": fake_kol}),
        alias_index=real.alias_index,
    )
    monkeypatch.setattr(
        "services.l1_entity_extractor.get_dictionaries",
        lambda: fake_dicts,
    )

    # 两条消息：一条 KOL 发的，一条普通作者
    _insert_normalized_msg(
        db, msg_id=1, text="$BTC is the future", author="cz_binance"
    )
    _insert_normalized_msg(
        db, msg_id=2, text="$BTC 加油", author="random_user"
    )

    assert service.run_once() is True

    with db.get_session() as s:
        mentions = {m.msg_id: m for m in s.scalars(select(EntityMention)).all()}

    assert mentions[1].is_kol_mention is True, "KOL 发的消息必须打标"
    assert mentions[2].is_kol_mention is False, "普通作者不应打标"


def test_entity_extractor_returns_false_on_empty(
    db: _SqliteDatabase, service: EntityExtractor
) -> None:
    """
    Req 8.5：没有待处理消息时，run_once 返回 False（worker 看 False 会 sleep）。
    """
    # 空库
    assert service.run_once() is False

    # 已处理过的消息也不算（l1_processed_at 非空）
    _insert_normalized_msg(
        db,
        msg_id=1,
        text="$BTC",
        l1_processed_at=_utc_now() - timedelta(hours=1),
    )
    assert service.run_once() is False
