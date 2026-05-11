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
    单线程 worker:level1 / level2 / new_services 全部串行触发。

    通过 start() 启动 worker 线程,shutdown() 干净退出。

    Phase 1 扩展（crypto-narrative-radar）：
    - 新增 `new_services` 参数（带默认值保证向后兼容）
    - `_worker_loop` 按固定顺序 level1 → level2 → new 轮转
    - 每组内部的异常被隔离：单 service 抛错只打 ERROR 日志，不影响同组/跨组其他 service
    """

    def __init__(
        self,
        level1_services: Sequence[object],
        level2_services: Sequence[object],
        poll_interval_seconds: int,
        new_services: Sequence[object] = (),
    ) -> None:
        self._level1_services = level1_services
        self._level2_services = level2_services
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
        - join_timeout：join 等待上限，默认 10s（生产 Req 8.8 的阈值）
                       单测可传更小值（如 2.0）验证快速停机能力
        """
        self._stop_event.set()
        if self._worker_thread is not None and self._worker_thread.is_alive():
            # daemon=True 即便没 join 上,主进程退出时也会一起结束。
            self._worker_thread.join(timeout=join_timeout)
            self._worker_thread = None

    def _worker_loop(self) -> None:
        """
        串行 worker 主循环（Phase 1 扩展为三组：level1 → level2 → new_services）。

        每轮:
        - 按固定顺序迭代 level1 / level2 / new_services 三组
        - 每个 svc.run_once() 同步阻塞；返回 True 表示真处理了数据
        - 单 service 抛异常 → 捕获 + log.error + 继续下一个 service
          （保证某个 service 坏掉不会拖死整个 worker）
        - `_stop_event.is_set()` 检查嵌入到 service 级别，shutdown 在最多
          一个 service 周期后生效（单测里的 wait 语义由此而来）
        - 任意一个 svc 真处理过 → 不 sleep,直接进下一轮
        - 全员都"数据不足"(或失败)→ sleep poll_interval_seconds 再 check
        """
        logger.info(
            "summary worker 启动:level1={},level2={},new={},空闲 sleep {}s",
            len(self._level1_services),
            len(self._level2_services),
            len(self._new_services),
            self._poll_interval_seconds,
        )
        # 固定顺序：老链路先跑（LLM 慢活优先消化），新链路跟在后面
        groups = (
            ("level1", self._level1_services),
            ("level2", self._level2_services),
            ("new", self._new_services),
        )
        while not self._stop_event.is_set():
            processed_any = False

            for group_name, group in groups:
                for svc in group:
                    if self._stop_event.is_set():
                        break
                    try:
                        if svc.run_once():
                            processed_any = True
                    except Exception as e:
                        # 异常隔离：单 service 抛错不影响其他 service 与本轮其他组
                        logger.error(
                            "{} 服务 {} 异常（已隔离）：{}",
                            group_name,
                            type(svc).__name__,
                            e,
                        )

            if processed_any:
                # 还可能有积压(level1 刚写入新数据,或者 level2 还能再凑一批)
                continue
            logger.info(
                "本轮三组均无数据可处理,sleep {}s 后重试",
                self._poll_interval_seconds,
            )
            self._stop_event.wait(self._poll_interval_seconds)

        logger.info("summary worker 已停止")
