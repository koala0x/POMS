from __future__ import annotations

"""
配置中心。

所有运行时配置直接写在这里,**不依赖外部 .env 文件**。
- 改配置:直接修改本文件 Settings 类里的字段默认值
- 新增配置:在 Settings 里加字段并填默认值,使用方 get_settings() 读取
"""

from dataclasses import dataclass, field
from functools import lru_cache
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Settings:
    """
    服务运行所需的全部配置项。

    约定:
    - 所有时间计算以 timezone 作为"业务时区",写入数据库时保留 tz 信息
    - DB_* 用于连接 PostgreSQL
    - OLLAMA_* 用于调用本地 Ollama 的 /api/chat 接口
    - 一次摘要(level1)与二次摘要(level2)使用各自的 Ollama 配置:
      level1 高频调用,推荐轻量模型;level2 一小时一次,可上大模型
    """

    # ------------------------------ PostgreSQL ------------------------------
    db_host: str = "127.0.0.1"
    db_port: int = 5432
    db_name: str = "all_new"
    db_user: str = "all_new"
    db_password: str = "123qwe"

    # ------------------------------ Ollama 服务 ------------------------------
    ollama_base_url: str = "http://localhost:11434"

    # 一次摘要(每 30s 触发,频繁) → 推荐轻量模型
    ollama_model_level1: str = "qwen3:8b"
    ollama_timeout_level1: int = 600

    # 二次摘要(每小时一次,可上大模型)
    ollama_model_level2: str = "qwen3:30b"
    ollama_timeout_level2: int = 1800

    # 重试策略(网络/解析类异常时使用,ReadTimeout 不重试)
    # 本地 Ollama 不会有网络抖动,失败基本是模型/输入问题,重试只会让 CPU 雪上加霜,
    # 因此默认 retry_times=1(只跑一次,不重试),delay=0。
    ollama_retry_times: int = 1
    ollama_retry_delay_seconds: int = 0

    # ------------------------------ 业务参数 --------------------------------
    poll_interval_seconds: int = 30   # Level1 worker 空闲时的轮询间隔
    batch_size: int = 10              # 一次摘要的批大小

    # ------------------------------ 日志 -----------------------------------
    log_path: str = "./logs/service.log"
    log_retention_days: int = 30

    # ------------------------------ 时区 -----------------------------------
    # 整点窗口计算用(level2 时间窗 [上一小时, 本小时))
    # frozen dataclass 中可变默认必须用 default_factory
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    返回单例 Settings。

    通过 lru_cache 保证整个进程只构造一次,避免重复实例化。
    """
    return Settings()
