from __future__ import annotations

"""
原始帖子预过滤(rule-based,无 LLM 成本)。

目的:
- 在送入 LLM 之前,把明显噪音(纯口水、跑题、过短)直接丢弃
- 保留所有"看得到信号"的帖子($X / 币名 / 长帖+数字 / 财经百分比等)
- 让 LLM 处理的密度更高,token 不再浪费在"梭就完事了"这种内容上

设计原则:
- 高召回 优先 高精确:宁可多放过几条噪音让 LLM 处理,也不要错杀真信号
- 规则简单且可读:只用 re + 词典,任何人扫一眼能判断"这条会被怎么处理"
- 词典外置(模块级常量),需要扩词随时改

规则(优先级自上而下):
- 强信号优先(命中即保留,不受长度约束):
  - A.  含 $X(X 可为英文 / 中文 / 数字),覆盖 $BTC / $币安人生 / $95000
  - B.  命中币名/项目/美股 ticker 词典(英文做"非字母数字边界"匹配,
        修复 Python \b 在 中英混排("类似BNB的") 时失效的问题)
- 强丢弃(无强信号 + 长度/噪音命中):
  - D.  长度 < 20 字
  - E.  长度 < 35 字 且 命中"纯情绪"模式(梭哈/亏麻/求带 等)
- 弱保留(无强信号 + 长度/含数字命中):
  - C.  长度 ≥ 50 字 且 含数字(覆盖宏观/财经/链上长帖)
  - F.  长度 ≥ 25 字 且 含数字 且 含 %(覆盖 "央行降准 43.5%" 类短宏观新闻)
- 其他默认丢弃。

强信号优先于长度的原因:像 "$BTC 突破 $73000"(14 字)这种很短但完全 actionable 的内容,
不应被 D 规则误杀。$X / 币名词典本身已经是高精确的信号,几乎不存在 false positive。
"""

import re
from dataclasses import dataclass, field


# ---------------- A: $X 检测 ----------------
# 接受三种 body:英文起头、中文起头、纯数字(可带 . 与 B/M/K 后缀)
# 用 (?![A-Za-z0-9一-鿿]) 而不是 \b 避免 "$币安人生总" 这种贴字边界失败
_DOLLAR_RE = re.compile(
    r"\$([A-Za-z一-鿿][A-Za-z0-9一-鿿]*|\d[\d.]*[BMK]?)"
)

# ---------------- B: 币名 / 项目 / ticker 词典 ----------------
# v1.1 变更（Task 2.2）：词典数据迁移到 dictionaries/tickers.yaml，
# 原 `COIN_KEYWORDS_EN` / `COIN_KEYWORDS_ZH` 两个模块级常量已删除。
# prefilter 在模块加载时从 `dictionaries.get_dictionaries()` 取数据，
# 动态构造下方两个正则 `_EN_COIN_RE` / `_ZH_COIN_RE`。
#
# 构造规则（保持与原版本的匹配行为 parity）：
# - 英文正则：取所有 ticker 的**标准名**（DictionaryEntry.name）中 ASCII 的部分
#   不扫 aliases 里的英文（如 "bitcoin"），避免改变原 prefilter 行为；需要扫的话
#   在 Entity_Extractor (Task 2.3b) 层做 substring match
# - 中文正则：取所有 ticker 的 aliases 中含中文字符的
#
# 匹配语义：
# - 英文用 (?<![A-Za-z0-9])(?![A-Za-z0-9]) 替代 \b，修复中英混排时 \b 失效问题
# - 中文整段子串匹配即可（没有词边界概念）
#
# 运行时不重读（Req 3.5）：如果测试想换词典，需要 get_dictionaries.cache_clear()
# 后 importlib.reload(services.prefilter) 重新触发模块加载。
_CJK_RE = re.compile(r"[一-鿿]")

# 空词典时的"永不命中"正则：`(?!x)x` 是自相矛盾的 pattern，任何输入都不命中；
# 比起让 pattern 变成 `()` 空组（会在每个位置零宽命中）更安全。
_NEVER_MATCH_RE = re.compile(r"(?!x)x")


def _build_coin_regexes() -> tuple[re.Pattern[str], re.Pattern[str]]:
    """
    从 `dictionaries.get_dictionaries()` 构造英文 / 中文两个币名正则。

    英文：ticker 标准名里的 ASCII name（如 BTC / ETH / BNB），non-alnum 边界 + 不分大小写
    中文：ticker aliases 里含中文字符的（如 比特币 / 以太坊 / 币安），子串直匹
    空词典：返回永不命中的哨兵正则
    """
    # 延迟 import：避免模块加载时出现循环依赖（理论上现在没有，但保险）
    from dictionaries import get_dictionaries

    dicts = get_dictionaries()
    tickers = dicts.tickers

    # 英文：取 ticker 的标准名（已是 ASCII 大写，loader 没 .lower() 过 name 字段）
    en_names = sorted(
        (e.name for e in tickers.values() if e.name.isascii()),
        key=len,
        reverse=True,
    )

    # 中文：取所有 aliases 里含中文字符的（loader 已 .lower()，中文不受影响）
    zh_aliases = {
        a
        for e in tickers.values()
        for a in e.aliases
        if _CJK_RE.search(a)
    }
    zh_sorted = sorted(zh_aliases, key=len, reverse=True)

    en_re = (
        re.compile(
            r"(?<![A-Za-z0-9])(" + "|".join(en_names) + r")(?![A-Za-z0-9])",
            re.IGNORECASE,
        )
        if en_names
        else _NEVER_MATCH_RE
    )
    zh_re = (
        re.compile("|".join(zh_sorted)) if zh_sorted else _NEVER_MATCH_RE
    )
    return en_re, zh_re


# 模块加载时一次性构造（Req 3.5：启动加载，运行时不重读）
_EN_COIN_RE, _ZH_COIN_RE = _build_coin_regexes()

# ---------------- B2: 合约地址（Task 2.3a 新增） ----------------
# 产出 entity_type='project' 的 Entity，confidence=0.95（正则命中）。
# 本区块仅新增辅助函数 `_extract_regex_entities`，不动 classify 的 keep/drop 规则；
# 整合进 classify 的工作在 Task 2.3b 做。
#
# EVM 地址：以太坊及其 L2 的合约/钱包地址，固定 `0x` + 40 位十六进制
# - 不校验 checksum（EIP-55），避免把合法但小写的地址误杀
# - 用 (?<![a-fA-F0-9])(?![a-fA-F0-9]) 边界防止"0xabcde...f0123456" 这种超长字符串
#   把前 40 位抠出来当成地址
_EVM_ADDR_RE = re.compile(
    r"(?<![a-fA-F0-9])0x[a-fA-F0-9]{40}(?![a-fA-F0-9])"
)

# Solana 地址：base58 编码的 32~44 字符，包含 ed25519 公钥和 PDA
# - base58 字母表：`1-9A-HJ-NP-Za-km-z`（排除 0 / O / I / l 避免视觉歧义）
# - 长度范围 32~44（典型 ed25519 = 44，部分 PDA = 43 或更短）
# - 用 (?<![1-9A-HJ-NP-Za-km-z])(?![1-9A-HJ-NP-Za-km-z]) 边界
#
# 注意：base58 字符集与普通英文字符大面积重叠，容易误伤长英文单词。
# Phase 1 接受这个 false positive 率（测试里会单独加用例观察），Phase 3 如果
# 发现误命中太多，可以加"全字母/全数字不算地址"的启发式过滤，或校验 base58 编码合法性。
_SOLANA_ADDR_RE = re.compile(
    r"(?<![1-9A-HJ-NP-Za-km-z])"
    r"[1-9A-HJ-NP-Za-km-z]{32,44}"
    r"(?![1-9A-HJ-NP-Za-km-z])"
)


def _extract_regex_entities(content: str) -> list["Entity"]:
    """
    仅用正则从 content 里抽取实体，confidence 固定 0.95。

    返回的 Entity 顺序：$TICKER → EVM → Solana（按源文本中出现位置的相对稳定性，
    不过 classify 最终会做去重，顺序只影响 Phase 2 的日志可读性）。

    **不做词典命中**（那部分在 Task 2.3b 里整合到 classify）。
    **不做单条消息内去重**（同上）——所以调用方拿到的列表可能含重复的 name。

    抽取规则：
    - $TICKER：复用现有 `_DOLLAR_RE`；ticker 标准化为大写（原硬编码 COIN 也是大写）
    - EVM 地址：0x + 40 hex，entity_type='project'
    - Solana 地址：base58 32~44 字符，entity_type='project'

    参数：
    - content：原始文本（已 strip）。空字符串时返回 `[]`。
    """
    if not content:
        return []

    entities: list[Entity] = []

    # 1. $TICKER 正则命中
    # `_DOLLAR_RE` 有两个分支：字母/中文起头（真 ticker）和数字起头（价格 $95000 / $0.0837）。
    # 价格分支只用于 keep/drop 决策（$ + 数字仍是强信号，让消息被保留），
    # **不应**作为 ticker 实体抽取——否则 "0.0837" / "95000" / "1.71" 这种数字
    # 会冒充 ticker 进 entity_mentions，污染 hotness 榜单。
    # Phase 2.8 修复：跳过 group(1) 首字符为数字的匹配，只保留真 ticker。
    for m in _DOLLAR_RE.finditer(content):
        captured = m.group(1)
        if captured and captured[0].isdigit():
            # 价格 / 数量值，不是 ticker，跳过
            continue
        ticker = captured.upper()
        entities.append(
            Entity(name=ticker, entity_type="ticker", confidence=0.95)
        )

    # 2. EVM 合约地址
    for m in _EVM_ADDR_RE.finditer(content):
        # 地址保持原大小写（EIP-55 checksum 依赖大小写，运维若要校验 checksum 需要原形）
        entities.append(
            Entity(name=m.group(0), entity_type="project", confidence=0.95)
        )

    # 3. Solana 合约地址
    for m in _SOLANA_ADDR_RE.finditer(content):
        addr = m.group(0)
        # 避免与 EVM 命中冲突：EVM 地址（0x 开头）会被 Solana 正则的第一段不小心吞下去
        # 因为 base58 字母表也包含 0、x 以外的大部分字符
        # 策略：若 addr 以 "0x" 开头且能被 _EVM_ADDR_RE 完整命中，跳过
        if addr.startswith("0x"):
            continue
        entities.append(
            Entity(name=addr, entity_type="project", confidence=0.95)
        )

    return entities

# ---------------- C / F: 数字 与 百分比 ----------------
_NUMBER_RE = re.compile(r"\d")
_PERCENT_RE = re.compile(r"[%％]")

# ---------------- E: 短帖 + 纯情绪 → 直接丢 ----------------
_NOISE_PATTERNS: list[str] = [
    r"梭就", r"梭哈", r"亏麻", r"抄在山腰", r"我是狗",
    r"求带", r"跟单", r"扛住", r"睡不着",
    r"兄弟们觉得", r"大家都在干嘛", r"行情这么淡",
    r"今天又是.*亏", r"什么时候能回本",
    r"完了完了", r"踏空",
    r"WAGMI", r"GM\s*[☕️]?$",
    r"韭菜的命运",
    r"看来又", r"刚刚梭",
    r"这一波.*抄.*山腰",
]
_NOISE_RE = re.compile("|".join(_NOISE_PATTERNS), re.IGNORECASE)

# ---------------- 阈值 ----------------
_HARD_DROP_LEN = 20    # 短于此一律丢
_SHORT_NOISE_LEN = 35  # 短于此 + 命中噪音模式 → 丢
_LONG_KEEP_LEN = 50    # 不低于此 + 含数字 → 自动保留
_MEDIUM_KEEP_LEN = 25  # 不低于此 + 含数字 + 含 % → 保留(短宏观新闻)


@dataclass(frozen=True)
class Entity:
    """
    单条消息里抽取出来的实体标签。

    由 `classify` 产出并挂在 `FilterDecision.entities` 上；
    下游 `EntityExtractor` 会把它落库到 `entity_mentions` 表。

    字段约束（对应 requirements.md Req 4.3，Phase 1 严格限定）：
    - name：标准名（ticker 保留大写如 "BTC"；chain/narrative 保留原形如 "Base"）
    - entity_type：必须是 `ticker / chain / narrative / project / kol` 之一
    - confidence：只允许 1.0（词典命中）或 0.95（正则命中）两种取值
    """

    name: str
    entity_type: str
    confidence: float


@dataclass(frozen=True)
class FilterDecision:
    """
    单条帖子的过滤决策 + 实体标签。

    reason 字段同现有日志/打分阶段的归因口径不变（例如 "A:$symbol"、"D:length<20"）。

    entities 字段带默认值 `field(default_factory=list)`（requirements.md Req 4.1）：
    - **关键兼容性**：老代码 `FilterDecision(True, "A:$symbol")` / `FilterDecision(False, "D:length<20")`
      这种两参数构造方式必须继续可用，不产生 TypeError。历史上 `Level1Service.split()`
      的老调用方依赖这个兼容性（已于 2026-05 随老链路一并淘汰，但保留无副作用）。
    - Task 2.3a/2.3b 里 `classify` 的新实现会显式传入实体列表；老的短路 return
      可以直接两参数返回，自动得到 `entities=[]`。
    """

    keep: bool
    reason: str
    entities: list[Entity] = field(default_factory=list)


def _merge_entities_dedup(
    regex_hits: list["Entity"],
    dict_hits: list["Entity"],
) -> list["Entity"]:
    """
    把正则实体和词典实体合并，按 entity.name 做单消息内去重。

    规则（requirements.md Req 4.4）：同一 name 被正则与词典同时命中时，
    **词典命中优先**（confidence=1.0 > 0.95），entity_type 以词典为准。

    去重后顺序：先 dict_hits 的插入顺序，再 regex_hits 里尚未出现的名字
    （测试用到稳定顺序断言时有用）。
    """
    seen: set[str] = set()
    merged: list[Entity] = []

    # 词典命中优先进入结果
    for e in dict_hits:
        if e.name in seen:
            continue
        seen.add(e.name)
        merged.append(e)

    # 正则命中：名字没被词典抢走的才保留
    for e in regex_hits:
        if e.name in seen:
            continue
        seen.add(e.name)
        merged.append(e)

    return merged


def _extract_dict_entities(content: str) -> list["Entity"]:
    """
    根据 `dictionaries.alias_index` 做 substring match，返回 confidence=1.0 的实体。

    实现细节：
    - 对 content 做一次 lower() 拷贝；`alias_index` 的 key 全部小写（loader 保证）
    - 每个 alias 做一次 `in` 判断，O(|dict| × |text|)；Phase 1 词典约 60 条，
      单次 classify 耗时 < 1ms。词典规模扩到几千条时再考虑 Aho-Corasick
    - 同一标准名被多个 alias 命中只记一次（用 `seen` 去重）
    - entity.name 使用词典里的**标准名**（不是 alias），保证后续统计口径统一
    """
    if not content:
        return []

    from dictionaries import get_dictionaries

    dicts = get_dictionaries()
    c_lower = content.lower()

    entities: list[Entity] = []
    seen: set[str] = set()
    for alias_lower, (name, entity_type) in dicts.alias_index.items():
        if alias_lower in c_lower and name not in seen:
            seen.add(name)
            entities.append(
                Entity(name=name, entity_type=entity_type, confidence=1.0)
            )
    return entities


def classify(content: str) -> FilterDecision:
    """
    对单条帖子内容做 keep / drop 决策，同时产出实体标签。

    返回值（Task 2.3b 起）：
    - `keep` / `reason`：与 v1.0 行为完全一致（历史上老 `Level1Service` 只读这两个字段；
      已淘汰，但行为契约保留以便未来其他调用方继续走两元组）
    - `entities`：本条消息抽取到的实体列表；被 keep=False 的消息也会带上，
      但下游 L1 只对原版（非重复 + 非被过滤）消息消费，不会造成冗余写入

    抽取规则（requirements.md Req 4.1~4.4）：
    - 正则命中：$TICKER / EVM 地址 / Solana 地址 → confidence=0.95
    - 词典命中：所有 ticker/chain/narrative/kol 的 alias → confidence=1.0
    - 单条消息内按 name 去重，词典命中优先覆盖正则
    """
    c = (content or "").strip()
    n = len(c)

    # 实体抽取（独立于 keep/drop 决策，两边结果都带上）
    entities = _merge_entities_dedup(
        regex_hits=_extract_regex_entities(c),
        dict_hits=_extract_dict_entities(c),
    )

    # ------------ 强信号优先(即使很短也保留)------------
    if _DOLLAR_RE.search(c):
        return FilterDecision(True, "A:$symbol", entities)
    if _EN_COIN_RE.search(c) or _ZH_COIN_RE.search(c):
        return FilterDecision(True, "B:coin_dict", entities)

    # ------------ 强丢弃 ------------
    if n < _HARD_DROP_LEN:
        return FilterDecision(False, "D:length<20", entities)
    if n < _SHORT_NOISE_LEN and _NOISE_RE.search(c):
        return FilterDecision(False, "E:short_noise", entities)

    # ------------ 弱保留(长度+数字)------------
    has_digit = bool(_NUMBER_RE.search(c))
    if n >= _LONG_KEEP_LEN and has_digit:
        return FilterDecision(True, "C:long+num", entities)
    if n >= _MEDIUM_KEEP_LEN and has_digit and _PERCENT_RE.search(c):
        return FilterDecision(True, "F:mid+num+pct", entities)

    return FilterDecision(False, "default", entities)


def split(posts: list) -> tuple[list, list[tuple[object, str]]]:
    """
    把一批 posts(需有 .content 属性)切成 (kept, dropped_with_reason)。

    - kept:    通过过滤的帖子,顺序保持
    - dropped: [(post, reason), ...],顺序保持,reason 字段同 FilterDecision.reason
    """
    kept: list = []
    dropped: list[tuple[object, str]] = []
    for p in posts:
        decision = classify(getattr(p, "content", "") or "")
        if decision.keep:
            kept.append(p)
        else:
            dropped.append((p, decision.reason))
    return kept, dropped
