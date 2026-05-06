from __future__ import annotations

"""
任务调度。

设计要点:
- 一次摘要(level1)与二次摘要(level2)共享**同一个 worker 线程**,串行调用。
  原因:本机 Ollama 同一时刻只能高效驻留一个模型,如果 level1 / level2 用不同
  模型并发请求,Ollama 会反复 swap(unload/load),CPU 上巨慢。把两者串到一个
  循环里就保证任意时刻只有一个 LLM 请求在飞。
- 触发条件由各 service 自己判断:
  - Level1Service.run_once():未一次摘要原始数据 >= batch_size 才真跑
  - Level2Service.run_once():未二次摘要 level1 >= level2_threshold 才真跑
- 节奏:
    while not stop:
        for svc in level1_services + level2_services:
            processed |= svc.run_once()
        if processed:    # 还可能有积压,立刻进下一轮
            continue
        else:            # 全部"数据不足",sleep poll_interval_seconds
            stop_event.wait(poll_interval_seconds)
"""

import threading
from typing import Sequence

from loguru import logger


class Jobs:
    """
    单线程 worker:level1 / level2 全部串行触发。

    通过 start() 启动 worker 线程,shutdown() 干净退出。
    """

    def __init__(
        self,
        level1_services: Sequence[object],
        level2_services: Sequence[object],
        poll_interval_seconds: int,
    ) -> None:
        self._level1_services = level1_services
        self._level2_services = level2_services
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

    def shutdown(self, wait: bool = False) -> None:
        """通知 worker 退出。"""
        self._stop_event.set()
        if self._worker_thread is not None and self._worker_thread.is_alive():
            # 等线程跑完当前一轮(最多卡在 LLM 调用上)。
            # daemon=True 即便没 join 上,主进程退出时也会一起结束。
            self._worker_thread.join(timeout=10)
            self._worker_thread = None

    def _worker_loop(self) -> None:
        """
        串行 worker 主循环。

        每轮:
        - 依次跑所有 level1_service(每个 svc.run_once() 同步阻塞)
        - 依次跑所有 level2_service(同上)
        - 任意一个 svc 真处理过 → 不 sleep,直接进下一轮(可能还有积压)
        - 全员都"数据不足"(或失败)→ sleep poll_interval_seconds 再 check
        """
        logger.info(
            "summary worker 启动:level1 services={},level2 services={},空闲 sleep {}s",
            len(self._level1_services),
            len(self._level2_services),
            self._poll_interval_seconds,
        )
        while not self._stop_event.is_set():
            processed_any = False

            # 一次提纯
            for svc in self._level1_services:
                if self._stop_event.is_set():
                    break
                try:
                    if svc.run_once():
                        processed_any = True
                except Exception as e:
                    logger.error("一次摘要任务异常:{}", e)

            # 二次提纯(在一次提纯之后,这样新写入的 level1 立刻参与触发判断)
            for svc in self._level2_services:
                if self._stop_event.is_set():
                    break
                try:
                    if svc.run_once():
                        processed_any = True
                except Exception as e:
                    logger.error("二次摘要任务异常:{}", e)

            if processed_any:
                # 还可能有积压(level1 刚写入新数据,或者 level2 还能再凑一批)
                continue
            self._stop_event.wait(self._poll_interval_seconds)

        logger.info("summary worker 已停止")
