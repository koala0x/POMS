from __future__ import annotations

"""
运行时基础设施配置（独立分组）。

放跨链路共享的"非业务参数"：日志、时区、worker 轮询间隔、老链路总开关。
这些参数无论新老链路都会用到，所以拆出来单独一层。

不放 Ollama 配置（那是老链路专属，去 `_legacy.py`）。
不放 hotness/normalizer 这些（那是新链路专属，去 `_new.py`）。
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

    # ----- 老链路总开关（Phase 1 过渡期）-----
    # True  = 停掉老链路（Level1Service / Level2Service），不调 Ollama
    # False = 老链路和新链路并行跑（原始设计）
    #
    # 影响：
    # - True 时 main.py 跳过 OllamaClient + 老 service 构造，启动更快
    # - level1_service.py / level2_service.py 代码本身不动，测试也仍然跑
    disable_legacy_pipeline: bool = True

    # ----- 日志 -----

    # 日志文件路径。启动时会自动创建上级目录。
    # 相对路径基于进程启动时的 cwd，建议部署时改成绝对路径避免歧义。
    log_path: str = "./logs/service.log"

    # 日志文件保留天数。按天滚动（rotation="00:00"），超过保留期的旧文件自动清理。
    log_retention_days: int = 30

    # ----- 业务时区 -----

    # 业务时区。影响：
    # - summary_level1 / summary_level2 的 created_at 写入 tz
    # - 新链路 hotness_snapshots.window_end 的对齐参考
    # 写库字段是 TIMESTAMPTZ，设成 UTC 以外的 tz 只影响显示，不影响存储语义。
    # frozen dataclass 里可变默认必须用 default_factory。
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"))
