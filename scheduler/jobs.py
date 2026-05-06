from __future__ import annotations

"""
调度器任务注册。

使用 APScheduler BackgroundScheduler：
- interval：每隔固定秒数轮询一次摘要任务（一次摘要）
- cron(minute=0)：每小时整点触发一次（二次摘要）

设计要点：
- max_instances=1：避免上一次任务没跑完又叠加一轮导致并发写库/重复摘要
- coalesce=True：如果某段时间服务卡顿，恢复后只补跑一次，而不是把错过的全部补齐
"""

from dataclasses import dataclass
from typing import Sequence

from apscheduler.schedulers.background import BackgroundScheduler
from loguru import logger


@dataclass(frozen=True)
class Jobs:
    """
    Jobs 负责把 service 绑定到 APScheduler 上。

    这里不关心业务细节，只负责“按频率触发”并做一层兜底异常捕获，
    避免单个 source 的异常把整个调度器线程打崩。
    """

    level1_services: Sequence[object]
    level2_services: Sequence[object]
    poll_interval_seconds: int
    timezone: object

    def start(self) -> BackgroundScheduler:
        scheduler = BackgroundScheduler(timezone=self.timezone)

        def run_level1() -> None:
            for svc in self.level1_services:
                try:
                    svc.run_once()
                except Exception as e:
                    logger.error("一次摘要任务异常：{}", e)

        def run_level2() -> None:
            for svc in self.level2_services:
                try:
                    svc.run_hourly()
                except Exception as e:
                    logger.error("二次摘要任务异常：{}", e)

        # 30 秒轮询一次：每次轮询内部会分别处理 twitter / binance_square
        scheduler.add_job(
            run_level1,
            trigger="interval",
            seconds=self.poll_interval_seconds,
            id="level1_poll",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        # 每小时整点：按过去 1 小时内的 level1 汇总生成 level2
        scheduler.add_job(
            run_level2,
            trigger="cron",
            minute=0,
            id="level2_hourly",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )

        scheduler.start()
        return scheduler
