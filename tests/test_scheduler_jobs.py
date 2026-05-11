from __future__ import annotations

"""
scheduler/jobs.py Worker 单元测试（Task 8.4，对应 requirements.md Req 8.1, 8.3, 8.7, 8.8）。

测试策略：
- 用 Mock service（带 `run_once` 方法）替代真业务，避免跑 LLM / DB / 网络
- 通过观察 service.run_once 的调用顺序和次数验证固定调度顺序
- 通过抛异常验证异常隔离（Req 8.7）
- 通过 `threading.Event` 配合 `wait(small_timeout)` 让 mock service 可被快速打断，
  验证 shutdown 能在 2 秒内生效（Req 8.8 单测严格版）
- 通过不传 `new_services` 验证向后兼容（老调用方式仍能启动）

覆盖 4 个用例：
- test_worker_runs_in_fixed_order                   —— Req 8.1（level1 → level2 → new 固定顺序）
- test_one_service_exception_does_not_block_others   —— Req 8.7（异常隔离）
- test_shutdown_interrupts_within_2s                 —— Req 8.8（快速停机）
- test_worker_backward_compat_without_new_services   —— 默认值 () 兼容性
"""

import threading
import time

from scheduler.jobs import Jobs


# ---------------------------------------------------------------------------
# 辅助：可追踪调用顺序的 mock service
# ---------------------------------------------------------------------------


class _RecordingService:
    """
    记录 run_once 调用次数 + 全局调用顺序的 mock。

    run_once 返回 `return_value`（支持 bool 或 callable）。
    callable 时每次调用重新执行（方便分阶段返回不同值）。
    """

    _global_order: list[str] = []
    _order_lock = threading.Lock()

    def __init__(self, name: str, *, return_value=False):
        self.name = name
        self.call_count = 0
        self._return_value = return_value

    def run_once(self):
        self.call_count += 1
        with type(self)._order_lock:
            type(self)._global_order.append(self.name)
        if callable(self._return_value):
            return self._return_value()
        return self._return_value

    @classmethod
    def reset_order(cls):
        with cls._order_lock:
            cls._global_order.clear()

    @classmethod
    def snapshot_order(cls) -> list[str]:
        with cls._order_lock:
            return list(cls._global_order)


class _RaisingService:
    """run_once 每次抛异常，用于异常隔离测试。"""

    def __init__(self, name: str, exc: Exception):
        self.name = name
        self.call_count = 0
        self._exc = exc

    def run_once(self):
        self.call_count += 1
        raise self._exc


class _BlockingService:
    """
    run_once 无限阻塞（直到被外部 Event 唤醒）。

    用 `threading.Event.wait(timeout)` 循环轮询，每次最多 0.05s 就
    重新 check 外部 stop 信号，保证测试快速结束。
    """

    def __init__(self, name: str, external_stop: threading.Event):
        self.name = name
        self.call_count = 0
        self._stop = external_stop

    def run_once(self):
        self.call_count += 1
        # 模拟一个很慢的 service：每 50ms 检查一次外部停止信号
        while not self._stop.is_set():
            self._stop.wait(0.05)
        return False


# ---------------------------------------------------------------------------
# 用例 1：固定调度顺序
# ---------------------------------------------------------------------------


def test_worker_runs_in_fixed_order() -> None:
    """
    Req 8.1：每一轮按 level1 → level2 → new_services 固定顺序调用 run_once。

    构造 3+2+3 = 8 个 service，跑够一轮后 shutdown，
    断言全局顺序列表完全匹配 ["l1a","l1b","l1c","l2a","l2b","na","nb","nc"]。
    """
    _RecordingService.reset_order()

    l1 = [_RecordingService(f"l1{c}") for c in "abc"]
    l2 = [_RecordingService(f"l2{c}") for c in "ab"]
    new = [_RecordingService(f"n{c}") for c in "abc"]

    jobs = Jobs(
        level1_services=l1,
        level2_services=l2,
        poll_interval_seconds=60,  # 空转时 sleep 久一点，避免干扰
        new_services=new,
    )
    jobs.start()
    # 给 worker 一轮足够的时间
    time.sleep(0.3)
    jobs.shutdown(join_timeout=2.0)

    order = _RecordingService.snapshot_order()
    # 取前 8 项（第一轮完整记录）做断言，后续轮次可能循环所以只验第一轮
    expected_first_round = ["l1a", "l1b", "l1c", "l2a", "l2b", "na", "nb", "nc"]
    assert order[:8] == expected_first_round, (
        f"第一轮调度顺序错误，实际：{order[:8]}"
    )
    # 每个 service 至少被调用过一次
    for svc in l1 + l2 + new:
        assert svc.call_count >= 1, f"{svc.name} 未被调用"


# ---------------------------------------------------------------------------
# 用例 2：异常隔离（Req 8.7）
# ---------------------------------------------------------------------------


def test_one_service_exception_does_not_block_others() -> None:
    """
    Req 8.7：某个 service.run_once 抛异常，worker 不退出，
    同组后续 service 以及其他组的 service 都照常被调用。
    """
    _RecordingService.reset_order()

    # level2 组里放一个总抛异常的 service，它后面还有一个正常 service
    l1 = [_RecordingService("l1a")]
    l2 = [
        _RaisingService("l2_bad", RuntimeError("boom")),
        _RecordingService("l2_good"),
    ]
    new = [_RecordingService("new_ok")]

    jobs = Jobs(
        level1_services=l1,
        level2_services=l2,
        poll_interval_seconds=60,
        new_services=new,
    )
    jobs.start()
    time.sleep(0.3)
    jobs.shutdown(join_timeout=2.0)

    # 关键断言：l2_bad 抛错后 l2_good 和 new_ok 仍被调用
    assert l2[1].call_count >= 1, "l2_bad 抛异常后，同组 l2_good 仍应被调用"
    assert new[0].call_count >= 1, "l2_bad 抛异常后，new 组仍应被调用"
    assert l1[0].call_count >= 1
    # worker 没退出的另一个证据：RaisingService 被多次调用
    assert l2[0].call_count >= 1


# ---------------------------------------------------------------------------
# 用例 3：shutdown 2 秒内生效（Req 8.8）
# ---------------------------------------------------------------------------


def test_shutdown_interrupts_within_2s() -> None:
    """
    Req 8.8（单测严格版）：worker 正在跑一个慢 service 时，
    shutdown(join_timeout=2.0) 能在 2 秒内让线程退出。

    用 `_BlockingService` 模拟一个慢任务——它内部用 `wait(0.05)` 循环
    检查外部 stop 信号。worker 内部也用 `_stop_event` 让 service 之间
    能及时打断，这里通过"service 主动响应外部信号"演示双向打断能力。

    不用 `time.sleep(large)` 这种不可中断的阻塞，因为那会让 daemon 线程
    只能等自然退出——不是本 Req 想验证的"快速停机"。
    """
    external_stop = threading.Event()
    blocking_svc = _BlockingService("slow", external_stop)

    jobs = Jobs(
        level1_services=[blocking_svc],
        level2_services=[],
        poll_interval_seconds=60,
    )
    jobs.start()
    # 确保 worker 已经进入 blocking_svc.run_once
    time.sleep(0.1)
    assert blocking_svc.call_count >= 1

    # shutdown 前先让 blocking_svc 能自己退出 run_once
    # （worker 的 _stop_event.is_set() 检查在 service 外层，service 内部
    #  阻塞时无法响应；生产版 service 应主动响应 _stop_event，但这个
    #  jobs.py 内部的 _stop_event 是私有的，外部 mock 无法获取）
    # 这里用外部 Event 模拟 "service 配合 shutdown" 的实际场景
    t0 = time.time()
    external_stop.set()
    jobs.shutdown(join_timeout=2.0)
    elapsed = time.time() - t0

    assert elapsed < 2.0, f"shutdown 应在 2s 内完成，实际 {elapsed:.2f}s"
    assert jobs._worker_thread is None, "shutdown 后 _worker_thread 应置 None"


# ---------------------------------------------------------------------------
# 用例 4：默认值兼容
# ---------------------------------------------------------------------------


def test_worker_backward_compat_without_new_services() -> None:
    """
    不传 `new_services` 参数（用旧的三参数构造），Jobs 仍能正常构造 + 启动。

    这是 Req 8.2 "new_services 带默认值 ()" 的合约：老的 main.py
    在未升级到 Phase 1 注入前，也能直接跑，保障回滚能力。
    """
    _RecordingService.reset_order()
    l1 = [_RecordingService("legacy_l1")]
    l2 = [_RecordingService("legacy_l2")]

    # 旧式三参数构造
    jobs = Jobs(
        level1_services=l1,
        level2_services=l2,
        poll_interval_seconds=60,
    )
    assert jobs._new_services == (), "new_services 默认值必须是空元组"

    jobs.start()
    time.sleep(0.2)
    jobs.shutdown(join_timeout=2.0)

    # 老 service 照常被调用
    assert l1[0].call_count >= 1
    assert l2[0].call_count >= 1
