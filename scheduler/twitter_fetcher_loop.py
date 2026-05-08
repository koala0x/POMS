from __future__ import annotations

"""
Twitter 抓取后台循环。

设计:
- 单独一条线程,与摘要 worker(main.py 里的 Jobs)是两个不同的进程关注点,
  api_main.py 启动时拉起这个 loop,Flask 主线程负责 HTTP /ingest。
- 启动后立刻跑一轮(让用户起服务后马上能看到数据),之后每 interval_seconds 一轮。
- 单轮异常被捕获并打日志,不让线程死掉;下一轮照常进行。
- shutdown() 通过 Event 唤醒 sleep,优雅退出。
"""

import threading

from loguru import logger

from services.twitter_fetcher_service import TwitterListFetcherService


class TwitterFetcherLoop:
    """后台轮询 wrapper,把 TwitterListFetcherService.run_once() 跑在独立线程里。"""

    def __init__(
        self,
        fetcher: TwitterListFetcherService,
        interval_seconds: int,
    ) -> None:
        self._fetcher = fetcher
        self._interval_seconds = interval_seconds
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
            "twitter fetcher loop 启动:间隔 {}s,启动立即跑首轮",
            self._interval_seconds,
        )
        # 启动立即跑一轮:用户起服务后第一次拉取不用等满 interval
        while not self._stop_event.is_set():
            try:
                self._fetcher.run_once()
            except Exception as e:
                # 单轮失败不影响后续轮次。常见原因:网络抖动、API 限流、key 失效。
                # 完整 traceback 进文件日志便于排查,控制台只看 ERROR 一行。
                logger.exception("twitter fetcher 本轮失败:{}", e)

            # wait() 既能 sleep 也能被 stop_event 立即唤醒
            self._stop_event.wait(self._interval_seconds)

        logger.info("twitter fetcher loop 已停止")
