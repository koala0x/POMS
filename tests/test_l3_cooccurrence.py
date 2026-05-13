from __future__ import annotations

"""
CooccurrenceService 单元 + 集成测试（Phase 2.5 Task 2.4 + 3.2）。

12 个用例分两类：
- **公式 / 候选生成 / 跳过分支**（用例 1~10）：用 Mock 替换 sliding_counter +
  mentions_repo + cooccur_repo + db，零 DB 依赖，断言精确
- **is_new_pair 判定**（用例 11、12）：mock 出 mentions_repo.count_pair_cooccur
  按场景返回，验证 service 层短路逻辑

测试矩阵对照（requirements.md Req 7.1 / 7.2 + design.md §5）：
 1. test_pairs_combination_correctness
 2. test_pairs_canonical_order
 3. test_pmi_formula
 4. test_pmi_independent_pair_low
 5. test_pmi_correlated_pair_high
 6. test_skips_when_data_sparse
 7. test_skips_when_window_unchanged
 8. test_min_cooccur_count_filter
 9. test_min_pmi_filter
10. test_upsert_idempotent（同窗口跑 2 次，rowcount 不暴涨）
11. test_is_new_pair_baseline_zero_short_three
12. test_is_new_pair_baseline_one_short_ten
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterator
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import math
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from db.models import Base, EntityCooccurrence, EntityMention, NormalizedMessage
from db.repositories.cooccurrence_repo import CooccurrenceRepo
from services.l3_cooccurrence import CooccurrenceService, _pmi


# ===========================================================================
# 辅助：Fake Database / mention_entities 注入
# ===========================================================================


@dataclass
class _FakeSession:
    """
    最小 Session mock：execute(stmt) 返回 fake_rows 的迭代器；
    其它属性透传给 _real_session（如果有的话）。

    用途：让 _compute_pairs 内部 `for row in session.execute(stmt):` 能拿到
    我们手工捏的 (msg_id, entity) 列表，绕开真实 PG 查询。
    """

    fake_rows: list[tuple[int, str]] = field(default_factory=list)

    def execute(self, stmt):
        return iter([(mid, ent) for mid, ent in self.fake_rows])

    # cooccur_repo.upsert_batch（即便 mock）会被 service 在 with 块里调用，
    # 默认 MagicMock 不抛错就 OK；下面三个方法是 service 真把 with 块当成
    # 写事务跑时的最低限度配合（commit/rollback/no-op，避免 AttributeError）
    def commit(self) -> None:  # pragma: no cover - 简单空实现
        pass

    def rollback(self) -> None:  # pragma: no cover
        pass

    def add(self, instance) -> None:  # pragma: no cover
        pass

    def flush(self) -> None:  # pragma: no cover
        pass


@dataclass
class _FakeDatabase:
    """
    把 mention_entities 注入 _FakeSession.fake_rows；
    每次 get_session() 都返回新的 session（保证 fake_rows 不被消费完）。
    """

    fake_rows: list[tuple[int, str]] = field(default_factory=list)

    @contextmanager
    def get_session(self):
        yield _FakeSession(fake_rows=list(self.fake_rows))


def _make_mock_sliding_counter(active: list[str]) -> MagicMock:
    """SlidingCounter 假件：active_entities('24h') 返回 active。"""
    sc = MagicMock()
    sc.active_entities.return_value = active
    return sc


def _make_mock_mentions_repo(
    *,
    distinct_msgs: int = 200,
    pair_baseline: dict[tuple[str, str], int] | None = None,
) -> MagicMock:
    """
    mentions_repo 假件：
    - count_distinct_msgs_since 返回 distinct_msgs
    - count_pair_cooccur 按 (a, b) 字典序查 pair_baseline，缺省返回 0
    """
    repo = MagicMock()
    repo.count_distinct_msgs_since.return_value = distinct_msgs

    def _baseline(_session, a, b, *, start, end):
        key = tuple(sorted((a, b)))
        return (pair_baseline or {}).get(key, 0)

    repo.count_pair_cooccur.side_effect = _baseline
    return repo


def _make_service(
    *,
    sliding_counter,
    mentions_repo,
    cooccur_repo=None,
    db=None,
    **overrides,
) -> CooccurrenceService:
    """统一入口：构造 CooccurrenceService。"""
    return CooccurrenceService(
        db=db or _FakeDatabase(),
        mentions_repo=mentions_repo,
        cooccur_repo=cooccur_repo or MagicMock(),
        sliding_counter=sliding_counter,
        window_type=overrides.get("window_type", "24h"),
        top_pairs=overrides.get("top_pairs", 100),
        min_cooccur_count=overrides.get("min_cooccur_count", 3),
        min_pmi=overrides.get("min_pmi", 1.0),
        min_window_msgs=overrides.get("min_window_msgs", 50),
        timezone=overrides.get("timezone", ZoneInfo("UTC")),
    )


def _freeze_now(monkeypatch, fake_now: datetime) -> None:
    """
    把 services.l3_cooccurrence 模块里 import 的 `datetime` 替换成
    一个 now() 永远返回 fake_now 的 fake 类。
    """
    import services.l3_cooccurrence as cooccur_mod

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz:
                return fake_now.astimezone(tz)
            return fake_now

    monkeypatch.setattr(cooccur_mod, "datetime", _FakeDateTime)


# ===========================================================================
# 用例 1-2：候选 pair 生成（_compute_pairs 直接测）
# ===========================================================================


def test_pairs_combination_correctness() -> None:
    """
    Req 2.4：一条消息含 [A, B, C] 3 个实体应生成 3 对：(A,B) / (A,C) / (B,C)。
    """
    sc = _make_mock_sliding_counter(active=["A", "B", "C"])
    repo = _make_mock_mentions_repo(distinct_msgs=200)
    db = _FakeDatabase(fake_rows=[(1, "A"), (1, "B"), (1, "C")])
    svc = _make_service(sliding_counter=sc, mentions_repo=repo, db=db)

    pairs = svc._compute_pairs(
        window_end=datetime(2026, 5, 14, 10, 0, tzinfo=ZoneInfo("UTC")),
        short_start=datetime(2026, 5, 13, 10, 0, tzinfo=ZoneInfo("UTC")),
        window_msgs=200,
    )

    pair_keys = {(p["entity_a"], p["entity_b"]) for p in pairs}
    assert pair_keys == {("A", "B"), ("A", "C"), ("B", "C")}
    # 每对在唯一一条消息共现一次
    counts = {(p["entity_a"], p["entity_b"]): p["cooccur_count"] for p in pairs}
    assert counts == {("A", "B"): 1, ("A", "C"): 1, ("B", "C"): 1}


def test_pairs_canonical_order() -> None:
    """
    Req 2.4：DB 里乱序输入 ["ETH", "BTC"]，输出对必然 entity_a="BTC" entity_b="ETH"。
    """
    sc = _make_mock_sliding_counter(active=["BTC", "ETH"])
    repo = _make_mock_mentions_repo(distinct_msgs=200)
    db = _FakeDatabase(fake_rows=[(7, "ETH"), (7, "BTC")])  # 输入乱序
    svc = _make_service(sliding_counter=sc, mentions_repo=repo, db=db)

    pairs = svc._compute_pairs(
        window_end=datetime(2026, 5, 14, 10, 0, tzinfo=ZoneInfo("UTC")),
        short_start=datetime(2026, 5, 13, 10, 0, tzinfo=ZoneInfo("UTC")),
        window_msgs=200,
    )

    assert len(pairs) == 1
    p = pairs[0]
    # 字典序 BTC < ETH
    assert p["entity_a"] == "BTC" and p["entity_b"] == "ETH"


# ===========================================================================
# 用例 3-5：PMI 公式测试（_pmi 模块函数直接测）
# ===========================================================================


def test_pmi_formula() -> None:
    """
    Req 3.1：PMI(a, b) = log( cooccur × N / (count_a × count_b) )

    给定 cooccur=12 / count_a=25 / count_b=18 / N=200：
        expected = 25 * 18 / 200 = 2.25
        PMI = log(12 / 2.25) = log(5.333...) ≈ 1.674
    （与 design.md §3.3 数值表对齐）
    """
    pmi = _pmi(cooccur=12, count_a=25, count_b=18, N=200)
    assert abs(pmi - math.log(12 * 200 / (25 * 18))) < 1e-9
    # 数值核对（design.md §3.3 给的 1.67）
    assert abs(pmi - 1.674) < 0.01


def test_pmi_independent_pair_low() -> None:
    """
    两个高频但独立的实体（如 BTC + USDT 巨头）PMI 应接近 0。

    构造：count_a = count_b = 100，N = 200，cooccur = 50
    expected = 100 * 100 / 200 = 50；cooccur / expected = 1.0；log(1.0) = 0.0
    """
    pmi = _pmi(cooccur=50, count_a=100, count_b=100, N=200)
    assert abs(pmi) < 1e-9, f"独立预期下 PMI 应 = 0，实际 {pmi}"


def test_pmi_correlated_pair_high() -> None:
    """
    两个低频但常一起出现的实体（叙事候选信号）PMI 应显著 > 0。

    构造：count_a = count_b = 10，N = 200，cooccur = 8
    expected = 10 * 10 / 200 = 0.5；cooccur / expected = 16；log(16) ≈ 2.77
    """
    pmi = _pmi(cooccur=8, count_a=10, count_b=10, N=200)
    assert abs(pmi - math.log(16)) < 1e-9
    assert pmi > 2.5, f"低频共振 PMI 应显著 > 2.5，实际 {pmi}"


# ===========================================================================
# 用例 6：数据稀疏跳过
# ===========================================================================


def test_skips_when_data_sparse(loguru_capture) -> None:
    """
    Req 2.3 + 8.3：count_distinct_msgs_since < min_window_msgs 时
    run_once 返回 False，不调 upsert，且打 INFO "data sparse" 日志。
    """
    sc = _make_mock_sliding_counter(active=[])
    repo = _make_mock_mentions_repo(distinct_msgs=10)  # < 50 阈值
    cooccur_repo = MagicMock()
    svc = _make_service(
        sliding_counter=sc, mentions_repo=repo, cooccur_repo=cooccur_repo
    )

    assert svc.run_once() is False
    cooccur_repo.upsert_batch.assert_not_called()
    info_msgs = [r["message"] for r in loguru_capture if r["level"] == "INFO"]
    assert any("data sparse" in m for m in info_msgs), (
        f"期望 INFO 含 'data sparse'，实际：{info_msgs}"
    )


# ===========================================================================
# 用例 7：同一窗口不重复处理
# ===========================================================================


def test_skips_when_window_unchanged(monkeypatch, loguru_capture) -> None:
    """
    Req 2.3：第二次 run_once 同一 align_to_quarter → 返回 False，不调 upsert，
    且打 INFO "window unchanged" 日志。
    """
    sc = _make_mock_sliding_counter(active=["A", "B"])
    repo = _make_mock_mentions_repo(distinct_msgs=200)
    cooccur_repo = MagicMock()
    db = _FakeDatabase(fake_rows=[(1, "A"), (1, "B")])
    svc = _make_service(
        sliding_counter=sc, mentions_repo=repo, cooccur_repo=cooccur_repo, db=db
    )

    # 第一轮：写入成功，_last_window_end 更新为 10:15
    _freeze_now(
        monkeypatch,
        datetime(2026, 5, 13, 10, 16, tzinfo=ZoneInfo("UTC")),
    )
    # cooccur_count=1 不达阈值（min_cooccur_count=3），run_once 返回 False
    # 但 _last_window_end 仍会被记录（避免下一轮反复扫空集，与 design 一致）
    assert svc.run_once() is False
    assert svc._last_window_end == datetime(
        2026, 5, 13, 10, 15, tzinfo=ZoneInfo("UTC")
    )

    cooccur_repo.upsert_batch.reset_mock()

    # 第二轮：仍在 10:28（对齐到同一 10:15），跳过
    _freeze_now(
        monkeypatch,
        datetime(2026, 5, 13, 10, 28, tzinfo=ZoneInfo("UTC")),
    )
    assert svc.run_once() is False
    cooccur_repo.upsert_batch.assert_not_called()

    info_msgs = [r["message"] for r in loguru_capture if r["level"] == "INFO"]
    assert any("window unchanged" in m for m in info_msgs), (
        f"期望 INFO 含 'window unchanged'，实际：{info_msgs}"
    )


# ===========================================================================
# 用例 8-9：min_cooccur_count / min_pmi 过滤
# ===========================================================================


def test_min_cooccur_count_filter(monkeypatch) -> None:
    """
    Req 2.3：cooccur_count=1~2 的对不写库；只有 cooccur ≥ 3 才进 Top-K。

    构造：A+B 共现 4 次（达标），A+C 共现 2 次（不达标），B+C 共现 1 次（不达标）。
    最终 upsert_batch 只收到 (A, B) 一对。
    """
    sc = _make_mock_sliding_counter(active=["A", "B", "C"])
    repo = _make_mock_mentions_repo(distinct_msgs=200)
    cooccur_repo = MagicMock()
    # 4 条消息：A+B / A+B / A+B / A+B+C （让 (A,B)=4, (A,C)=1, (B,C)=1）
    db = _FakeDatabase(
        fake_rows=[
            (1, "A"), (1, "B"),
            (2, "A"), (2, "B"),
            (3, "A"), (3, "B"),
            (4, "A"), (4, "B"), (4, "C"),
        ]
    )
    svc = _make_service(
        sliding_counter=sc,
        mentions_repo=repo,
        cooccur_repo=cooccur_repo,
        db=db,
        # 让 PMI 都过 1.0（candidate count 给得低，cooccur 高）
        min_pmi=0.0,  # 关掉 PMI 过滤，只测 min_cooccur_count
    )
    _freeze_now(monkeypatch, datetime(2026, 5, 13, 10, 16, tzinfo=ZoneInfo("UTC")))

    assert svc.run_once() is True
    call = cooccur_repo.upsert_batch.call_args
    written = {(p["entity_a"], p["entity_b"]) for p in call.kwargs["pairs"]}
    assert written == {("A", "B")}, f"只 (A,B) 达 min_cooccur=3，实际：{written}"


def test_min_pmi_filter(monkeypatch) -> None:
    """
    Req 2.3：PMI < min_pmi 的对不写库。

    构造：A+B 高频但独立（PMI ≈ 0），不应写库；
        C+D 低频共振（PMI 显著 > 0），应写库。
    具体：N=200 / count_a=count_b=100 / cooccur=50（PMI=0）—— A+B
        N=200 / count_c=count_d=10 / cooccur=8（PMI≈2.77）—— C+D
    """
    sc = _make_mock_sliding_counter(active=["A", "B", "C", "D"])
    repo = _make_mock_mentions_repo(distinct_msgs=200)
    cooccur_repo = MagicMock()

    # 50 条 A+B 共现（让 count_a=count_b=100, cooccur=50, N=200 → 但 N 由
    # window_msgs 给定，不依赖 fake_rows 长度；所以这里只关心 fake_rows
    # 提供的 msg 集合本身）
    fake_rows: list[tuple[int, str]] = []
    # 50 条只含 A 的 message（让 count_a = 100，但 count 通过 msg_entities
    # 在 _compute_pairs 内累加；A+B 共现 50 次需要 50 条 A+B 消息）
    # 为 PMI 准确：count_a 应 ≈ count_b ≈ 100，cooccur=50。让 50 条 A+B 共现，
    # 50 条只 A，50 条只 B → count_a=100, count_b=100, cooccur=50
    msg_id = 0
    for _ in range(50):
        msg_id += 1
        fake_rows += [(msg_id, "A"), (msg_id, "B")]
    for _ in range(50):
        msg_id += 1
        fake_rows.append((msg_id, "A"))
    for _ in range(50):
        msg_id += 1
        fake_rows.append((msg_id, "B"))
    # C+D：让 count_c=count_d=10, cooccur=8 → PMI ≈ 2.77
    for _ in range(8):
        msg_id += 1
        fake_rows += [(msg_id, "C"), (msg_id, "D")]
    for _ in range(2):
        msg_id += 1
        fake_rows.append((msg_id, "C"))
    for _ in range(2):
        msg_id += 1
        fake_rows.append((msg_id, "D"))
    db = _FakeDatabase(fake_rows=fake_rows)

    svc = _make_service(
        sliding_counter=sc,
        mentions_repo=repo,
        cooccur_repo=cooccur_repo,
        db=db,
        min_cooccur_count=3,
        min_pmi=1.0,
    )
    _freeze_now(monkeypatch, datetime(2026, 5, 13, 10, 16, tzinfo=ZoneInfo("UTC")))

    assert svc.run_once() is True
    call = cooccur_repo.upsert_batch.call_args
    written = {(p["entity_a"], p["entity_b"]) for p in call.kwargs["pairs"]}
    assert ("C", "D") in written, f"C+D PMI≈2.77 应写库，实际：{written}"
    assert ("A", "B") not in written, f"A+B PMI≈0 应被过滤，实际：{written}"


# ===========================================================================
# 用例 10：UPSERT 幂等（用 SQLite + 子类化 repo）
# ===========================================================================


class _SqliteFriendlyCooccurRepo(CooccurrenceRepo):
    """
    SQLite 版 repo：把 PG `on_conflict_do_update` 换成"先 SELECT 已存在 →
    UPDATE / INSERT"等价实现。结构与 test_l2_hotness 同款。
    """

    _id_counter: int = 0

    def upsert_batch(
        self,
        session: Session,
        *,
        window_end: datetime,
        window_type: str,
        pairs: list[dict],
    ) -> int:
        if not pairs:
            return 0

        # 构造 (entity_a, entity_b) 集合查已存在的行
        keys = [(p["entity_a"], p["entity_b"]) for p in pairs]
        existing_stmt = select(EntityCooccurrence).where(
            EntityCooccurrence.window_end == window_end,
            EntityCooccurrence.window_type == window_type,
        )
        existing_all = list(session.scalars(existing_stmt).all())
        existing_map = {(r.entity_a, r.entity_b): r for r in existing_all}

        for p in pairs:
            key = (p["entity_a"], p["entity_b"])
            if key in existing_map:
                row = existing_map[key]
                row.cooccur_count = p["cooccur_count"]
                row.pmi = p["pmi"]
                row.is_new_pair = p.get("is_new_pair", False)
            else:
                type(self)._id_counter += 1
                session.add(
                    EntityCooccurrence(
                        id=type(self)._id_counter,
                        window_end=window_end,
                        window_type=window_type,
                        **p,
                    )
                )
        session.flush()
        return len(pairs)


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
            EntityCooccurrence.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return _SqliteDatabase(session_factory=factory)


def test_upsert_idempotent(sqlite_db: _SqliteDatabase) -> None:
    """
    Req 2.3 + 8.x：对同 (entity_a, entity_b, window_end, window_type) 两次
    upsert，第二次应覆盖，行数不增。
    """
    _SqliteFriendlyCooccurRepo._id_counter = 0
    repo = _SqliteFriendlyCooccurRepo()
    window_end = datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc)

    # 第一次写入
    with sqlite_db.get_session() as s:
        repo.upsert_batch(
            s,
            window_end=window_end,
            window_type="24h",
            pairs=[
                {
                    "entity_a": "EIGEN",
                    "entity_b": "ETHFI",
                    "cooccur_count": 5,
                    "pmi": 2.5,
                    "is_new_pair": True,
                }
            ],
        )
        s.commit()

    # 第二次写入（同 pair，更新值）
    with sqlite_db.get_session() as s:
        repo.upsert_batch(
            s,
            window_end=window_end,
            window_type="24h",
            pairs=[
                {
                    "entity_a": "EIGEN",
                    "entity_b": "ETHFI",
                    "cooccur_count": 12,
                    "pmi": 3.1,
                    "is_new_pair": False,
                }
            ],
        )
        s.commit()

    # 断言：表里仍然只有 1 行，且 cooccur_count / pmi / is_new_pair 都是第二次的值
    with sqlite_db.get_session() as s:
        rows = list(s.scalars(select(EntityCooccurrence)).all())
    assert len(rows) == 1, f"UPSERT 应覆盖，实际行数：{len(rows)}"
    r = rows[0]
    assert r.cooccur_count == 12
    assert abs(r.pmi - 3.1) < 1e-9
    assert r.is_new_pair is False


# ===========================================================================
# 用例 11-12：is_new_pair 检测
# ===========================================================================


def test_is_new_pair_baseline_zero_short_three() -> None:
    """
    Req 3.3：baseline 期 0 次共现 + 当前短窗 cooccur_count=3 → True。
    """
    sc = _make_mock_sliding_counter(active=["EIGEN", "ETHFI"])
    repo = _make_mock_mentions_repo(
        distinct_msgs=200,
        pair_baseline={("EIGEN", "ETHFI"): 0},
    )
    svc = _make_service(sliding_counter=sc, mentions_repo=repo)

    is_new = svc._is_new_pair(
        "EIGEN",
        "ETHFI",
        baseline_start=datetime(2026, 5, 6, 10, 0, tzinfo=ZoneInfo("UTC")),
        short_start=datetime(2026, 5, 13, 10, 0, tzinfo=ZoneInfo("UTC")),
        cooccur_count=3,
    )
    assert is_new is True


def test_is_new_pair_baseline_one_short_ten() -> None:
    """
    Req 3.3：baseline 期 1 次共现 → False（即便 cooccur_count=10，已不"新"了）。
    """
    sc = _make_mock_sliding_counter(active=["EIGEN", "ETHFI"])
    repo = _make_mock_mentions_repo(
        distinct_msgs=200,
        pair_baseline={("EIGEN", "ETHFI"): 1},
    )
    svc = _make_service(sliding_counter=sc, mentions_repo=repo)

    is_new = svc._is_new_pair(
        "EIGEN",
        "ETHFI",
        baseline_start=datetime(2026, 5, 6, 10, 0, tzinfo=ZoneInfo("UTC")),
        short_start=datetime(2026, 5, 13, 10, 0, tzinfo=ZoneInfo("UTC")),
        cooccur_count=10,
    )
    assert is_new is False


# 短路保护：cooccur_count < 3 时 _is_new_pair 直接返回 False（不查 DB）
def test_is_new_pair_short_circuit_below_min() -> None:
    """
    Req 3.3：cooccur_count < 3 时短路返回 False，且不调用 mentions_repo。
    """
    sc = _make_mock_sliding_counter(active=["A", "B"])
    repo = _make_mock_mentions_repo(distinct_msgs=200)
    svc = _make_service(sliding_counter=sc, mentions_repo=repo)

    is_new = svc._is_new_pair(
        "A",
        "B",
        baseline_start=datetime(2026, 5, 6, 10, 0, tzinfo=ZoneInfo("UTC")),
        short_start=datetime(2026, 5, 13, 10, 0, tzinfo=ZoneInfo("UTC")),
        cooccur_count=2,
    )
    assert is_new is False
    repo.count_pair_cooccur.assert_not_called()


# ===========================================================================
# loguru 日志捕获 fixture（与 test_l2_hotness 同款）
# ===========================================================================


@pytest.fixture
def loguru_capture():
    """用 loguru 自己的 sink 机制捕获日志；测完 remove。"""
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
