from __future__ import annotations

"""
词典加载器：从 dictionaries/*.yaml 构造不可变的 `Dictionaries` 对象。

使用者（prefilter.py、Entity_Extractor）通过 `dictionaries/__init__.py` 暴露的
`get_dictionaries()` 拿到单例，保证同一份词典数据被所有消费方共享。

设计要点（对应 requirements.md Req 3）：
- 启动时一次性加载，运行时不重载（Req 3.5：Phase 1 不做热加载）
- 四个文件：tickers.yaml / chains.yaml / narratives.yaml / kols.yaml
- 格式错（YAML 语法错、必填字段缺失）直接 raise，阻止服务启动（Req 3.5）
- 空文件 WARN 允许启动（Req 3.6）
- 跨文件同名 / 别名冲突 raise 明确错误（Req 3.8）
- 加载完成输出每个文件的词条数（Req 3.7）

不可变性实现：
- `DictionaryEntry` / `Dictionaries` 均为 frozen dataclass
- 所有 dict 字段用 `types.MappingProxyType` 包裹，防止消费方误改全局状态
"""

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import yaml
from loguru import logger


@dataclass(frozen=True)
class DictionaryEntry:
    """
    单个词条的不可变描述。

    - name：标准名（大小写保留原样，匹配时按需小写化）
    - entity_type：顶层实体类型，严格限定为 ticker / chain / narrative / kol 之一
                   （加 project 是 Entity_Extractor 的正则路径独占，不走词典）
                   **这个值由所在文件决定**，不能被 YAML 的条目配置覆盖（Req 4.3）
    - category：细分类，例如 ticker 下的 layer1 / defi / meme / stablecoin
                Phase 1 只存不用，Phase 2+ 做叙事分组时会派上用场
    - aliases：包含标准名本身 + YAML 里声明的 aliases/keywords，全部小写
                匹配时统一用小写做 substring 比较
    - weight：仅 kol 类型使用；其他类型固定 1.0
    """

    name: str
    entity_type: str
    category: str | None
    aliases: tuple[str, ...]
    weight: float = 1.0


@dataclass(frozen=True)
class Dictionaries:
    """
    全局词典快照。

    - tickers / chains / narratives / kols：按 entity_type 分组的 name → entry 映射
    - alias_index：所有 alias（小写）→ (standard_name, entity_type) 的反查索引
                   消费方（Entity_Extractor）只需要一次字典查找就能拿到实体类型
    """

    tickers: Mapping[str, DictionaryEntry]
    chains: Mapping[str, DictionaryEntry]
    narratives: Mapping[str, DictionaryEntry]
    kols: Mapping[str, DictionaryEntry]
    alias_index: Mapping[str, tuple[str, str]] = field(repr=False)


# ---------------------------------------------------------------------------
# 加载主流程
# ---------------------------------------------------------------------------


def load_dictionaries(base_dir: Path) -> Dictionaries:
    """
    从 base_dir 加载四个 YAML 词典文件，返回不可变 Dictionaries 对象。

    失败策略：
    - 文件不存在 → RuntimeError（明确指出缺哪个文件）
    - YAML 语法错 → yaml.YAMLError 向上抛（保留原始异常便于定位）
    - 必填字段缺失（条目值是 None 或空 dict 且无 type/aliases）→ RuntimeError
    - 空文件（YAML 合法但内容为 null / {} ）→ WARN，继续加载
    - 跨文件同名 / 别名冲突 → RuntimeError

    INFO 日志：
    - 每个文件加载完成后输出词条数
    - 全部加载完成后输出 alias_index 总大小
    """
    tickers = _load_one(base_dir / "tickers.yaml", entity_type="ticker")
    chains = _load_one(base_dir / "chains.yaml", entity_type="chain")
    narratives = _load_one(base_dir / "narratives.yaml", entity_type="narrative")
    kols = _load_one(base_dir / "kols.yaml", entity_type="kol")

    # 跨文件同名检查（Req 3.8）
    _check_cross_file_conflicts(
        [
            ("tickers.yaml", tickers),
            ("chains.yaml", chains),
            ("narratives.yaml", narratives),
            ("kols.yaml", kols),
        ]
    )

    # 构建别名反查索引 + 别名冲突检查
    alias_index = _build_alias_index(tickers, chains, narratives, kols)

    logger.info(
        "词典加载完成：tickers={} chains={} narratives={} kols={} aliases={}",
        len(tickers),
        len(chains),
        len(narratives),
        len(kols),
        len(alias_index),
    )

    return Dictionaries(
        tickers=MappingProxyType(dict(tickers)),
        chains=MappingProxyType(dict(chains)),
        narratives=MappingProxyType(dict(narratives)),
        kols=MappingProxyType(dict(kols)),
        alias_index=MappingProxyType(alias_index),
    )


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _load_one(path: Path, entity_type: str) -> dict[str, DictionaryEntry]:
    """
    加载单个 YAML 文件，返回 name → DictionaryEntry 字典。

    - 文件不存在：raise RuntimeError
    - YAML 语法错：yaml.YAMLError 向上抛（由 safe_load 本身抛）
    - 空文件（内容是 None 或空 dict）：WARN + 返回空 dict（Req 3.6）
    - 条目必填字段缺失：raise RuntimeError（Req 3.5）
    """
    if not path.exists():
        raise RuntimeError(
            f"词典文件不存在：{path}。请确认 dictionaries/ 目录下四个 YAML 文件齐全。"
        )

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)  # 语法错直接抛 yaml.YAMLError

    # 空文件：{} 或 null 都被 safe_load 解析为 None 或空 dict
    if raw is None or raw == {}:
        logger.warning("词典文件为空：{}（允许启动，但该类实体无法被识别）", path)
        return {}

    if not isinstance(raw, dict):
        raise RuntimeError(
            f"词典文件 {path} 顶层必须是 dict（mapping），实际：{type(raw).__name__}"
        )

    result: dict[str, DictionaryEntry] = {}
    for name, cfg in raw.items():
        # 必填字段校验（Req 3.5）
        # cfg 可以是空 dict（条目无任何配置，只有名字）——这是允许的
        # 但 cfg 是 None（YAML 里写 `BTC:` 后没值）意味着条目写错了，raise
        if cfg is None:
            raise RuntimeError(
                f"词典文件 {path} 的条目 '{name}' 值为空（YAML 里看起来是 `{name}:` 后没内容）。"
                f"请至少写成 `{name}: {{}}` 或明确标注 type/aliases。"
            )
        if not isinstance(cfg, dict):
            raise RuntimeError(
                f"词典文件 {path} 的条目 '{name}' 值必须是 dict，"
                f"实际：{type(cfg).__name__}（{cfg!r}）"
            )

        # aliases 与 keywords 同义（narrative 类型习惯用 keywords）
        raw_aliases = cfg.get("aliases") or cfg.get("keywords") or []
        if not isinstance(raw_aliases, list):
            raise RuntimeError(
                f"词典文件 {path} 条目 '{name}' 的 aliases/keywords 必须是列表，"
                f"实际：{type(raw_aliases).__name__}"
            )

        # aliases 元组：标准名 + 声明的别名，全部小写去重
        aliases_list = [str(name).lower()] + [str(a).lower() for a in raw_aliases]
        aliases_tuple = tuple(dict.fromkeys(aliases_list))  # 保序去重

        result[str(name)] = DictionaryEntry(
            name=str(name),
            # ★ entity_type 由所在文件决定（参数传入），不从 YAML 读，
            #   保证 Req 4.3 的五类约束硬落地
            entity_type=entity_type,
            # YAML 里的 `type` 字段改读作 category（细分类，Phase 1 只存不用）
            category=(str(cfg["type"]) if "type" in cfg else None),
            aliases=aliases_tuple,
            weight=float(cfg.get("weight", 1.0)),
        )

    logger.info("加载 {}：{} 条词条", path.name, len(result))
    return result


def _check_cross_file_conflicts(
    files: list[tuple[str, dict[str, DictionaryEntry]]]
) -> None:
    """
    检查不同文件之间是否定义了同名的 entity。

    例如 tickers.yaml 有 BTC，chains.yaml 也定义 BTC → raise。
    """
    seen: dict[str, str] = {}  # name → 来源文件名
    for filename, entries in files:
        for name in entries:
            if name in seen:
                raise RuntimeError(
                    f"词典跨文件同名冲突：'{name}' 同时定义在 "
                    f"{seen[name]} 和 {filename}，请只在一处保留。"
                )
            seen[name] = filename


def _build_alias_index(
    *all_dicts: dict[str, DictionaryEntry],
) -> dict[str, tuple[str, str]]:
    """
    构建 alias（小写）→ (standard_name, entity_type) 反查索引。

    别名冲突（两个不同实体声明了同一个 alias）→ raise 明确错误指出冲突双方。
    """
    alias_index: dict[str, tuple[str, str]] = {}
    for d in all_dicts:
        for name, entry in d.items():
            for alias in entry.aliases:
                if alias in alias_index:
                    existing_name, existing_type = alias_index[alias]
                    if existing_name == name:
                        continue  # 自己跟自己别名相同，允许
                    raise RuntimeError(
                        f"词典别名冲突：别名 '{alias}' 同时指向 "
                        f"({existing_name}, {existing_type}) 和 "
                        f"({name}, {entry.entity_type})，请在 YAML 里调整其一。"
                    )
                alias_index[alias] = (name, entry.entity_type)
    return alias_index


__all__ = [
    "DictionaryEntry",
    "Dictionaries",
    "load_dictionaries",
]
