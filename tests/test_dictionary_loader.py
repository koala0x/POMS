from __future__ import annotations

"""
词典加载器 (`dictionaries/loader.py`) 单元测试。

全部使用 `tmp_path` fixture 构造临时 YAML 文件，不依赖项目里的真实
`dictionaries/*.yaml`，保证测试与实际词典内容解耦。

覆盖 requirements.md Req 3.5 / 3.6 / 3.7 / 3.8 以及 loader.py 的边界情况。
"""

import pytest
import yaml

from dictionaries.loader import (
    DictionaryEntry,
    Dictionaries,
    load_dictionaries,
)


# ---------------------------------------------------------------------------
# 测试辅助：构造一个"合法的四文件目录"，测试可按需覆盖其中任何一份
# ---------------------------------------------------------------------------


def _write(path, content: str) -> None:
    """把 content（YAML 字符串）写到 path。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _seed_valid_dir(base_dir) -> None:
    """
    写入一份最小合法的四文件骨架：
      - tickers.yaml 含 2 个 ticker（BTC / ETH）
      - chains.yaml / narratives.yaml / kols.yaml 皆为空
    """
    _write(
        base_dir / "tickers.yaml",
        """
BTC:
  type: layer1
  aliases: [比特币, bitcoin]
ETH:
  type: layer1
""",
    )
    _write(base_dir / "chains.yaml", "{}\n")
    _write(base_dir / "narratives.yaml", "{}\n")
    _write(base_dir / "kols.yaml", "{}\n")


# ---------------------------------------------------------------------------
# 正常加载
# ---------------------------------------------------------------------------


def test_load_valid_tickers_yaml(tmp_path) -> None:
    """
    正常加载：含 2 个 ticker + aliases 的 yaml。
    验证：
    - tickers 字典大小正确
    - entity_type 固定为 'ticker'（由文件决定，不受 YAML 的 type 字段影响）
    - YAML 的 type 字段被存到 category
    - aliases 包含标准名自身（小写）+ 声明的别名
    """
    _seed_valid_dir(tmp_path)

    dicts = load_dictionaries(tmp_path)
    assert isinstance(dicts, Dictionaries)

    assert set(dicts.tickers.keys()) == {"BTC", "ETH"}

    btc = dicts.tickers["BTC"]
    assert btc.name == "BTC"
    assert btc.entity_type == "ticker"  # 由文件决定，不是 layer1
    assert btc.category == "layer1"  # YAML 里的 type 现在是 category
    assert "btc" in btc.aliases
    assert "比特币" in btc.aliases
    assert "bitcoin" in btc.aliases

    # 没定义 aliases 的条目，至少要包含自己的小写名
    eth = dicts.tickers["ETH"]
    assert eth.aliases == ("eth",)


def test_empty_file_allows_startup(tmp_path, caplog) -> None:
    """
    Req 3.6：YAML 合法但内容为空（{} 或 null）时，允许启动，产生 WARN 日志。
    """
    # 故意让 chains.yaml 是空的 `{}`
    _seed_valid_dir(tmp_path)

    # 不应 raise
    dicts = load_dictionaries(tmp_path)

    # chains 确实是空
    assert len(dicts.chains) == 0
    assert len(dicts.narratives) == 0
    assert len(dicts.kols) == 0

    # tickers 还是有 2 条（互不干扰）
    assert len(dicts.tickers) == 2


def test_null_yaml_also_allowed(tmp_path) -> None:
    """YAML 解析出 None（空文件 / 只有注释）也算空内容，不应 raise。"""
    _seed_valid_dir(tmp_path)
    # 覆盖 chains.yaml 成纯注释（解析为 None）
    _write(tmp_path / "chains.yaml", "# only a comment\n")

    dicts = load_dictionaries(tmp_path)
    assert len(dicts.chains) == 0


def test_invalid_yaml_raises(tmp_path) -> None:
    """Req 3.5：YAML 语法错误时 raise（yaml.YAMLError）。"""
    _seed_valid_dir(tmp_path)
    # 故意写一个语法错误：未闭合的列表 + 缩进混乱
    _write(tmp_path / "tickers.yaml", "BTC:\n  type: [layer1\n  aliases: [a, b\n")

    with pytest.raises(yaml.YAMLError):
        load_dictionaries(tmp_path)


def test_missing_required_field_raises(tmp_path) -> None:
    """
    Req 3.5：必填字段缺失（条目值是 None，即 YAML 写了 `BTC:` 后无内容）→ raise。

    允许 `BTC: {}`（空 dict，条目本身合法但没任何配置），
    但不允许 `BTC:` 后面空置（yaml.safe_load 会解析为 None）。
    """
    _seed_valid_dir(tmp_path)
    # `BTC:` 后面什么都没有 → yaml 解析为 None
    _write(
        tmp_path / "tickers.yaml",
        """
BTC:
ETH:
  type: layer1
""",
    )

    with pytest.raises(RuntimeError, match="BTC"):
        load_dictionaries(tmp_path)


def test_non_dict_config_raises(tmp_path) -> None:
    """条目值不是 dict（而是字符串/列表）时抛 RuntimeError。"""
    _seed_valid_dir(tmp_path)
    _write(
        tmp_path / "tickers.yaml",
        """
BTC: just_a_string
""",
    )

    with pytest.raises(RuntimeError, match="必须是 dict"):
        load_dictionaries(tmp_path)


def test_aliases_must_be_list(tmp_path) -> None:
    """aliases 不是 list 时报错。"""
    _seed_valid_dir(tmp_path)
    _write(
        tmp_path / "tickers.yaml",
        """
BTC:
  type: layer1
  aliases: "比特币"
""",
    )

    with pytest.raises(RuntimeError, match="aliases/keywords 必须是列表"):
        load_dictionaries(tmp_path)


# ---------------------------------------------------------------------------
# 跨文件冲突
# ---------------------------------------------------------------------------


def test_cross_file_name_conflict_raises(tmp_path) -> None:
    """Req 3.8：tickers.yaml 和 chains.yaml 同时定义 BTC → raise。"""
    _seed_valid_dir(tmp_path)
    _write(
        tmp_path / "chains.yaml",
        """
BTC:
  aliases: []
""",
    )

    with pytest.raises(RuntimeError, match="跨文件同名冲突"):
        load_dictionaries(tmp_path)


def test_alias_conflict_raises(tmp_path) -> None:
    """
    两个不同 ticker 声明了同一个别名 → raise 明确错误，文案应包含冲突双方。
    """
    _seed_valid_dir(tmp_path)
    _write(
        tmp_path / "tickers.yaml",
        """
BTC:
  type: layer1
  aliases: [大饼]
ETH:
  type: layer1
  aliases: [大饼]
""",
    )

    with pytest.raises(RuntimeError, match="别名冲突"):
        load_dictionaries(tmp_path)


# ---------------------------------------------------------------------------
# alias_index 反查索引
# ---------------------------------------------------------------------------


def test_alias_index_lowercase(tmp_path) -> None:
    """alias_index 的所有 key 都应是小写。"""
    _seed_valid_dir(tmp_path)
    dicts = load_dictionaries(tmp_path)

    for alias in dicts.alias_index.keys():
        assert alias == alias.lower(), f"alias '{alias}' 未小写化"


def test_alias_index_reverse_lookup(tmp_path) -> None:
    """alias → (standard_name, entity_type) 反查正确。"""
    _seed_valid_dir(tmp_path)
    dicts = load_dictionaries(tmp_path)

    # 标准名自身小写
    assert dicts.alias_index["btc"] == ("BTC", "ticker")
    # 中文别名
    assert dicts.alias_index["比特币"] == ("BTC", "ticker")
    # 英文别名
    assert dicts.alias_index["bitcoin"] == ("BTC", "ticker")


def test_dictionaries_is_immutable(tmp_path) -> None:
    """
    Dictionaries 对象应该是不可变的：
    - mapping 用 MappingProxyType 包裹，不能 __setitem__
    - 修改应 raise TypeError
    """
    _seed_valid_dir(tmp_path)
    dicts = load_dictionaries(tmp_path)

    with pytest.raises(TypeError):
        dicts.tickers["FAKE"] = DictionaryEntry(  # type: ignore[index]
            name="FAKE",
            entity_type="ticker",
            category=None,
            aliases=("fake",),
        )
    with pytest.raises(TypeError):
        dicts.alias_index["fake"] = ("FAKE", "ticker")  # type: ignore[index]
