from __future__ import annotations

"""
任务调度。

设计要点：
- 单 worker 线程串行跑所有 service。
- 老链路（Level1Service / Level2Service）已于 2026-05 淘汰，本类只调度
  新链路（normalizer / entity_extractor / hotness / cooccur / alert / briefing 等）。
- 触发条件由各 service 自己判断，run_once() 返回 True 表示真处理了数据。
- 节奏：
    while not stop:
        for svc in new_services:
            processed |= svc.run_once()
        if processed:    # 还可能有积压，立刻进下一轮
            continue
        else:            # 全部"数据不足"，sleep poll_interval_seconds
            stop_event.wait(poll_interval_seconds)
"""

import threading
from typing import Sequence

from loguru import logger


class Jobs:
    """
    单线程 worker 调度器。

    通过 start() 启动 worker 线程，shutdown() 干净退出。

    异常隔离：单 service 抛错只打 ERROR 日志，不影响同轮其他 service，
    也不影响 worker 主循环。
    """

    def __init__(
        self,
        new_services: Sequence[object],
        poll_interval_seconds: int,
    ) -> None:
        self._new_services = new_services
        self._poll_interval_seconds = poll_interval_seconds
        # 用 Event 让 worker 线程能被唤醒/停止
        self._stop_event = threading.Event()
        self._worker_thread: threading.Thread | None = None

    def start(self) -> None:
        """启动 worker 线程。"""
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            daemon=True,
            name="summary_worker",
        )
        self._worker_thread.start()

    def shutdown(self, wait: bool = False, join_timeout: float = 10.0) -> None:
        """
        通知 worker 退出。

        参数：
        - wait：当前版本未使用；保留签名以兼容历史调用方
        - join_timeout：join 等待上限，默认 10s
                       单测可传更小值（如 2.0）验证快速停机能力
        """
        self._stop_event.set()
        if self._worker_thread is not None and self._worker_thread.is_alive():
            # daemon=True 即便没 join 上，主进程退出时也会一起结束
            self._worker_thread.join(timeout=join_timeout)
            self._worker_thread = None

    def _worker_loop(self) -> None:
        """
        串行 worker 主循环。

        每轮：
        - 顺序迭代 new_services
        - 每个 svc.run_once() 同步阻塞；返回 True 表示真处理了数据
        - 单 service 抛异常 → 捕获 + log.error + 继续下一个 service
          （保证某个 service 坏掉不会拖死整个 worker）
        - `_stop_event.is_set()` 检查嵌入到 service 级别，shutdown 在最多
          一个 service 周期后生效
        - 任意一个 svc 真处理过 → 不 sleep，直接进下一轮
        - 全员都"数据不足"（或失败）→ sleep poll_interval_seconds 再 check
        """
        logger.info(
            "summary worker 启动：services={}，空闲 sleep {}s",
            len(self._new_services),
            self._poll_interval_seconds,
        )
        while not self._stop_event.is_set():
            processed_any = False

            for svc in self._new_services:
                if self._stop_event.is_set():
                    break
                try:
                    if svc.run_once():
                        processed_any = True
                except Exception as e:
                    logger.error(
                        "service {} 异常（已隔离）：{}",
                        type(svc).__name__,
                        e,
                    )

            if processed_any:
                # 还可能有积压（normalizer 刚写入新数据，或 hotness 还能再凑一批）
                continue
            logger.info(
                "本轮无数据可处理，sleep {}s 后重试",
                self._poll_interval_seconds,
            )
            self._stop_event.wait(self._poll_interval_seconds)

        logger.info("summary worker 已停止")
