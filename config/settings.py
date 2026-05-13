from __future__ import annotations

"""
配置中心总入口。

物理拆分（按职责分组）：
  - _database.py  → DatabaseSettings    PG 连接
  - _runtime.py   → RuntimeSettings     日志 / 时区 / worker 调度
  - _llm.py       → LLMSettings         Ollama 服务（BriefingService 用）
  - _new.py       → NewPipelineSettings 业务流水线参数（normalizer / dedup /
                                       extractor / hotness / cooccur / briefing）
  - _alerts.py    → AlertSettings       Telegram 告警

逻辑组装：
  Settings 通过 dataclass 多继承把上面 5 块拼成一个对象。
  对外完全保持 `settings.db_host` `settings.hotness_top_k` `settings.briefing_top_n`
  这种扁平访问方式，**不破坏调用方**。

历史变更（2026-05）：
  - 老链路（Level1Service / Level2Service）已淘汰
  - 旧 `LegacySettings`（`config/_legacy.py`）改名为 `LLMSettings`（`config/_llm.py`）
  - 删除字段：`ollama_model_level1` / `ollama_timeout_level1` /
    `ollama_model_level2` / `ollama_timeout_level2` / `batch_size` /
    `level2_threshold` / `disable_legacy_pipeline`
  - 保留字段：`ollama_base_url` / `ollama_model_level5` / `ollama_timeout_level5`

使用方：
    from config.settings import get_settings
    settings = get_settings()
    print(settings.hotness_top_k)         # ← NewPipelineSettings 的字段
    print(settings.briefing_top_n)        # ← NewPipelineSettings 的字段
    print(settings.timezone)              # ← RuntimeSettings 的字段
    print(settings.db_host)               # ← DatabaseSettings 的字段
    print(settings.ollama_model_level5)   # ← LLMSettings 的字段

修改配置：
  - 找到对应分组文件，改字段默认值，重启服务

测试中如需修改：
  - get_settings.cache_clear() 后重建实例
  - 或者 monkeypatch Settings 的字段默认值
"""

from dataclasses import dataclass
from functools import lru_cache

from ._alerts import AlertSettings
from ._database import DatabaseSettings
from ._llm import LLMSettings
from ._new import NewPipelineSettings
from ._runtime import RuntimeSettings


@dataclass(frozen=True)
class Settings(
    DatabaseSettings,
    RuntimeSettings,
    LLMSettings,
    NewPipelineSettings,
    AlertSettings,
):
    """
    全部配置的"扁平视图"。

    通过 dataclass 多继承把 5 个分组合并到一个对象上。所有字段名跨分组
    必须不重复。

    继承顺序的副作用：MRO 从左到右是
        Settings → DatabaseSettings → RuntimeSettings → LLMSettings
                 → NewPipelineSettings → AlertSettings
    访问 `settings.<field>` 时按 MRO 找；当前各分组字段名互不重叠
    （AlertSettings 全部带 alert_/telegram_ 前缀），顺序不影响行为。
    """


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    返回单例 Settings。

    通过 lru_cache 保证整个进程只构造一次，避免重复实例化。
    测试中如需修改配置，可以 get_settings.cache_clear() 后重建实例。
    """
    return Settings()


__all__ = [
    "Settings",
    "get_settings",
    "DatabaseSettings",
    "RuntimeSettings",
    "LLMSettings",
    "NewPipelineSettings",
    "AlertSettings",
]
