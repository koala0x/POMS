from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from apscheduler.schedulers.background import BackgroundScheduler
from loguru import logger


@dataclass(frozen=True)
class Jobs:
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

        scheduler.add_job(
            run_level1,
            trigger="interval",
            seconds=self.poll_interval_seconds,
            id="level1_poll",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
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
