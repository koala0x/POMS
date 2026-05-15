from __future__ import annotations

"""
运行时基础设施配置（独立分组）。

放跨链路共享的"非业务参数"：日志、时区、worker 轮询间隔。
LLM 配置去 `_llm.py`；新链路业务配置去 `_new.py`；告警去 `_alerts.py`。
"""

from dataclasses import dataclass, field
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class RuntimeSettings:
    # ----- worker 调度 -----

    # worker 空闲轮询间隔（秒）。
    # 当所有 service 都"数据不足无事可做"时，worker sleep 这么久再查一遍；
    # 只要本轮有任一 service 真处理了数据，就**立刻**进下一轮不 sleep。
    poll_interval_seconds: int = 30

    # ----- 日志 -----

    # 日志文件路径。启动时会自动创建上级目录。
    # 相对路径基于进程启动时的 cwd，建议部署时改成绝对路径避免歧义。
    log_path: str = "./logs/service.log"

    # 日志文件保留天数。按天滚动（rotation="00:00"），超过保留期的旧文件自动清理。
    log_retention_days: int = 30

    # ----- 业务时区 -----

    # 业务时区。影响：
    # - 新链路 hotness_snapshots.window_end / entity_briefings.window_end 的对齐参考
    # - Telegram 告警 / Digest 消息里的时间戳显示
    # 写库字段是 TIMESTAMPTZ，设成 UTC 以外的 tz 只影响显示，不影响存储语义。
    # frozen dataclass 里可变默认必须用 default_factory。
    #
    # 默认 Asia/Shanghai（UTC+8）：项目主要用户在国内，看 UTC 不直观。
    # 改成 UTC 走 ZoneInfo("UTC")；改成美东走 ZoneInfo("America/New_York")。
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("Asia/Shanghai"))
