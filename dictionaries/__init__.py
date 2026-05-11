from __future__ import annotations

"""
词典包入口。

对外暴露：
- `get_dictionaries() -> Dictionaries`：返回进程内单例
- `DictionaryEntry` / `Dictionaries`：数据类型，供类型标注使用

使用方式（在任一模块里）：
    from dictionaries import get_dictionaries
    dicts = get_dictionaries()
    # 查实体：dicts.alias_index.get("btc") → ("BTC", "ticker")

单例实现：
- 用 `functools.lru_cache(maxsize=1)` 做进程内懒加载
- 首次调用时读 YAML；后续直接返回缓存对象
- Phase 1 不提供热加载（requirements.md Req 3.5：启动加载，运行时不重读）

测试友好：
- 测试可以通过 `get_dictionaries.cache_clear()` 强制重新加载
- 或者直接调用 `load_dictionaries(custom_dir)` 拿到独立实例做断言
"""

from functools import lru_cache
from pathlib import Path

from .loader import DictionaryEntry, Dictionaries, load_dictionaries

# dictionaries/ 目录 = 本文件所在目录
_DEFAULT_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def get_dictionaries() -> Dictionaries:
    """
    返回全局词典单例。

    首次调用时会从 `dictionaries/*.yaml` 加载；后续调用直接命中缓存。
    若加载失败（YAML 语法错、必填字段缺失、跨文件冲突）会抛错，
    由上层（main.py）在进程启动阶段拦截，阻止服务起来。
    """
    return load_dictionaries(_DEFAULT_DIR)


__all__ = [
    "DictionaryEntry",
    "Dictionaries",
    "load_dictionaries",
    "get_dictionaries",
]
