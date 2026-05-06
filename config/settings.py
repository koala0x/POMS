from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str

    ollama_base_url: str
    ollama_model: str
    ollama_timeout_seconds: int
    ollama_retry_times: int
    ollama_retry_delay_seconds: int

    poll_interval_seconds: int
    batch_size: int

    log_path: str
    log_retention_days: int

    timezone: ZoneInfo


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv()

    tz_name = os.getenv("TIMEZONE", "UTC")
    timezone = ZoneInfo(tz_name)

    return Settings(
        db_host=os.getenv("DB_HOST", "127.0.0.1"),
        db_port=_env_int("DB_PORT", 5432),
        db_name=os.getenv("DB_NAME", "postgres"),
        db_user=os.getenv("DB_USER", "postgres"),
        db_password=os.getenv("DB_PASSWORD", "postgres"),
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        ollama_model=os.getenv("OLLAMA_MODEL", "qwen3:30b"),
        ollama_timeout_seconds=_env_int("OLLAMA_TIMEOUT_SECONDS", 120),
        ollama_retry_times=_env_int("OLLAMA_RETRY_TIMES", 3),
        ollama_retry_delay_seconds=_env_int("OLLAMA_RETRY_DELAY_SECONDS", 10),
        poll_interval_seconds=_env_int("POLL_INTERVAL_SECONDS", 30),
        batch_size=_env_int("BATCH_SIZE", 50),
        log_path=os.getenv("LOG_PATH", "./logs/service.log"),
        log_retention_days=_env_int("LOG_RETENTION_DAYS", 30),
        timezone=timezone,
    )
