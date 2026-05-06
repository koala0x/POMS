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
from dataclasses import dataclass


# ---------------- A: $X 检测 ----------------
# 接受三种 body:英文起头、中文起头、纯数字(可带 . 与 B/M/K 后缀)
# 用 (?![A-Za-z0-9一-鿿]) 而不是 \b 避免 "$币安人生总" 这种贴字边界失败
_DOLLAR_RE = re.compile(
    r"\$([A-Za-z一-鿿][A-Za-z0-9一-鿿]*|\d[\d.]*[BMK]?)"
)

# ---------------- B: 币名 / 项目 / ticker 词典 ----------------
# 保持大写形式;匹配时不区分大小写。新加的词追加在合适分组下。
COIN_KEYWORDS_EN: list[str] = [
    # 主流币
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "AVAX", "DOT", "TRX", "LINK", "TON",
    "ATOM", "NEAR", "INJ", "SUI", "APT", "MATIC",
    # meme
    "DOGE", "PEPE", "SHIB", "WIF", "BONK", "DOGS", "FLOKI",
    # DeFi
    "AAVE", "UNI", "COMP", "LIDO", "PENDLE", "EIGEN", "EIGENLAYER", "CRV", "MKR",
    # L2 / 跨链
    "ARB", "OP", "BASE", "STARK", "ZKSYNC",
    # 隐私 / BRC20 / 老币
    "ZEC", "ORDI", "SATS", "FIL", "LTC", "BCH",
    # 美股 ticker(财经资讯里高频)
    "AAPL", "TSLA", "MSFT", "NVDA", "AMD", "META", "GOOG", "AMZN", "MSTR", "COIN",
    # 稳定币
    "USDT", "USDC", "DAI",
]

COIN_KEYWORDS_ZH: list[str] = [
    "比特币", "以太坊", "以太", "狗狗币", "波场", "币安", "微策略",
]

# 英文用 (?<![A-Za-z0-9])(?![A-Za-z0-9]) 替代 \b,避免中英混排("类似BNB的")时 \b 不触发
_EN_COIN_RE = re.compile(
    r"(?<![A-Za-z0-9])("
    + "|".join(sorted(COIN_KEYWORDS_EN, key=len, reverse=True))
    + r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)
# 中文整段子串匹配即可(不存在词边界问题)
_ZH_COIN_RE = re.compile("|".join(sorted(COIN_KEYWORDS_ZH, key=len, reverse=True)))

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
class FilterDecision:
    """单条帖子的过滤决策。reason 既用于日志,也方便打分阶段做归因。"""

    keep: bool
    reason: str


def classify(content: str) -> FilterDecision:
    """
    对单条帖子内容做 keep / drop 决策。

    见模块 docstring 的规则表。reason 与规则字母对应,便于日志聚合与排查。
    """
    c = (content or "").strip()
    n = len(c)

    # ------------ 强信号优先(即使很短也保留)------------
    if _DOLLAR_RE.search(c):
        return FilterDecision(True, "A:$symbol")
    if _EN_COIN_RE.search(c) or _ZH_COIN_RE.search(c):
        return FilterDecision(True, "B:coin_dict")

    # ------------ 强丢弃 ------------
    if n < _HARD_DROP_LEN:
        return FilterDecision(False, "D:length<20")
    if n < _SHORT_NOISE_LEN and _NOISE_RE.search(c):
        return FilterDecision(False, "E:short_noise")

    # ------------ 弱保留(长度+数字)------------
    has_digit = bool(_NUMBER_RE.search(c))
    if n >= _LONG_KEEP_LEN and has_digit:
        return FilterDecision(True, "C:long+num")
    if n >= _MEDIUM_KEEP_LEN and has_digit and _PERCENT_RE.search(c):
        return FilterDecision(True, "F:mid+num+pct")

    return FilterDecision(False, "default")


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
