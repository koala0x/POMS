from __future__ import annotations

"""
配置加载模块。

- 从 .env / 环境变量读取配置并统一暴露给其他模块使用
- 使用 lru_cache 确保配置只解析一次，避免多处 import 反复读取环境变量
- TIMEZONE 用于“整点”边界计算（例如二次摘要按小时窗口汇总）
"""

import os
from dataclasses import dataclass
from functools import lru_cache
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    """
    服务运行所需的全部配置项。

    约定:
    - 所有时间计算以 timezone 作为"业务时区",写入数据库时保留 tz 信息
    - DB_* 用于连接 PostgreSQL
    - OLLAMA_* 用于调用本地 Ollama 的 /api/chat 接口
    - 一次摘要(level1)与二次摘要(level2)可以使用不同的模型/超时:
      level1 高频调用,推荐轻量模型;level2 一小时一次,可上大模型
    """

    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str

    ollama_base_url: str
    ollama_model_level1: str
    ollama_model_level2: str
    ollama_timeout_level1: int
    ollama_timeout_level2: int
    ollama_retry_times: int
    ollama_retry_delay_seconds: int

    poll_interval_seconds: int
    batch_size: int

    log_path: str
    log_retention_days: int

    timezone: ZoneInfo


def _env_int(name: str, default: int) -> int:
    """
    从环境变量读取 int，缺省或空字符串时返回 default。

    这里不吞掉 ValueError，避免默默使用错误配置导致难以排查的问题。
    """
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    读取并构造 Settings。

    load_dotenv() 会按惯例从当前工作目录下的 .env 加载环境变量(如果存在)。
    lru_cache 保证整个进程只创建一份 Settings 实例。

    向后兼容:
    - 如果只设置了 OLLAMA_MODEL,会同时作为 level1 / level2 的默认模型
    - OLLAMA_TIMEOUT_SECONDS 同理
    """
    load_dotenv()

    tz_name = os.getenv("TIMEZONE", "UTC")
    timezone = ZoneInfo(tz_name)

    legacy_model = os.getenv("OLLAMA_MODEL", "qwen3:8b")
    legacy_timeout = _env_int("OLLAMA_TIMEOUT_SECONDS", 120)

    return Settings(
        db_host=os.getenv("DB_HOST", "127.0.0.1"),
        db_port=_env_int("DB_PORT", 5432),
        db_name=os.getenv("DB_NAME", "all_new"),
        db_user=os.getenv("DB_USER", "all_new"),
        db_password=os.getenv("DB_PASSWORD", "123qwe"),
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        ollama_model_level1=os.getenv("OLLAMA_MODEL_LEVEL1", legacy_model),
        ollama_model_level2=os.getenv("OLLAMA_MODEL_LEVEL2", legacy_model),
        ollama_timeout_level1=_env_int("OLLAMA_TIMEOUT_LEVEL1", legacy_timeout),
        ollama_timeout_level2=_env_int("OLLAMA_TIMEOUT_LEVEL2", legacy_timeout),
        ollama_retry_times=_env_int("OLLAMA_RETRY_TIMES", 3),
        ollama_retry_delay_seconds=_env_int("OLLAMA_RETRY_DELAY_SECONDS", 10),
        poll_interval_seconds=_env_int("POLL_INTERVAL_SECONDS", 30),
        batch_size=_env_int("BATCH_SIZE", 50),
        log_path=os.getenv("LOG_PATH", "./logs/service.log"),
        log_retention_days=_env_int("LOG_RETENTION_DAYS", 30),
        timezone=timezone,
    )
