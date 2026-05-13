from __future__ import annotations

"""
配置中心总入口。

物理拆分（按职责分组）：
  - _database.py  → DatabaseSettings   PG 连接（新老链路共享）
  - _runtime.py   → RuntimeSettings    日志 / 时区 / worker 调度（新老链路共享）
  - _legacy.py    → LegacySettings     老链路：Ollama + level1/2 阈值
  - _new.py       → NewPipelineSettings 新链路：normalizer/dedup/extractor/hotness

逻辑组装：
  Settings 通过 dataclass 多继承把上面 4 块拼成一个对象。
  对外完全保持 `settings.db_host` `settings.batch_size` `settings.hotness_top_k`
  这种扁平访问方式，**不破坏调用方**。

使用方：
    from config.settings import get_settings
    settings = get_settings()
    print(settings.batch_size)            # ← LegacySettings 的字段
    print(settings.hotness_top_k)         # ← NewPipelineSettings 的字段
    print(settings.timezone)              # ← RuntimeSettings 的字段
    print(settings.db_host)               # ← DatabaseSettings 的字段

修改配置：
  - 找到对应分组文件（_legacy.py / _new.py / _runtime.py / _database.py）
  - 改字段默认值，重启服务

测试中如需修改：
  - get_settings.cache_clear() 后重建实例
  - 或者 monkeypatch Settings 的字段默认值
"""

from dataclasses import dataclass
from functools import lru_cache

from ._alerts import AlertSettings
from ._database import DatabaseSettings
from ._legacy import LegacySettings
from ._new import NewPipelineSettings
from ._runtime import RuntimeSettings


@dataclass(frozen=True)
class Settings(
    DatabaseSettings,
    RuntimeSettings,
    LegacySettings,
    NewPipelineSettings,
    AlertSettings,
):
    """
    全部配置的"扁平视图"。

    通过 dataclass 多继承把 5 个分组合并到一个对象上。所有字段名跨分组
    必须不重复（dataclass 多继承会因重名字段产生不可预期行为，loader 启动
    会报错或后定义覆盖前定义）。

    继承顺序的副作用：MRO 从左到右是
        Settings → DatabaseSettings → RuntimeSettings → LegacySettings
                 → NewPipelineSettings → AlertSettings
    访问 `settings.<field>` 时按 MRO 找，所以同名字段以**靠左继承的优先**。
    当前各分组字段名互不重叠（AlertSettings 全部带 alert_/telegram_ 前缀），
    顺序不影响行为。
    """


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    返回单例 Settings。

    通过 lru_cache 保证整个进程只构造一次，避免重复实例化。
    测试中如需修改配置，可以 get_settings.cache_clear() 后重建实例。
    """
    return Settings()


# 公开导出 —— 调用方习惯 `from config.settings import get_settings, Settings`
__all__ = [
    "Settings",
    "get_settings",
    # 顺带把 5 个分组类也导出，方便测试 / 类型标注精准引用某个子集
    "DatabaseSettings",
    "RuntimeSettings",
    "LegacySettings",
    "NewPipelineSettings",
    "AlertSettings",
]
