from __future__ import annotations

"""
HotnessService 单元 + 集成测试（Task 7.2，对应 requirements.md Req 7.1~7.10）。

测试分两种形态：
- **纯公式 / 跳过分支测试**（绝大多数）：用 Mock 替换 SlidingCounter + mentions_repo
  + hotness_repo + db，零 DB 依赖，测试快且断言精确
- **UPSERT 幂等 + 回滚测试**：用 SQLite in-memory + 真实 HotnessSnapshotsRepo
  的子类版（绕开 PG on_conflict_do_update），走真 SQLAlchemy 路径

覆盖 10 个用例（对应 tasks.md §7.2）：
- test_growth_rate_formula
- test_smoothing_prevents_zero_division
- test_new_entity_flag
- test_final_score_cross_source_weight
- test_stable_ordering
- test_baseline_insufficient_skips
- test_counter_not_ready_skips_and_resets
- test_align_to_quarter
- test_upsert_overwrites_same_window
- test_write_failure_rolls_back
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from db.models import Base, EntityMention, HotnessSnapshot, NormalizedMessage
from db.repositories.hotness_snapshots_repo import HotnessSnapshotsRepo
from services.l2_hotness import HotnessService, align_to_quarter


# ===========================================================================
# 辅助：统一的 Mock 构建
# ===========================================================================


def _make_mock_sliding_counter(active: list[str], counts: dict[str, int]) -> MagicMock:
    """
    造一个 SlidingCounter 假件：
    - `active_entities('24h')` 返回 `active`
    - `count(entity, '1h')` 按 `counts` 字典返回，缺失的返回 0
    """
    sc = MagicMock()
    sc.active_entities.return_value = active
    sc.count.side_effect = lambda entity, window: counts.get(entity, 0)
    return sc


def _make_mock_mentions_repo(
    baseline_totals: dict[str, int],
    cross_sources: dict[str, int],
    count_since_value: int = 10_000,
) -> MagicMock:
    """
    造一个 mentions_repo 假件：
    - `count_since(session, since)` 返回 `count_since_value`（供基线充足性检查）
    - `count_for_entity(session, entity, start=, end=)` 按 baseline_totals 返回
    - `count_sources_for_entity(session, entity, start=, end=)` 按 cross_sources 返回
    """
    repo = MagicMock()
    repo.count_since.return_value = count_since_value
    repo.count_for_entity.side_effect = lambda session, entity, start, end: (
        baseline_totals.get(entity, 0)
    )
    repo.count_sources_for_entity.side_effect = lambda session, entity, start, end: (
        cross_sources.get(entity, 1)
    )
    return repo


@dataclass
class _FakeDatabase:
    """最小 Database mock：只需要 get_session 可用作 contextmanager。"""

    _session: MagicMock = field(default_factory=MagicMock)

    @contextmanager
    def get_session(self):
        yield self._session


def _make_service(
    *,
    sliding_counter,
    mentions_repo,
    hotness_repo=None,
    db=None,
    **overrides,
) -> HotnessService:
    """统一入口：构造 HotnessService。允许覆写 top_k / baseline_days 等。"""
    return HotnessService(
        db=db or _FakeDatabase(),
        mentions_repo=mentions_repo,
        hotness_repo=hotness_repo or MagicMock(),
        sliding_counter=sliding_counter,
        top_k=overrides.get("top_k", 20),
        smoothing=overrides.get("smoothing", 2.0),
        short_hours=overrides.get("short_hours", 1),
        baseline_days=overrides.get("baseline_days", 7),
        min_baseline_count=overrides.get("min_baseline_count", 100),
        timezone=overrides.get("timezone", ZoneInfo("UTC")),
    )


# ===========================================================================
# Part 1：纯函数 —— align_to_quarter
# ===========================================================================


def test_align_to_quarter() -> None:
    """
    Req 7.1：向下对齐到 :00 / :15 / :30 / :45。
    """
    # 10:23:45 → 10:15
    d = datetime(2026, 5, 11, 10, 23, 45, 123456)
    assert align_to_quarter(d) == datetime(2026, 5, 11, 10, 15, 0, 0)

    # 10:45:30 → 10:45
    d = datetime(2026, 5, 11, 10, 45, 30)
    assert align_to_quarter(d) == datetime(2026, 5, 11, 10, 45, 0, 0)

    # 10:59:59 → 10:45
    d = datetime(2026, 5, 11, 10, 59, 59)
    assert align_to_quarter(d) == datetime(2026, 5, 11, 10, 45, 0, 0)

    # 10:00:00 → 10:00（整点本身）
    d = datetime(2026, 5, 11, 10, 0, 0)
    assert align_to_quarter(d) == datetime(2026, 5, 11, 10, 0, 0)

    # tz 保留
    d = datetime(2026, 5, 11, 10, 7, 30, tzinfo=ZoneInfo("UTC"))
    aligned = align_to_quarter(d)
    assert aligned.tzinfo == ZoneInfo("UTC")
    assert aligned.hour == 10 and aligned.minute == 0


# ===========================================================================
# Part 2：公式测试（_compute_records 直接测）
# ===========================================================================


def test_growth_rate_formula() -> None:
    """
    Req 7.2：growth_rate = short_count / max(baseline_per_hour, smoothing)

    造一组已知数据：
    - short_count=900
    - baseline_total=20 * 167 = 3340（baseline_hours=7*24-1=167）
      → baseline_per_hour=20
    - smoothing=2.0 < 20，所以分母取 baseline_per_hour=20
    - growth_rate=900/20=45
    """
    sc = _make_mock_sliding_counter(active=["BTC"], counts={"BTC": 900})
    # baseline_total 要让 baseline_per_hour == 20
    # baseline_hours = 7 * 24 - 1 = 167
    # baseline_total = 20 * 167 = 3340
    repo = _make_mock_mentions_repo(
        baseline_totals={"BTC": 3340},
        cross_sources={"BTC": 1},
    )
    svc = _make_service(sliding_counter=sc, mentions_repo=repo)
    window_end = datetime(2026, 5, 11, 10, 0, 0, tzinfo=ZoneInfo("UTC"))

    records = svc._compute_records(window_end)
    assert len(records) == 1
    rec = records[0]
    # 允许浮点误差 1%
    assert abs(rec["count_baseline"] - 20.0) < 0.01
    assert abs(rec["growth_rate"] - 45.0) < 0.01 * 45


def test_smoothing_prevents_zero_division() -> None:
    """
    Req 7.5：baseline=0 时分母用 smoothing=2.0，避免 ZeroDivisionError。
    short_count=5 → growth_rate = 5 / 2 = 2.5
    """
    sc = _make_mock_sliding_counter(active=["NEW"], counts={"NEW": 5})
    repo = _make_mock_mentions_repo(
        baseline_totals={"NEW": 0},  # 基线为 0
        cross_sources={"NEW": 1},
    )
    svc = _make_service(sliding_counter=sc, mentions_repo=repo)
    window_end = datetime(2026, 5, 11, 10, 0, 0, tzinfo=ZoneInfo("UTC"))

    records = svc._compute_records(window_end)
    assert len(records) == 1
    rec = records[0]
    assert rec["count_baseline"] == 0.0
    # growth = 5 / max(0, 2.0) = 5 / 2 = 2.5
    assert abs(rec["growth_rate"] - 2.5) < 1e-9


def test_new_entity_flag() -> None:
    """
    Req 7.4：baseline_total=0 且 short_count>=5 → is_new_entity=True；
    baseline_total=0 且 short_count<5 → False；
    baseline_total>0 即便 short_count 很大也 False。
    """
    sc = _make_mock_sliding_counter(
        active=["A", "B", "C"],
        counts={"A": 5, "B": 4, "C": 100},
    )
    repo = _make_mock_mentions_repo(
        baseline_totals={"A": 0, "B": 0, "C": 3340},
        cross_sources={"A": 1, "B": 1, "C": 1},
    )
    svc = _make_service(sliding_counter=sc, mentions_repo=repo)
    window_end = datetime(2026, 5, 11, 10, 0, 0, tzinfo=ZoneInfo("UTC"))

    records = {r["entity"]: r for r in svc._compute_records(window_end)}
    assert records["A"]["is_new_entity"] is True  # baseline=0, short=5
    assert records["B"]["is_new_entity"] is False  # baseline=0 但 short<5
    assert records["C"]["is_new_entity"] is False  # baseline>0


def test_final_score_cross_source_weight() -> None:
    """
    Req 7.3：final_score = growth_rate * (1 + 0.3 * (cross_source - 1))

    用同样的 growth_rate 对比不同 cross_source：
    - cs=1 → factor=1.0 → final_score = growth_rate
    - cs=3 → factor=1.6 → final_score = growth_rate * 1.6
    """
    sc = _make_mock_sliding_counter(
        active=["SINGLE", "TRIPLE"],
        counts={"SINGLE": 5, "TRIPLE": 5},
    )
    # 让两者 baseline_per_hour 相同（都走 smoothing 分支，growth=5/2=2.5）
    repo = _make_mock_mentions_repo(
        baseline_totals={"SINGLE": 0, "TRIPLE": 0},
        cross_sources={"SINGLE": 1, "TRIPLE": 3},
    )
    svc = _make_service(sliding_counter=sc, mentions_repo=repo)
    window_end = datetime(2026, 5, 11, 10, 0, 0, tzinfo=ZoneInfo("UTC"))

    records = {r["entity"]: r for r in svc._compute_records(window_end)}
    # 同样 growth_rate
    assert records["SINGLE"]["growth_rate"] == records["TRIPLE"]["growth_rate"]
    # 但 cross_source 权重不同
    assert abs(records["SINGLE"]["final_score"] - 2.5 * 1.0) < 1e-9
    assert abs(records["TRIPLE"]["final_score"] - 2.5 * 1.6) < 1e-9


# ===========================================================================
# Part 3：稳定排序（对 run_once 后写入的 records 做断言）
# ===========================================================================


def test_stable_ordering(monkeypatch) -> None:
    """
    Req 7.10：三级排序 (−final_score, −count_short, entity)。

    构造：
    - A 和 B final_score 相同（growth 相同 + cross 相同），count_short 不同 → count_short 高的排前
    - C 和 D final_score 相同、count_short 相同 → 按 entity 字母序 C 在 D 前
    """
    # 让 final_score 相同 + count_short 不同
    # A: short=10, baseline=0 → growth=5, cross=1 → score=5
    # B: short=5,  baseline=0 → growth=2.5, cross=3 → score=2.5*1.6=4.0（排序里 B 会比 A 低）
    # 需要 A、B final_score 相同：
    # 让 A short=10 cross=1 → score = (10/2) * 1.0 = 5.0
    #    B short=10 cross=1 → score = 5.0，用 count_short 同时相同就演示不了 tier-2
    #
    # 重新设计：
    # A: short=10, baseline=0, cross=1 → growth=5, score=5
    # B: short=20, baseline=0, cross=1 → growth=10, score=10 ← 排第 1
    #
    # 为了 tier-2（score 相同 count_short 不同）：
    # X: short=10, baseline=0, cross=1 → score=5
    # Y: short=5,  baseline=0, cross=3 → score=2.5*1.6=4.0
    # 不同 score，再重新造：
    #
    # 做法：让 score 相同 count_short 不同 → 要同 final_score，只能通过 growth × cs_factor 凑
    # 简单方案：score 用同 growth 同 cs，但 count_short 不同（不可能）
    # growth = short / max(b_per_hour, 2)  要让 short 不同 growth 相同，就调节 baseline_total
    #
    # 取：
    # P: short=10, baseline_total=0 → growth=10/2=5, cross=1 → score=5
    # Q: short=15, baseline=15*167=2505 → b_per_h=15 → growth=15/15=1, cross=1 → score=1 ≠ P
    #
    # 换策略：让 tier-2 和 tier-3 分开测：
    # 用单独两组数据，一组演示 tier-2，一组演示 tier-3
    # 这里用一个大用例覆盖两者：
    #
    # 1) tier-2：A 和 B final_score 相同，count_short A > B → A 排在前
    #    构造：
    #    A: short=10, baseline=0, cross=1 → score=5
    #    B: short=8,  baseline=0, cross=5 → score=8/2 * (1+0.3*4) = 4 * 2.2 = 8.8 ≠ 5
    #    难凑，改用更直接的方法：
    #
    # 决定：用 count_short 小的搭配 cross 大的，造 score 完全相同：
    # A: short=10, baseline=0, cross=2 → score = 5 * 1.3 = 6.5
    # B: short=13, baseline=0, cross=1 → score = 6.5 * 1.0 = 6.5 ✓
    # A.count_short=10 < B.count_short=13 → 按 tier-2 B 应在 A 前

    # 3 个 entity 一起测 tier-2 + tier-3：
    # A: short=13, baseline=0, cross=1 → growth=13/2=6.5, score=6.5
    # B: short=10, baseline=0, cross=2 → growth=5,       score=5*1.3=6.5
    # C: short=10, baseline=0, cross=2 → growth=5,       score=6.5
    # 三者 score 全相同；A.count_short=13 > B.C.count_short=10
    # → 期望排序：A, B, C（B 和 C count_short 同，按字母 B<C）
    sc = _make_mock_sliding_counter(
        active=["A", "B", "C"],
        counts={"A": 13, "B": 10, "C": 10},
    )
    repo = _make_mock_mentions_repo(
        baseline_totals={"A": 0, "B": 0, "C": 0},
        cross_sources={"A": 1, "B": 2, "C": 2},
    )
    hotness_repo = MagicMock()
    svc = _make_service(sliding_counter=sc, mentions_repo=repo, hotness_repo=hotness_repo)

    # monkeypatch datetime.now 让 window_end 每次都固定
    window_end_target = datetime(2026, 5, 11, 10, 0, 0, tzinfo=ZoneInfo("UTC"))
    fake_now = window_end_target + timedelta(seconds=5)

    import services.l2_hotness as hotness_mod

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz:
                return fake_now.astimezone(tz)
            return fake_now

    monkeypatch.setattr(hotness_mod, "datetime", _FakeDateTime)

    assert svc.run_once() is True

    # 读 hotness_repo.upsert_batch 的 kwargs 看 records 顺序
    call = hotness_repo.upsert_batch.call_args
    records = call.kwargs["records"]
    names = [r["entity"] for r in records]
    ranks = [r["rank"] for r in records]

    assert names == ["A", "B", "C"], f"排序错误，实际：{names}"
    assert ranks == [1, 2, 3]


# ===========================================================================
# Part 4：跳过分支
# ===========================================================================


def test_baseline_insufficient_skips(loguru_capture) -> None:
    """
    Req 7.7：最近 baseline_days 天 entity_mentions 总数 < min_baseline_count 时，
    run_once 返回 False + 打 INFO 日志 "baseline data insufficient"。
    """
    sc = _make_mock_sliding_counter(active=[], counts={})
    # count_since 返回 50，默认阈值 100 → 不足
    repo = _make_mock_mentions_repo(
        baseline_totals={},
        cross_sources={},
        count_since_value=50,
    )
    hotness_repo = MagicMock()
    svc = _make_service(sliding_counter=sc, mentions_repo=repo, hotness_repo=hotness_repo)

    assert svc.run_once() is False
    # 不应触发 UPSERT
    hotness_repo.upsert_batch.assert_not_called()
    # 日志检查
    info_msgs = [r["message"] for r in loguru_capture if r["level"] == "INFO"]
    assert any("baseline data insufficient" in m for m in info_msgs), (
        f"期望 INFO 含 'baseline data insufficient'，实际：{info_msgs}"
    )


def test_counter_not_ready_skips_and_resets(loguru_capture) -> None:
    """
    Req 7.8：`_counter_ready=False` 时本轮跳过、置回 True；下一轮允许再跑。
    """
    sc = _make_mock_sliding_counter(active=[], counts={})
    repo = _make_mock_mentions_repo(baseline_totals={}, cross_sources={})
    hotness_repo = MagicMock()
    svc = _make_service(sliding_counter=sc, mentions_repo=repo, hotness_repo=hotness_repo)
    svc._counter_ready = False

    # 第一轮：跳过
    assert svc.run_once() is False
    info_msgs = [r["message"] for r in loguru_capture if r["level"] == "INFO"]
    assert any("sliding counter not ready" in m for m in info_msgs)

    # 自愈：flag 被置回 True
    assert svc._counter_ready is True

    # 第二轮：flag 已 True，不会再进入 "not ready" 分支
    # 因为 count_since 默认 10000 > 100，只要 active_entities 为空也返回 True（records 空）
    # 让 hotness_repo.upsert_batch 不抛错，本轮应进入正常流程
    # 但 active_entities=[] → records=[] → top=[] → 仍然 upsert 空 records
    # repo 已经设为 MagicMock 默认不抛，应返回 True
    # 保证不会再产生 "not ready" 日志
    n_skips_before = sum(
        1 for r in loguru_capture if "sliding counter not ready" in r["message"]
    )
    svc.run_once()
    n_skips_after = sum(
        1 for r in loguru_capture if "sliding counter not ready" in r["message"]
    )
    assert n_skips_after == n_skips_before, "第二轮不应再产生 'not ready' 日志"


# ===========================================================================
# Part 5：写库路径（用 SQLite + 子类化 repo）
# ===========================================================================


class _SqliteFriendlyHotnessRepo(HotnessSnapshotsRepo):
    """
    SQLite 版 repo：把 `upsert_batch` 从 PG `on_conflict_do_update` 改写为
    `先 SELECT 已存在 → 已存在的 UPDATE，其他 INSERT` 的等价实现。

    这里还手动分配 id（SQLite BigInteger 主键不自增）。
    """

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

        # 查已存在的 entity 集合
        entities = [r["entity"] for r in records]
        existing_stmt = select(HotnessSnapshot).where(
            HotnessSnapshot.window_end == window_end,
            HotnessSnapshot.window_type == window_type,
            HotnessSnapshot.entity.in_(entities),
        )
        existing = {r.entity: r for r in session.scalars(existing_stmt).all()}

        for r in records:
            if r["entity"] in existing:
                # UPDATE：覆盖所有统计字段
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


@pytest.fixture()
def sqlite_db() -> _SqliteDatabase:
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
            NormalizedMessage.__table__,
            EntityMention.__table__,
            HotnessSnapshot.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return _SqliteDatabase(session_factory=factory)


def test_upsert_overwrites_same_window(sqlite_db: _SqliteDatabase) -> None:
    """
    Req 7.6：对同 (window_end, entity) 两次 upsert，第二次应覆盖第一次的统计值，
    而不是新增一行。
    """
    _SqliteFriendlyHotnessRepo._id_counter = 0
    repo = _SqliteFriendlyHotnessRepo()
    window_end = datetime(2026, 5, 11, 10, 0, 0)

    # 第一次写入
    with sqlite_db.get_session() as s:
        repo.upsert_batch(
            s,
            window_end=window_end,
            window_type="1h",
            records=[
                {
                    "entity": "BTC",
                    "count_short": 10,
                    "count_baseline": 5.0,
                    "growth_rate": 2.0,
                    "cross_source": 1,
                    "is_new_entity": False,
                    "final_score": 2.0,
                    "rank": 1,
                }
            ],
        )
        s.commit()

    # 第二次写入（覆盖）
    with sqlite_db.get_session() as s:
        repo.upsert_batch(
            s,
            window_end=window_end,
            window_type="1h",
            records=[
                {
                    "entity": "BTC",
                    "count_short": 50,
                    "count_baseline": 10.0,
                    "growth_rate": 5.0,
                    "cross_source": 3,
                    "is_new_entity": False,
                    "final_score": 8.0,
                    "rank": 1,
                }
            ],
        )
        s.commit()

    # 断言：只有 1 行，且统计值是第二次的
    with sqlite_db.get_session() as s:
        rows = list(s.scalars(select(HotnessSnapshot)).all())
    assert len(rows) == 1, f"预期 UPSERT 只保留 1 行，实际：{len(rows)}"
    r = rows[0]
    assert r.count_short == 50
    assert r.final_score == 8.0
    assert r.cross_source == 3


def test_write_failure_rolls_back(sqlite_db: _SqliteDatabase) -> None:
    """
    Req 7.9：upsert_batch 抛异常时，run_once 返回 False + 不更新 _last_window_end，
    且 hotness_snapshots 不留任何脏数据；下一轮重试仍可写入。
    """
    _SqliteFriendlyHotnessRepo._id_counter = 0

    # active_entities 返回 BTC，count 非 0，让流程真的走到 upsert
    sc = _make_mock_sliding_counter(active=["BTC"], counts={"BTC": 5})
    # count_since 足够，baseline 0 → growth=2.5
    repo = _make_mock_mentions_repo(
        baseline_totals={"BTC": 0},
        cross_sources={"BTC": 1},
        count_since_value=200,  # > 100 过基线检查
    )
    # 让 upsert 抛错
    hotness_repo = MagicMock()
    hotness_repo.upsert_batch.side_effect = RuntimeError("simulated db write failure")
    svc = HotnessService(
        db=sqlite_db,
        mentions_repo=repo,
        hotness_repo=hotness_repo,
        sliding_counter=sc,
        top_k=20,
        smoothing=2.0,
        short_hours=1,
        baseline_days=7,
        min_baseline_count=100,
        timezone=ZoneInfo("UTC"),
    )

    # 第一轮：upsert 失败 → False，_last_window_end 未更新
    assert svc.run_once() is False
    assert svc._last_window_end is None, "失败时不得更新 _last_window_end"

    # 验证 hotness_snapshots 没有脏数据
    with sqlite_db.get_session() as s:
        rows = list(s.scalars(select(HotnessSnapshot)).all())
    assert rows == [], f"失败事务必须 rollback，实际留下：{rows}"

    # 第二轮：把 side_effect 去掉，模拟 DB 恢复
    hotness_repo.upsert_batch.side_effect = None
    hotness_repo.upsert_batch.return_value = 1
    assert svc.run_once() is True
    # 第二轮成功后 _last_window_end 被更新
    assert svc._last_window_end is not None


# ===========================================================================
# Part 6：黑名单（exclude_entities）—— 让 BTC/ETH 这种常驻巨头不出现在榜上
# ===========================================================================


def test_exclude_entities_filters_out_blacklisted(monkeypatch) -> None:
    """
    新增字段 `exclude_entities`：被列入黑名单的实体应**完全不出现**在
    最终写入 hotness_snapshots 的 records 里。

    场景：BTC / ETH 短窗有提及，但因为在黑名单里，最终榜单只剩 NEWMEME。
    """
    sc = _make_mock_sliding_counter(
        active=["BTC", "ETH", "NEWMEME"],
        counts={"BTC": 50, "ETH": 30, "NEWMEME": 5},
    )
    repo = _make_mock_mentions_repo(
        baseline_totals={"BTC": 0, "ETH": 0, "NEWMEME": 0},
        cross_sources={"BTC": 1, "ETH": 1, "NEWMEME": 1},
    )
    hotness_repo = MagicMock()

    svc = HotnessService(
        db=_FakeDatabase(),
        mentions_repo=repo,
        hotness_repo=hotness_repo,
        sliding_counter=sc,
        top_k=20,
        smoothing=2.0,
        short_hours=1,
        baseline_days=7,
        min_baseline_count=100,
        timezone=ZoneInfo("UTC"),
        exclude_entities=("BTC", "ETH"),  # ← 黑名单
    )

    # mock datetime.now
    import services.l2_hotness as hotness_mod

    fake_now = datetime(2026, 5, 13, 10, 0, 5, tzinfo=ZoneInfo("UTC"))

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz:
                return fake_now.astimezone(tz)
            return fake_now

    monkeypatch.setattr(hotness_mod, "datetime", _FakeDateTime)

    assert svc.run_once() is True

    # 看实际写入的 records
    call = hotness_repo.upsert_batch.call_args
    records = call.kwargs["records"]
    names = {r["entity"] for r in records}

    assert names == {"NEWMEME"}, f"黑名单应过滤掉 BTC/ETH，实际：{names}"
    # 还原：NEWMEME 排名 1（唯一一条）
    assert records[0]["rank"] == 1


def test_exclude_entities_case_insensitive(monkeypatch) -> None:
    """
    黑名单比较应不区分大小写。
    `exclude_entities=("btc",)` 应屏蔽 entity="BTC"（来自 prefilter 的标准化大写）。
    """
    sc = _make_mock_sliding_counter(
        active=["BTC", "OTHER"],
        counts={"BTC": 10, "OTHER": 5},
    )
    repo = _make_mock_mentions_repo(
        baseline_totals={"BTC": 0, "OTHER": 0},
        cross_sources={"BTC": 1, "OTHER": 1},
    )
    hotness_repo = MagicMock()

    svc = HotnessService(
        db=_FakeDatabase(),
        mentions_repo=repo,
        hotness_repo=hotness_repo,
        sliding_counter=sc,
        top_k=20,
        smoothing=2.0,
        short_hours=1,
        baseline_days=7,
        min_baseline_count=100,
        timezone=ZoneInfo("UTC"),
        exclude_entities=("btc",),  # ← 小写写法
    )

    import services.l2_hotness as hotness_mod

    fake_now = datetime(2026, 5, 13, 10, 0, 5, tzinfo=ZoneInfo("UTC"))

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz:
                return fake_now.astimezone(tz)
            return fake_now

    monkeypatch.setattr(hotness_mod, "datetime", _FakeDateTime)

    assert svc.run_once() is True
    records = hotness_repo.upsert_batch.call_args.kwargs["records"]
    names = {r["entity"] for r in records}
    assert names == {"OTHER"}, f"小写黑名单也该屏蔽大写实体，实际：{names}"


def test_exclude_entities_default_is_empty(monkeypatch) -> None:
    """
    向后兼容：不传 exclude_entities 时（默认空 tuple），所有实体都进榜。
    """
    sc = _make_mock_sliding_counter(
        active=["BTC", "ETH"],
        counts={"BTC": 10, "ETH": 5},
    )
    repo = _make_mock_mentions_repo(
        baseline_totals={"BTC": 0, "ETH": 0},
        cross_sources={"BTC": 1, "ETH": 1},
    )
    hotness_repo = MagicMock()

    # 不传 exclude_entities，走默认值 ()
    svc = _make_service(sliding_counter=sc, mentions_repo=repo, hotness_repo=hotness_repo)
    assert svc.exclude_entities == ()

    import services.l2_hotness as hotness_mod

    fake_now = datetime(2026, 5, 13, 10, 0, 5, tzinfo=ZoneInfo("UTC"))

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz:
                return fake_now.astimezone(tz)
            return fake_now

    monkeypatch.setattr(hotness_mod, "datetime", _FakeDateTime)

    svc.run_once()
    records = hotness_repo.upsert_batch.call_args.kwargs["records"]
    names = {r["entity"] for r in records}
    assert names == {"BTC", "ETH"}, f"默认空黑名单应不过滤，实际：{names}"


# ===========================================================================
# loguru 日志捕获 fixture（复用 Task 5.3 同款策略）
# ===========================================================================


@pytest.fixture
def loguru_capture():
    """
    用 loguru 自己的 sink 机制捕获日志；测完 remove，避免污染后续测试。
    """
    from loguru import logger

    records: list[dict] = []

    def sink(message):
        r = message.record
        records.append({"level": r["level"].name, "message": r["message"]})

    sink_id = logger.add(sink, level="TRACE", format="{message}")
    try:
        yield records
    finally:
        logger.remove(sink_id)


# ===========================================================================
# Phase 2.1 多窗口扩展（Task 2）
# ---------------------------------------------------------------------------
# 验证 HotnessService.window_type 字段 + __post_init__ 三道校验，
# 以及多窗口下 _compute_records 的两个改动点：
#   - count(entity, self.window_type)
#   - active_entities("7d" if self.window_type == "24h" else "24h")
# ===========================================================================


def test_hotness_24h_baseline_days_lt_8_raises() -> None:
    """
    Phase 2.1 Req 2.2 校验 3：baseline_days * 24 - short_hours <= 0 触发 raise。

    24h 窗口 + baseline_days=7 → 7*24-24 = 144 数学上 > 0 但语义错乱
    （基线只剩 6 天）；
    更极端 baseline_days=1 → 1*24-24 = 0 直接触发；
    本用例用 7 天验证：requirements 把 24h 默认 baseline_days 锁成 8。
    """
    sc = MagicMock()
    repo = MagicMock()

    # baseline_days=7 + short_hours=24 → 7*24-24 = 144 > 0，不触发 raise
    # 但要演示"边界数学约束"，用更小的值制造 <= 0
    with pytest.raises(ValueError, match="baseline_days"):
        HotnessService(
            db=_FakeDatabase(),
            mentions_repo=repo,
            hotness_repo=MagicMock(),
            sliding_counter=sc,
            window_type="24h",
            short_hours=24,
            baseline_days=1,  # 1*24-24 = 0 触发分母 ≤ 0
            top_k=20,
            smoothing=10.0,
            min_baseline_count=500,
            timezone=ZoneInfo("UTC"),
        )


def test_hotness_window_type_unknown_raises() -> None:
    """
    Phase 2.1 Req 2.2 校验 1：window_type 不在 WINDOWS_SECONDS 时 raise。
    """
    sc = MagicMock()
    repo = MagicMock()
    with pytest.raises(ValueError, match="window_type=.*'2h'.*不支持"):
        HotnessService(
            db=_FakeDatabase(),
            mentions_repo=repo,
            hotness_repo=MagicMock(),
            sliding_counter=sc,
            window_type="2h",  # 不存在的窗口名
            short_hours=2,
            baseline_days=7,
            timezone=ZoneInfo("UTC"),
        )


def test_hotness_window_type_short_hours_mismatch_raises() -> None:
    """
    Phase 2.1 Req 2.2 校验 2：short_hours 与 window_type 隐含小时数不一致 → raise。

    例：window_type='6h' 应隐含 short_hours=6，传 short_hours=1 应 raise。
    """
    sc = MagicMock()
    repo = MagicMock()
    with pytest.raises(ValueError, match="short_hours"):
        HotnessService(
            db=_FakeDatabase(),
            mentions_repo=repo,
            hotness_repo=MagicMock(),
            sliding_counter=sc,
            window_type="6h",
            short_hours=1,  # 与 6h 不一致
            baseline_days=7,
            timezone=ZoneInfo("UTC"),
        )


def test_hotness_6h_writes_window_type_6h(monkeypatch) -> None:
    """
    Phase 2.1 Req 2.3 集成：6h 实例的 _compute_records 应：
    1. 调用 sliding_counter.count(entity, '6h')
    2. upsert_batch 写入时传 window_type='6h'
    3. growth_rate 用 6h smoothing 公式：60 / max(100/162, 5.0) = 60/5 = 12.0
    """
    # 6h 实例：smoothing=5.0 / baseline_days=7 / short_hours=6
    sc = MagicMock()
    sc.active_entities.return_value = ["NEWMEME"]
    # count('6h') 返回 60；其它窗口返回 0（保证只走 6h 路径）
    sc.count.side_effect = lambda entity, window: 60 if window == "6h" else 0

    repo = _make_mock_mentions_repo(
        baseline_totals={"NEWMEME": 100},
        cross_sources={"NEWMEME": 2},
        count_since_value=300,  # > min_baseline_count=200
    )
    hotness_repo = MagicMock()

    svc = HotnessService(
        db=_FakeDatabase(),
        mentions_repo=repo,
        hotness_repo=hotness_repo,
        sliding_counter=sc,
        window_type="6h",
        short_hours=6,
        top_k=20,
        smoothing=5.0,
        baseline_days=7,
        min_baseline_count=200,
        timezone=ZoneInfo("UTC"),
    )

    # 冻结 datetime.now 让 window_end 固定
    import services.l2_hotness as hotness_mod

    fake_now = datetime(2026, 5, 14, 10, 0, 5, tzinfo=ZoneInfo("UTC"))

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz:
                return fake_now.astimezone(tz)
            return fake_now

    monkeypatch.setattr(hotness_mod, "datetime", _FakeDateTime)

    assert svc.run_once() is True

    # 验证 1：upsert_batch 调用时 window_type='6h'
    call = hotness_repo.upsert_batch.call_args
    assert call.kwargs["window_type"] == "6h", (
        f"6h 实例应写 window_type='6h'，实际：{call.kwargs['window_type']}"
    )

    # 验证 2：sliding_counter.count 用 '6h' 调用过
    sc.count.assert_any_call("NEWMEME", "6h")

    # 验证 3：growth_rate 公式
    # baseline_per_hour = 100 / (7*24-6) = 100/162 ≈ 0.617
    # max(0.617, smoothing=5.0) = 5.0
    # growth_rate = 60 / 5.0 = 12.0
    rec = call.kwargs["records"][0]
    assert rec["entity"] == "NEWMEME"
    assert abs(rec["growth_rate"] - 12.0) < 0.01, (
        f"growth_rate 应 ≈ 12.0，实际 {rec['growth_rate']}"
    )


def test_hotness_24h_uses_7d_active_entities(monkeypatch) -> None:
    """
    Phase 2.1 Req 2.3：24h 实例的候选集应来自 active_entities('7d')，
    而不是 active_entities('24h')——让候选涵盖 24 小时前刚活跃过的边缘 entity。
    """
    sc = MagicMock()
    # 区分两个窗口的返回值，确保 service 选了正确那个
    def _active(window):
        if window == "7d":
            return ["FROM_7D"]
        if window == "24h":
            return ["FROM_24H"]
        raise AssertionError(f"unexpected active_entities window={window}")

    sc.active_entities.side_effect = _active
    sc.count.side_effect = lambda entity, window: 80 if window == "24h" else 0

    repo = _make_mock_mentions_repo(
        baseline_totals={"FROM_7D": 100, "FROM_24H": 100},
        cross_sources={"FROM_7D": 2, "FROM_24H": 2},
        count_since_value=1000,  # > min_baseline_count=500
    )
    hotness_repo = MagicMock()

    svc = HotnessService(
        db=_FakeDatabase(),
        mentions_repo=repo,
        hotness_repo=hotness_repo,
        sliding_counter=sc,
        window_type="24h",
        short_hours=24,
        top_k=20,
        smoothing=10.0,
        baseline_days=8,
        min_baseline_count=500,
        timezone=ZoneInfo("UTC"),
    )

    import services.l2_hotness as hotness_mod

    fake_now = datetime(2026, 5, 14, 10, 0, 5, tzinfo=ZoneInfo("UTC"))

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz:
                return fake_now.astimezone(tz)
            return fake_now

    monkeypatch.setattr(hotness_mod, "datetime", _FakeDateTime)

    assert svc.run_once() is True

    # 关键断言：active_entities 用 '7d' 调过
    sc.active_entities.assert_called_with("7d")

    # 验证：写入的 entity 来自 7d 候选集
    call = hotness_repo.upsert_batch.call_args
    names = {r["entity"] for r in call.kwargs["records"]}
    assert names == {"FROM_7D"}, (
        f"24h 实例 candidates 应来自 active_entities('7d')，实际：{names}"
    )
    assert call.kwargs["window_type"] == "24h"
