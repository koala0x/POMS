from __future__ import annotations

"""
Phase 1 新链路端到端集成测试（Task 8.5，对应 requirements.md Req 8.9）。

验收场景：
1. 三源原始表 → 新链路 normalizer → entity_extractor → hotness_service 全跑通
2. 最终 hotness_snapshots 产出 rank=1 的 entity 符合"BTC 短窗暴增 + ETH 基线 ≈ baseline 背景"的预期
3. **零 LLM 硬约束**：整个测试过程不触发任何 `OllamaClient.chat` 调用

构造的数据分布（BTC 显著跑赢 ETH）：
- 100 条 `$BTC` 推文（当前时刻附近）→ BTC 短窗 count_short=100, baseline=0
- 10 条 `$ETH` 推文（当前时刻附近）   → ETH 短窗 count_short=10, baseline=少量
- 100 条 `$ETH` 推文（7 天前）         → ETH 基线期有背景量，保证
  count_since(7d) >= 100 能过基线充足性检查

预期排序：BTC 的 growth_rate ≈ 100/2 = 50（basedline=0 走 smoothing=2），
ETH growth_rate ≈ 10/(100/167) = 10/0.6 ≈ 16.7，BTC 明显领先 → rank=1 是 BTC。

测试用 SQLite in-memory + 子类化 repo 绕开 PG on_conflict 方言，
同 Task 4.5 / 6.2 / 7.2 的 pattern。
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterator, Optional
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from db.models import (
    Base,
    EntityMention,
    HotnessSnapshot,
    NormalizedMessage,
    TwitterPost,
)
from db.repositories.entity_mentions_repo import EntityMentionsRepo
from db.repositories.hotness_snapshots_repo import HotnessSnapshotsRepo
from db.repositories.normalized_messages_repo import NormalizedMessagesRepo
from services.l0_dedup import Deduplicator
from services.l0_normalizer import NormalizerService
from services.l1_entity_extractor import EntityExtractor
from services.l2_hotness import HotnessService
from services.l2_sliding_counter import SlidingCounter


# ===========================================================================
# SQLite 适配：Database wrapper + 方言兼容的 repo 子类（复用 Task 4/6/7 pattern）
# ===========================================================================


@dataclass
class _SqliteDatabase:
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


class _SqliteNormalizedMessagesRepo(NormalizedMessagesRepo):
    """SQLite 版：手动 id 自增 + 捕获 IntegrityError 模拟 ON CONFLICT DO NOTHING。"""

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
        try:
            with session.begin_nested():
                session.add(obj)
                session.flush()
        except IntegrityError:
            return None
        return int(obj.id)


class _SqliteEntityMentionsRepo(EntityMentionsRepo):
    """SQLite 版：先查已存在再 add_all，模拟 ON CONFLICT DO NOTHING。"""

    _id_counter: int = 0

    def bulk_upsert(self, session: Session, rows: list[dict]) -> int:
        if not rows:
            return 0

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
                continue
            type(self)._id_counter += 1
            session.add(EntityMention(id=type(self)._id_counter, **r))
            inserted += 1
            existing.add(key)
        session.flush()
        return inserted


class _SqliteHotnessSnapshotsRepo(HotnessSnapshotsRepo):
    """SQLite 版：SELECT 已存在 → UPDATE 或 INSERT，模拟 ON CONFLICT DO UPDATE。"""

    _id_counter: int = 0

    def upsert_batch(
        self,
        session: Session,
        *,
        window_end: datetime,
        window_type: str,
        records: list[dict],
    ) -> int:
        if not records:
            return 0
        entities = [r["entity"] for r in records]
        existing_stmt = select(HotnessSnapshot).where(
            HotnessSnapshot.window_end == window_end,
            HotnessSnapshot.window_type == window_type,
            HotnessSnapshot.entity.in_(entities),
        )
        existing = {r.entity: r for r in session.scalars(existing_stmt).all()}

        for r in records:
            if r["entity"] in existing:
                row = existing[r["entity"]]
                for k, v in r.items():
                    if k == "entity":
                        continue
                    setattr(row, k, v)
            else:
                type(self)._id_counter += 1
                session.add(
                    HotnessSnapshot(
                        id=type(self)._id_counter,
                        window_end=window_end,
                        window_type=window_type,
                        **r,
                    )
                )
        session.flush()
        return len(records)


# ===========================================================================
# Fixture：完整流水线 + 种子数据
# ===========================================================================


@pytest.fixture()
def db() -> _SqliteDatabase:
    """SQLite in-memory，建 Phase 1 需要的 4 张表（跳过 ARRAY 类型的老表）。"""
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            TwitterPost.__table__,
            # 即使本测试不往这两张表写数据，NormalizerService 的三源扫描也会
            # SELECT 它们；表不存在时会抛 OperationalError 整个 scan 失败
            Base.metadata.tables["binance_square_posts"],
            Base.metadata.tables["discord_messages"],
            NormalizedMessage.__table__,
            EntityMention.__table__,
            HotnessSnapshot.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return _SqliteDatabase(session_factory=factory)


def _local_naive_now() -> datetime:
    """
    返回本地 naive datetime。

    同 test_l1_entity_extractor.py 的 `_utc_now`：SQLite 丢失 tzinfo +
    Python naive datetime `.timestamp()` 按本地时区解释，所以测试用本地
    naive 保证写读一致；生产 PG TIMESTAMPTZ 不受影响。
    """
    return datetime.now()


def _seed_twitter_posts(db: _SqliteDatabase) -> datetime:
    """
    种子数据 → 三类：
    - 100 条当前时刻 $BTC（每条内容显著不同避免 SimHash 误判）
    - 10 条当前时刻 $ETH（同样每条独立）
    - 100 条 7 天前 $ETH（基线背景，保证 count_since(7d) >= 100）

    为避免 SimHash（阈值=3）把内容相似的条目判为重复，每条消息内嵌
    随机风格的长句子，让它们之间的 token 分布差异拉大。

    返回 `now` 供后续 hotness 对齐用。
    """
    import random

    random.seed(42)  # 固定随机种子保证测试可重现

    # 为了让 SimHash 距离足够大，每条消息拼一句独立的长文本
    # 用 Faker 风格的模板 + 大量变体词让 token 分布显著不同
    filler_templates = [
        "breaking news from the analyst desk on regulatory updates worldwide",
        "market makers are positioning ahead of the federal reserve meeting",
        "institutional inflows keep accelerating across major trading venues",
        "on-chain metrics show significant whale accumulation over past week",
        "technical analysis suggests a potential breakout from the consolidation",
        "derivatives open interest hits new high across perpetual contracts",
        "spot ETF applications are being reviewed by multiple jurisdictions",
        "stablecoin supply expanded by billions in just this quarter alone",
        "layer two scaling solutions are processing record transaction volumes",
        "decentralized exchange volume surpasses centralized competitor metrics",
    ]

    now = _local_naive_now()
    with db.get_session() as s:
        tid = 0
        # 当前：100 条 $BTC
        for i in range(100):
            tid += 1
            filler = filler_templates[i % len(filler_templates)]
            # 加一个独立 id 和随机数字，保证 simhash 各不相同
            content = (
                f"$BTC alert {i} code {random.randint(10000, 99999)} "
                f"detail {filler} index {i * 7 + 3}"
            )
            s.add(
                TwitterPost(
                    id=tid,
                    content=content,
                    author=f"user_btc_{i}",
                    posted_at=now - timedelta(seconds=i),
                )
            )
        # 当前：10 条 $ETH
        for i in range(10):
            tid += 1
            filler = filler_templates[i % len(filler_templates)]
            content = (
                f"$ETH trend {i} code {random.randint(10000, 99999)} "
                f"detail {filler} index {i * 11 + 5}"
            )
            s.add(
                TwitterPost(
                    id=tid,
                    content=content,
                    author=f"user_eth_{i}",
                    posted_at=now - timedelta(seconds=i),
                )
            )
        # 7 天前：100 条 $ETH（基线背景）
        seven_days_ago = now - timedelta(days=7, hours=1)
        for i in range(100):
            tid += 1
            filler = filler_templates[i % len(filler_templates)]
            content = (
                f"$ETH legacy {i} code {random.randint(10000, 99999)} "
                f"detail {filler} index {i * 13 + 9}"
            )
            s.add(
                TwitterPost(
                    id=tid,
                    content=content,
                    author=f"user_eth_old_{i}",
                    posted_at=seven_days_ago - timedelta(minutes=i),
                )
            )
        s.commit()
    return now


# ===========================================================================
# 测试主用例
# ===========================================================================


def test_phase1_pipeline_end_to_end(db: _SqliteDatabase, monkeypatch) -> None:
    """
    Req 8.9：Phase 1 新链路端到端跑通，BTC 排第一，零 LLM 调用。

    流程：
    1. 种入 210 条 twitter_posts（100 BTC + 10 ETH 当前 + 100 ETH 基线期）
    2. 用 `unittest.mock.patch` 替换 `OllamaClient.chat`，记录 call_count
    3. 依次跑 NormalizerService / EntityExtractor / HotnessService 的 run_once
    4. 断言：
       - hotness_snapshots 至少 1 条记录
       - rank=1 的 entity 是 BTC
       - OllamaClient.chat 的 call_count == 0
    """
    # -------- 1. 种数据 --------
    now = _seed_twitter_posts(db)

    # -------- 2. 构造新链路 service（全部用 SQLite 兼容 repo）--------
    _SqliteNormalizedMessagesRepo._id_counter = 0
    _SqliteEntityMentionsRepo._id_counter = 0
    _SqliteHotnessSnapshotsRepo._id_counter = 0

    normalized_repo = _SqliteNormalizedMessagesRepo()
    mentions_repo = _SqliteEntityMentionsRepo()
    hotness_repo = _SqliteHotnessSnapshotsRepo()
    sliding_counter = SlidingCounter()
    dedup = Deduplicator(hamming_threshold=3, window_hours=24)

    normalizer = NormalizerService(
        db=db,
        normalized_repo=normalized_repo,
        dedup=dedup,
        batch_size=500,
        timezone=ZoneInfo("UTC"),
    )
    extractor = EntityExtractor(
        db=db,
        normalized_repo=normalized_repo,
        mentions_repo=mentions_repo,
        sliding_counter=sliding_counter,
        batch_size=500,
    )
    # 降低 min_baseline_count 门槛：只有 100 条 baseline + 100+10 条 current，
    # count_since(7d) ≈ 210，默认 100 可过；不过 short_hours=1 意味着基线期不含短窗
    # 最近 1h 内的那 100+10 条会被排除，剩下 100 条正好等于门槛
    # 为安全起见把门槛降到 50，避免测试数据波动被边界值卡住
    hotness = HotnessService(
        db=db,
        mentions_repo=mentions_repo,
        hotness_repo=hotness_repo,
        sliding_counter=sliding_counter,
        top_k=20,
        smoothing=2.0,
        short_hours=1,
        baseline_days=7,
        min_baseline_count=50,  # 测试用小门槛
        timezone=ZoneInfo("UTC"),
    )

    # -------- 3. mock OllamaClient.chat 确认零 LLM 调用 --------
    # 即便某个 service 意外引用了 OllamaClient.chat，call_count 也能准确捕捉
    with patch(
        "llm.ollama_client.OllamaClient.chat",
        return_value="THIS SHOULD NOT BE CALLED",
    ) as mock_chat:
        # 阶段 1：归一化
        assert normalizer.run_once() is True, "种了 210 条原始数据，归一化应返回 True"

        # 阶段 2：实体抽取（会同步 add 到 sliding_counter）
        # 可能一轮跑不完（batch_size=500 下应该够），循环直到空
        for _ in range(5):
            if not extractor.run_once():
                break
        # 断言 sliding_counter 已有 BTC/ETH 的短窗计数
        assert sliding_counter.count("BTC", "1h") >= 50, (
            f"预期 BTC 1h 窗口至少 50 次，实际 {sliding_counter.count('BTC', '1h')}"
        )

        # 阶段 3：hotness
        # HotnessService 的 run_once 依赖 datetime.now(tz)；由于我们写入的 ts
        # 是本地 naive（为解决 SQLite tz 坑），这里测试 datetime 本身不 mock，
        # 直接跑真 now。mentions 的 ts 写入时也是本地 naive datetime，两者
        # 作比较时 ORM 会把 datetime.now(tz) 转成 SQLite 的 ISO 字符串；
        # 为了时区可比，HotnessService 构造传 tz=UTC，count_for_entity 的
        # start/end 也是 aware → SQLite 比较时会统一 ISO 字符串化，不会出错
        # 但 baseline_start/short_start 也是 aware，与 db 里 naive 的 ts 做
        # 比较可能产生结果偏差。简化：测试里把 hotness 的 tz 改成 None 等价处理
        # ——实际通过 monkeypatch datetime.now 返回 naive
        import services.l2_hotness as hotness_mod

        class _FakeDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                # 忽略传入 tz，始终返回 local naive，与 seed 数据一致
                return datetime.now()

        monkeypatch.setattr(hotness_mod, "datetime", _FakeDateTime)

        assert hotness.run_once() is True, "hotness 应在基线过门槛 + 有候选时返回 True"

    # -------- 4. 断言结果 --------

    # hotness_snapshots 有记录
    with db.get_session() as s:
        snaps = list(s.scalars(select(HotnessSnapshot).order_by(HotnessSnapshot.rank.asc())).all())
    assert len(snaps) >= 1, "hotness_snapshots 应至少有 1 条"

    # rank=1 的 entity 是 BTC
    top1 = next((s for s in snaps if s.rank == 1), None)
    assert top1 is not None, "应存在 rank=1 的记录"
    assert top1.entity == "BTC", (
        f"rank=1 应是 BTC，实际 {top1.entity}；所有快照：{[(s.rank, s.entity, s.final_score) for s in snaps]}"
    )

    # 零 LLM：断言 OllamaClient.chat 从未被调用
    assert mock_chat.call_count == 0, (
        f"Phase 1 新链路必须零 LLM 调用，实际 call_count={mock_chat.call_count}"
    )


def test_phase1_pipeline_zero_llm_import_smoke() -> None:
    """
    补充的"导入期"零 LLM 约束验证：
    新链路 5 个模块（l0_normalizer / l0_dedup / l1_entity_extractor /
    l2_sliding_counter / l2_hotness）的 import 阶段不应主动调 OllamaClient。

    说明：这不是完备的零 LLM 证明（某模块里可能只 import 了类但不调 chat），
    完备证明是上面端到端 case 里 `mock_chat.call_count == 0`。
    这里只是一个快速冒烟：import 即暴露的 typo、非预期的顶层调用能被发现。
    """
    with patch("llm.ollama_client.OllamaClient.chat") as mock_chat:
        # 重新 import 链路上的所有新模块（若之前已加载过，import 不会重执行）
        # 关键：不构造也不 run_once，只是访问模块对象
        import services.l0_dedup  # noqa: F401
        import services.l0_normalizer  # noqa: F401
        import services.l1_entity_extractor  # noqa: F401
        import services.l2_hotness  # noqa: F401
        import services.l2_sliding_counter  # noqa: F401

        assert mock_chat.call_count == 0, (
            "新链路模块 import 阶段不应触发任何 OllamaClient.chat 调用"
        )
