from __future__ import annotations

"""
SlidingCounter 单元测试（Task 5.3，对应 requirements.md Req 6.1~6.7）。

测试策略：
- 核心 API（add / count / active_entities）：
  - 用 `monkeypatch` 替换 `services.l2_sliding_counter.time.time`，
    强制返回固定"现在时刻"，让 cutoff 计算完全确定
  - 不依赖真实的 time 走动，测试可重复运行
- backfill（情况 A/B/C/D）：
  - 用 loguru 自带的 `logger.add(sink)` 注入一个内存 sink，
    断言日志级别（INFO / WARNING / ERROR）和关键词
  - 用 FakeDatabase + FakeSession mock 掉真实 DB，按需注入"一个 chunk
    消耗多少秒"来制造不同耗时场景
  - 不跑真实 SQLAlchemy，避免把测试跑得慢 + 减少对 SQLite 兼容性的依赖

覆盖 9 个用例：
- test_add_and_count_same_window        —— Req 6.1, 6.3
- test_count_different_windows          —— Req 6.2（四窗口独立）
- test_expired_entries_lazily_cleaned   —— Req 6.4（惰性清理）
- test_active_entities                  —— Req 6.6
- test_unknown_window_raises            —— 接口防御
- test_backfill_fast_success_info_log   —— Req 6.7 情况 A
- test_backfill_slow_success_warns      —— Req 6.7 情况 B
- test_backfill_hard_timeout            —— Req 6.7 情况 C
- test_backfill_db_exception            —— Req 6.7 情况 D
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterator

import pytest
from loguru import logger

from services import l2_sliding_counter as sc_module
from services.l2_sliding_counter import SlidingCounter


# ===========================================================================
# Part 1：核心 API 测试（monkeypatch time.time 保证确定性）
# ===========================================================================


def test_add_and_count_same_window(monkeypatch) -> None:
    """
    Req 6.1 + 6.3：同一实体 add 多次，count 返回的是窗口内的条数。
    """
    sc = SlidingCounter()
    # 冻结"现在时刻"到一个整点，避免 time 漂移影响断言
    fixed_now = 1_700_000_000.0
    monkeypatch.setattr(sc_module.time, "time", lambda: fixed_now)

    # 都在 1h 窗口内（距 fixed_now < 3600 秒）
    sc.add("BTC", fixed_now - 10)
    sc.add("BTC", fixed_now - 20)
    sc.add("BTC", fixed_now - 30)

    assert sc.count("BTC", "1h") == 3


def test_count_different_windows(monkeypatch) -> None:
    """
    Req 6.2：同一次 add 会被记入所有四个窗口；短窗在范围内的条目也在长窗内。
    """
    sc = SlidingCounter()
    fixed_now = 1_700_000_000.0
    monkeypatch.setattr(sc_module.time, "time", lambda: fixed_now)

    sc.add("ETH", fixed_now - 5)  # 距 now 5 秒，四个窗口都在内

    assert sc.count("ETH", "15min") == 1
    assert sc.count("ETH", "1h") == 1
    assert sc.count("ETH", "24h") == 1
    assert sc.count("ETH", "7d") == 1


def test_expired_entries_lazily_cleaned(monkeypatch) -> None:
    """
    Req 6.4：count 调用时会把窗口外的 ts 从 deque 左端 popleft。

    场景：同一个 entity 两次 add，一次在 1h 前，一次在 1h 内；
    对 1h 窗口做 count 应返回 1（老的被清理了），对 24h 窗口 count 仍返回 2。
    """
    sc = SlidingCounter()
    fixed_now = 1_700_000_000.0
    monkeypatch.setattr(sc_module.time, "time", lambda: fixed_now)

    sc.add("SOL", fixed_now - 7200)  # 2 小时前（在 1h 外，在 24h 内）
    sc.add("SOL", fixed_now - 60)    # 1 分钟前（全窗口内）

    # 1h 窗口：只剩 1 条
    assert sc.count("SOL", "1h") == 1
    # 24h 窗口：两条都在
    assert sc.count("SOL", "24h") == 2

    # 验证 deque 确实被清理了——底层 _store['1h']['SOL'] 只剩一个元素
    assert len(sc._store["1h"]["SOL"]) == 1


def test_active_entities(monkeypatch) -> None:
    """
    Req 6.6：active_entities('24h') 只返回在 24h 内有活动的实体，
    超过 24h 未再提及的实体不出现。
    """
    sc = SlidingCounter()
    fixed_now = 1_700_000_000.0
    monkeypatch.setattr(sc_module.time, "time", lambda: fixed_now)

    sc.add("BTC", fixed_now - 60)        # 1 分钟前 → 24h 内
    sc.add("ETH", fixed_now - 3600)      # 1 小时前 → 24h 内
    sc.add("OLD", fixed_now - 48 * 3600) # 48 小时前 → 24h 外（但 7d 内）

    active_24h = sorted(sc.active_entities("24h"))
    assert active_24h == ["BTC", "ETH"]

    # 对 7d 窗口来说 OLD 仍活跃
    active_7d = sorted(sc.active_entities("7d"))
    assert active_7d == ["BTC", "ETH", "OLD"]


def test_unknown_window_raises() -> None:
    """
    接口防御：非法 window 名应抛 ValueError，而不是默默返回 0。
    这能在调用方拼错窗口名时第一时间发现。
    """
    sc = SlidingCounter()
    with pytest.raises(ValueError, match="unknown window"):
        sc.count("BTC", "2h")
    with pytest.raises(ValueError, match="unknown window"):
        sc.active_entities("30min")


# ===========================================================================
# Part 2：backfill_from_db 测试
# ===========================================================================
#
# 策略：
# 1. 用 FakeDatabase + FakeSession 替代真实 DB
# 2. FakeResult.partitions(chunk_size) 返回若干 chunk（每个是 (entity, ts) 的 list）
# 3. 通过 `chunk_delay` 参数控制每处理完一个 chunk 时 time.time 推进多少秒，
#    从而精确造出"耗时 N 秒"的场景
# 4. 用 loguru `logger.add(sink_fn, level='TRACE')` 捕获日志，
#    测试结束在 finally 里 remove，避免影响后续测试
# ===========================================================================


@dataclass
class _FakeResult:
    """模拟 `session.execute(stmt)` 的返回值，只需要 `.partitions(size)`。"""

    chunks: list[list[tuple[str, datetime]]]

    def partitions(self, size: int) -> Iterator[list[tuple[str, datetime]]]:
        # size 参数被 design 里的 stream 代码传入；我们已经按
        # chunk_size 手动切好了 chunks，直接 yield 即可
        for c in self.chunks:
            yield c


@dataclass
class _FakeSession:
    """模拟 SQLAlchemy Session，只实现 execute + chunk_delay 推进 time。"""

    chunks: list[list[tuple[str, datetime]]]
    # 每处理完一个 chunk 推进多少秒（用于造耗时场景）
    chunk_delay: float = 0.0
    # 如果非 None，execute 直接抛这个异常（模拟情况 D）
    raise_on_execute: Exception | None = None
    # 与外层共享的"当前 time"引用，通过 list[float] 传递可变
    time_holder: list[float] = field(default_factory=lambda: [0.0])

    def execute(self, stmt):
        if self.raise_on_execute is not None:
            raise self.raise_on_execute
        return _FakeResultWithDelay(
            chunks=self.chunks,
            chunk_delay=self.chunk_delay,
            time_holder=self.time_holder,
        )


@dataclass
class _FakeResultWithDelay:
    """
    partitions 每 yield 一个 chunk 前/后推进 time_holder，
    模拟"处理这个 chunk 用了 chunk_delay 秒"。
    """

    chunks: list[list[tuple[str, datetime]]]
    chunk_delay: float
    time_holder: list[float]

    def partitions(self, size: int) -> Iterator[list[tuple[str, datetime]]]:
        for c in self.chunks:
            yield c
            # chunk 处理完（调用方 add 完）后推进时间，
            # 这样 backfill_from_db 内的 `elapsed_now = time() - start` 会看到耗时增长
            self.time_holder[0] += self.chunk_delay


@dataclass
class _FakeDatabase:
    """最小 Database mock：只提供 `get_session()` contextmanager。"""

    session: _FakeSession

    @contextmanager
    def get_session(self):
        yield self.session


def _make_chunks(count_per_chunk: int, n_chunks: int) -> list[list[tuple[str, datetime]]]:
    """生成 n_chunks 个 chunk，每个含 count_per_chunk 条 (entity, ts) 测试数据。"""
    now = datetime.now(timezone.utc)
    chunks = []
    for ci in range(n_chunks):
        chunk = []
        for i in range(count_per_chunk):
            chunk.append((f"E{ci}_{i}", now - timedelta(seconds=ci * 10 + i)))
        chunks.append(chunk)
    return chunks


@pytest.fixture
def loguru_capture():
    """
    用 loguru 自己的 sink 机制捕获日志。

    pytest 的 caplog 默认只抓 stdlib logging，抓不到 loguru 的输出；
    这里在 fixture 里 `logger.add(sink_fn)`，sink_fn 把每条消息追加到
    一个 list，测试用例读 list 做断言。测试结束在 finally 里 remove，
    保证后续测试的日志行为不受影响。
    """
    records: list[dict] = []

    def sink(message):
        # message.record 是 loguru 的记录字典（含 level, message, ...）
        r = message.record
        records.append({
            "level": r["level"].name,
            "message": r["message"],
        })

    sink_id = logger.add(sink, level="TRACE", format="{message}")
    try:
        yield records
    finally:
        logger.remove(sink_id)


def test_backfill_fast_success_info_log(monkeypatch, loguru_capture) -> None:
    """
    Req 6.7 情况 A：耗时 ≤ warn_seconds，返回 (True, N, elapsed)，产生 INFO 日志。

    场景：3 个 chunk 共 15 条，每 chunk 耗时 0.1s，总耗时 ~0.3s；
    warn_seconds=1.0 → 落在情况 A。
    """
    time_holder = [1000.0]  # 任意起始值
    monkeypatch.setattr(sc_module.time, "time", lambda: time_holder[0])

    chunks = _make_chunks(count_per_chunk=5, n_chunks=3)
    session = _FakeSession(chunks=chunks, chunk_delay=0.1, time_holder=time_holder)
    db = _FakeDatabase(session=session)

    sc = SlidingCounter()
    ok, total, elapsed = sc.backfill_from_db(
        db, max_seconds=2.0, warn_seconds=1.0, chunk_size=10
    )

    assert ok is True
    assert total == 15
    assert 0.2 <= elapsed <= 0.5, f"耗时应在 ~0.3s，实际 {elapsed}"

    # 断言只有 INFO 日志（没有 WARN/ERROR）
    levels = [r["level"] for r in loguru_capture]
    assert "INFO" in levels
    assert "WARNING" not in levels
    assert "ERROR" not in levels
    # 日志内容含关键字
    info_msgs = [r["message"] for r in loguru_capture if r["level"] == "INFO"]
    assert any("backfill 完成" in m for m in info_msgs), f"期望 INFO 含 'backfill 完成'，实际：{info_msgs}"


def test_backfill_slow_success_warns(monkeypatch, loguru_capture) -> None:
    """
    Req 6.7 情况 B：warn_seconds < 耗时 ≤ max_seconds，返回 (True, N, elapsed)，WARN 日志。

    场景：3 个 chunk 共 15 条，每 chunk 耗时 0.15s，总耗时 ~0.45s；
    warn_seconds=0.1, max_seconds=2.0 → 落在情况 B（耗时超过 warn 但没超过 max）。
    """
    time_holder = [2000.0]
    monkeypatch.setattr(sc_module.time, "time", lambda: time_holder[0])

    chunks = _make_chunks(count_per_chunk=5, n_chunks=3)
    session = _FakeSession(chunks=chunks, chunk_delay=0.15, time_holder=time_holder)
    db = _FakeDatabase(session=session)

    sc = SlidingCounter()
    ok, total, elapsed = sc.backfill_from_db(
        db, max_seconds=2.0, warn_seconds=0.1, chunk_size=10
    )

    assert ok is True
    assert total == 15
    assert elapsed > 0.1, f"耗时应超 warn_seconds(0.1s)，实际 {elapsed}"
    assert elapsed < 2.0, f"耗时不应超 max_seconds(2.0s)，实际 {elapsed}"

    # 断言产生了 WARNING 日志，且内容含"慢速成功"
    levels = [r["level"] for r in loguru_capture]
    assert "WARNING" in levels, f"期望 WARN 日志，实际 levels={levels}"
    assert "ERROR" not in levels
    warn_msgs = [r["message"] for r in loguru_capture if r["level"] == "WARNING"]
    assert any("慢速成功" in m for m in warn_msgs), f"期望 WARN 含 '慢速成功'，实际：{warn_msgs}"


def test_backfill_hard_timeout(monkeypatch, loguru_capture) -> None:
    """
    Req 6.7 情况 C：耗时超过 max_seconds，强制中止，返回 (False, ..., elapsed)，ERROR 日志。

    场景：5 个 chunk，每 chunk 耗时 0.3s，max_seconds=0.5 → 第 2 个 chunk 处理完
    累计 0.6s > 0.5s，触发硬超时中止；后续 chunk 不再处理。
    """
    time_holder = [3000.0]
    monkeypatch.setattr(sc_module.time, "time", lambda: time_holder[0])

    chunks = _make_chunks(count_per_chunk=5, n_chunks=5)
    session = _FakeSession(chunks=chunks, chunk_delay=0.3, time_holder=time_holder)
    db = _FakeDatabase(session=session)

    sc = SlidingCounter()
    ok, total, elapsed = sc.backfill_from_db(
        db, max_seconds=0.5, warn_seconds=0.1, chunk_size=10
    )

    assert ok is False, "硬超时应返回 False"
    # 至少处理了第一个 chunk（5 条），但第二个 chunk 处理完就中止
    # 因此 total 是 5 的倍数且小于 25
    assert 0 < total < 25, f"应已回填部分数据但未完成全部，实际 total={total}"
    assert elapsed > 0.5, f"触发超时时耗时必须 > max_seconds，实际 {elapsed}"

    # 断言产生了 ERROR 日志，且内容含"超过" + "硬上限"
    levels = [r["level"] for r in loguru_capture]
    assert "ERROR" in levels
    err_msgs = [r["message"] for r in loguru_capture if r["level"] == "ERROR"]
    assert any("硬上限" in m for m in err_msgs), f"期望 ERROR 含 '硬上限'，实际：{err_msgs}"


def test_backfill_db_exception(monkeypatch, loguru_capture) -> None:
    """
    Req 6.7 情况 D：数据库查询抛异常 → 捕获 + ERROR 日志，返回 (False, 0, elapsed)。
    """
    time_holder = [4000.0]
    monkeypatch.setattr(sc_module.time, "time", lambda: time_holder[0])

    # execute 被调用时直接抛异常，不会进入 partitions 循环
    session = _FakeSession(
        chunks=[],
        chunk_delay=0.0,
        raise_on_execute=RuntimeError("simulated db connection lost"),
        time_holder=time_holder,
    )
    db = _FakeDatabase(session=session)

    sc = SlidingCounter()
    ok, total, elapsed = sc.backfill_from_db(
        db, max_seconds=2.0, warn_seconds=0.5, chunk_size=10
    )

    assert ok is False
    assert total == 0

    # 断言 ERROR 日志包含异常消息
    levels = [r["level"] for r in loguru_capture]
    assert "ERROR" in levels
    err_msgs = [r["message"] for r in loguru_capture if r["level"] == "ERROR"]
    assert any("simulated db connection lost" in m for m in err_msgs), (
        f"期望 ERROR 含原异常消息，实际：{err_msgs}"
    )
    # 必须明确是"backfill failed"，便于运维 grep
    assert any("backfill failed" in m for m in err_msgs)
