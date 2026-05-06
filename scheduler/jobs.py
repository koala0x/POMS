from __future__ import annotations

"""
任务调度。

设计要点:
- Level1(一次摘要)采用**工作线程 + while 循环**的串行轮询,而不是 APScheduler 的
  interval 触发。原因:LLM 单次推理可能远超 30 秒(CPU 上 2-5 分钟很常见),
  interval 触发会导致请求堆积/skip 警告。串行轮询天然避免这个问题:
    while not stop:
        for svc in level1_services:
            processed = svc.run_once()  # 数据足够才会真正调 LLM
        if 任何一个 svc 处理过 → 立刻进下一轮(可能还有积压)
        else                     → sleep poll_interval_seconds 再 recheck

- Level2(二次摘要)依然用 APScheduler 的 cron(minute=0)整点触发,
  本身一小时一次,不存在抢占。
"""

import threading
from typing import Sequence

from apscheduler.schedulers.background import BackgroundScheduler
from loguru import logger


class Jobs:
    """
    任务调度器:Level1 走 worker 线程,Level2 走 APScheduler cron。

    通过 start() 启动,shutdown() 干净退出。
    """

    def __init__(
        self,
        level1_services: Sequence[object],
        level2_services: Sequence[object],
        poll_interval_seconds: int,
        timezone: object,
    ) -> None:
        self._level1_services = level1_services
        self._level2_services = level2_services
        self._poll_interval_seconds = poll_interval_seconds
        self._timezone = timezone
        # 用 Event 让 worker 线程能被唤醒/停止
        self._stop_event = threading.Event()
        self._level1_thread: threading.Thread | None = None
        self._scheduler: BackgroundScheduler | None = None

    def start(self) -> None:
        """启动 Level1 worker 线程 + Level2 cron 调度器。"""
        self._level1_thread = threading.Thread(
            target=self._level1_loop,
            daemon=True,
            name="level1_worker",
        )
        self._level1_thread.start()

        scheduler = BackgroundScheduler(timezone=self._timezone)
        scheduler.add_job(
            self._run_level2_all,
            trigger="cron",
            minute=0,
            id="level2_hourly",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        scheduler.start()
        self._scheduler = scheduler

    def shutdown(self, wait: bool = False) -> None:
        """通知 worker 退出 + 关 cron 调度器。"""
        self._stop_event.set()
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=wait)
            self._scheduler = None
        if self._level1_thread is not None and self._level1_thread.is_alive():
            # 等线程跑完当前一轮(最多卡在 LLM 调用上)。
            # daemon=True 即便没 join 上,主进程退出时也会一起结束。
            self._level1_thread.join(timeout=10)
            self._level1_thread = None

    def _level1_loop(self) -> None:
        """
        Level1 串行 worker。

        关键节奏:
        - 每轮按顺序遍历所有 level1_service,每个 svc.run_once() **同步阻塞**等返回
        - 任意 svc 真的处理了一批 → 不 sleep,直接进下一轮(可能还有积压未处理完)
        - 一整轮全员都"数据不足" → sleep poll_interval_seconds 再 check
        """
        logger.info(
            "Level1 worker 启动:串行轮询 {} 个 source,空闲 sleep {}s",
            len(self._level1_services),
            self._poll_interval_seconds,
        )
        while not self._stop_event.is_set():
            processed_any = False
            for svc in self._level1_services:
                if self._stop_event.is_set():
                    break
                try:
                    if svc.run_once():
                        processed_any = True
                except Exception as e:
                    logger.error("Level1 worker 异常:{}", e)

            if processed_any:
                # 还有可能有积压数据,立刻进下一轮
                continue
            # 所有 source 都数据不足,sleep poll_interval_seconds(支持被 stop_event 唤醒提前退出)
            self._stop_event.wait(self._poll_interval_seconds)

        logger.info("Level1 worker 已停止")

    def _run_level2_all(self) -> None:
        """APScheduler 触发回调:依次跑所有 Level2 service。"""
        for svc in self._level2_services:
            try:
                svc.run_hourly()
            except Exception as e:
                logger.error("二次摘要任务异常:{}", e)
