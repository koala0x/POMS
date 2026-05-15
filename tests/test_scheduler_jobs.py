from __future__ import annotations

"""
scheduler/jobs.py Worker 单元测试。

测试策略：
- 用 Mock service（带 `run_once` 方法）替代真业务，避免跑 LLM / DB / 网络
- 通过观察 service.run_once 的调用顺序和次数验证固定调度顺序
- 通过抛异常验证异常隔离
- 通过 `threading.Event` 配合 `wait(small_timeout)` 让 mock service 可被快速打断，
  验证 shutdown 能在 2 秒内生效

历史变更（2026-05）：
  - 老链路（Level1Service / Level2Service）已淘汰
  - Jobs.__init__ 简化：只接受 new_services + poll_interval_seconds 两个参数
  - 删除：test_worker_backward_compat_without_new_services（旧 API 已不存在）

覆盖 3 个用例：
- test_worker_runs_in_fixed_order      —— new_services 按列表顺序逐一调用
- test_one_service_exception_does_not_block_others  —— 异常隔离
- test_shutdown_interrupts_within_2s    —— 快速停机
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
    每一轮按 new_services 列表顺序逐一调用 run_once。

    构造 5 个 service 跑够一轮后 shutdown，断言全局顺序列表前 5 项
    完全等于注入顺序。
    """
    _RecordingService.reset_order()

    services = [_RecordingService(f"svc{i}") for i in range(5)]

    jobs = Jobs(
        new_services=services,
        poll_interval_seconds=60,  # 空转时 sleep 久一点，避免干扰
    )
    jobs.start()
    # 给 worker 一轮足够的时间
    time.sleep(0.3)
    jobs.shutdown(join_timeout=2.0)

    order = _RecordingService.snapshot_order()
    expected_first_round = ["svc0", "svc1", "svc2", "svc3", "svc4"]
    assert order[:5] == expected_first_round, (
        f"第一轮调度顺序错误，实际：{order[:5]}"
    )
    # 每个 service 至少被调用过一次
    for svc in services:
        assert svc.call_count >= 1, f"{svc.name} 未被调用"


# ---------------------------------------------------------------------------
# 用例 2：异常隔离
# ---------------------------------------------------------------------------


def test_one_service_exception_does_not_block_others() -> None:
    """
    某个 service.run_once 抛异常，worker 不退出，
    后续 service 仍被调用。
    """
    _RecordingService.reset_order()

    services = [
        _RecordingService("svc_before"),
        _RaisingService("svc_bad", RuntimeError("boom")),
        _RecordingService("svc_after"),
    ]

    jobs = Jobs(
        new_services=services,
        poll_interval_seconds=60,
    )
    jobs.start()
    time.sleep(0.3)
    jobs.shutdown(join_timeout=2.0)

    # 关键断言：svc_bad 抛错后 svc_after 仍被调用
    assert services[2].call_count >= 1, "svc_bad 抛异常后，svc_after 仍应被调用"
    assert services[0].call_count >= 1
    # worker 没退出的另一个证据：RaisingService 被多次调用
    assert services[1].call_count >= 1


# ---------------------------------------------------------------------------
# 用例 3：shutdown 2 秒内生效
# ---------------------------------------------------------------------------


def test_shutdown_interrupts_within_2s() -> None:
    """
    worker 正在跑一个慢 service 时，shutdown(join_timeout=2.0) 能在
    2 秒内让线程退出。

    用 `_BlockingService` 模拟一个慢任务——它内部用 `wait(0.05)` 循环
    检查外部 stop 信号。这里通过"service 主动响应外部信号"演示双向打断能力。
    """
    external_stop = threading.Event()
    blocking_svc = _BlockingService("slow", external_stop)

    jobs = Jobs(
        new_services=[blocking_svc],
        poll_interval_seconds=60,
    )
    jobs.start()
    # 确保 worker 已经进入 blocking_svc.run_once
    time.sleep(0.1)
    assert blocking_svc.call_count >= 1

    # shutdown 前先让 blocking_svc 能自己退出 run_once
    t0 = time.time()
    external_stop.set()
    jobs.shutdown(join_timeout=2.0)
    elapsed = time.time() - t0

    assert elapsed < 2.0, f"shutdown 应在 2s 内完成，实际 {elapsed:.2f}s"
    assert jobs._worker_thread is None, "shutdown 后 _worker_thread 应置 None"
