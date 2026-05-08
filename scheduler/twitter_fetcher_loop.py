from __future__ import annotations

"""
Twitter 抓取后台循环。

设计:
- 单独一条线程,与摘要 worker(main.py 里的 Jobs)是两个不同的进程关注点,
  api_main.py 启动时拉起这个 loop,Flask 主线程负责 HTTP /ingest。
- 启动后立刻跑一轮(让用户起服务后马上能看到数据),之后每 interval_seconds 一轮。
- 重试策略(单轮内):
    - 可重试错误(超时 / 5xx / 429 / DB 抖动 / 网络错):间隔 retry_delay_seconds 重试,
      最多尝试 retry_times 次(含首次)。重试期间用 stop_event.wait 睡,shutdown 立即唤醒。
    - 不可重试错误(TwitterFetchPermanentError,即 4xx 非 429):立刻放弃当轮,等下个 interval。
- 单轮全部失败后,日志打 ERROR,等下个 interval 重新开始。线程**不会死**。
- shutdown() 通过 Event 唤醒所有 sleep,优雅退出。
"""

import threading

from loguru import logger

from services.twitter_fetcher_service import (
    TwitterFetchPermanentError,
    TwitterListFetcherService,
)


class TwitterFetcherLoop:
    """后台轮询 wrapper,把 TwitterListFetcherService.run_once() 跑在独立线程里。"""

    def __init__(
        self,
        fetcher: TwitterListFetcherService,
        interval_seconds: int,
        retry_times: int = 1,
        retry_delay_seconds: int = 0,
    ) -> None:
        self._fetcher = fetcher
        self._interval_seconds = interval_seconds
        # 总尝试次数(含首次),最少 1。设 0/负数会被钳制为 1,避免一轮直接什么都不做
        self._retry_times = max(1, retry_times)
        self._retry_delay_seconds = max(0, retry_delay_seconds)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """启动后台线程。daemon=True,主进程退出时跟着结束。"""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("twitter fetcher loop 已经在跑了,忽略重复 start")
            return
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="twitter_fetcher_loop",
        )
        self._thread.start()

    def shutdown(self, wait: bool = False) -> None:
        """唤醒 sleep 中的线程让它退出。"""
        self._stop_event.set()
        if wait and self._thread is not None and self._thread.is_alive():
            # 单轮可能卡在 HTTP 调用上,给 30s 让它体面退出
            self._thread.join(timeout=30)
            self._thread = None

    def _loop(self) -> None:
        logger.info(
            "twitter fetcher loop 启动:间隔 {}s,单轮最多尝试 {} 次,重试间隔 {}s",
            self._interval_seconds,
            self._retry_times,
            self._retry_delay_seconds,
        )
        # 启动立即跑一轮:用户起服务后第一次拉取不用等满 interval
        while not self._stop_event.is_set():
            self._run_round_with_retry()
            # wait() 既能 sleep 也能被 stop_event 立即唤醒
            self._stop_event.wait(self._interval_seconds)

        logger.info("twitter fetcher loop 已停止")

    def _run_round_with_retry(self) -> None:
        """跑一整轮(包含重试)。只在 _loop 里调用,异常都被吞,不会让线程死。"""
        for attempt in range(1, self._retry_times + 1):
            if self._stop_event.is_set():
                return
            try:
                self._fetcher.run_once()
                return  # 成功,本轮结束
            except TwitterFetchPermanentError as e:
                # 4xx:配置/权限问题,重试也没用。打 ERROR 提醒人来修,本轮放弃。
                logger.error(
                    "twitter fetcher 永久错误,放弃本轮(等下一个 interval): {}", e
                )
                return
            except Exception as e:
                # 可重试错误:网络抖动 / 5xx / 429 / DB 短暂不可用
                if attempt < self._retry_times:
                    logger.warning(
                        "twitter fetcher 第 {}/{} 次失败,{}s 后重试: {}",
                        attempt,
                        self._retry_times,
                        self._retry_delay_seconds,
                        e,
                    )
                    # 用 stop_event.wait 等,shutdown 时能立刻醒
                    if self._stop_event.wait(self._retry_delay_seconds):
                        return  # shutdown 信号到了,提前退出本轮
                else:
                    # 最后一次也失败了,完整 traceback 进文件日志便于排查
                    logger.exception(
                        "twitter fetcher 达到最大尝试次数 {} 仍失败,等下一个 interval: {}",
                        self._retry_times,
                        e,
                    )
