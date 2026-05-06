from __future__ import annotations

"""
prefilter 单元测试。

覆盖:
- 各 keep 规则(A: $X、B: 词典、C: 长帖+数字、F: 中帖+数字+百分比)
- 各 drop 规则(D: 太短、E: 短帖噪音、default: 其他)
- 之前在真实数据里发现的边界 case:
  - $币安人生 / $95000 等"$ + 中文 / 数字"组合
  - "类似BNB的平台币" 中英混排导致 \b 失效
  - "央行降准 43.5%" 中等长度宏观新闻
- split() 切分顺序与字段。
"""

from dataclasses import dataclass

from services.prefilter import classify, split


@dataclass(frozen=True)
class _Post:
    """模拟 ORM Post 的最小鸭子类型,只需 .content 字段。"""

    content: str


# ------------------------------ keep 规则 ------------------------------


def test_keep_rule_a_english_dollar() -> None:
    d = classify("$BTC 突破 $73000，机构 ETF 持仓占比突破 26%")
    assert d.keep and d.reason == "A:$symbol"


def test_keep_rule_a_chinese_dollar() -> None:
    """$币安人生 这种"$ + 中文"在 v1 正则被漏,v2 必须保留。"""
    d = classify("$币安人生 这波不知道骗了多少多头上车，立帖为证")
    assert d.keep and d.reason == "A:$symbol"


def test_keep_rule_a_pure_digit_dollar() -> None:
    """$95000 这种"$ + 纯金额"也算硬数据信号。"""
    d = classify("MicroStrategy 再次增持 9 BTC，均价 $95000")
    # BTC 也在词典里,但 A 规则优先匹配,应返回 A。
    assert d.keep and d.reason == "A:$symbol"


def test_keep_rule_b_english_coin_after_chinese() -> None:
    """中英混排:'类似BNB的平台币',Python \\b 在中英之间不触发,必须用 lookaround 兜底。"""
    d = classify("就像CZ说的，加密市值还没一家公司高，类似BNB的平台币是首选")
    assert d.keep and d.reason == "B:coin_dict"


def test_keep_rule_b_chinese_coin_keyword() -> None:
    d = classify("Techub News 据 crypto.news 报道，CME 集团计划推出比特币波动率期货")
    assert d.keep and d.reason in ("B:coin_dict", "C:long+num")
    # 这条很长且含数字,理论上 A 不命中、B 命中比特币,应该是 B
    assert d.reason == "B:coin_dict"


def test_keep_rule_c_long_with_digit() -> None:
    """长帖+数字:不带 $ 与币名也保留,覆盖宏观/财经长文。"""
    text = "中国市场不再采购英伟达 H200 芯片，黄仁勋 5 月 5 日在米尔肯峰会发言："
    text += "中方绝无可能拿到最先进芯片，美国必须守住 AI 领域的绝对领先地位。"
    d = classify(text)
    assert d.keep and d.reason == "C:long+num"


def test_keep_rule_f_medium_with_pct() -> None:
    """27 字短宏观新闻,含 % 和数字,应被 F 规则捞回。"""
    d = classify("央行降准 43.5%，A 股普涨，港股科技板块表现强势")
    assert d.keep and d.reason == "F:mid+num+pct"


# ------------------------------ drop 规则 ------------------------------


def test_drop_rule_d_too_short() -> None:
    d = classify("今天又是亏麻的一天")
    assert not d.keep and d.reason == "D:length<20"


def test_drop_rule_e_short_noise() -> None:
    """长度在 [20, 35) + 命中噪音词 → E 丢弃。"""
    d = classify("梭就完事了，反正也亏不到哪去，韭菜命运罢了")  # 21 字附近
    assert not d.keep and d.reason == "E:short_noise"


def test_drop_default_offtopic() -> None:
    """跑题内容:无币名 / 无数字 / 不够长 → 默认丢。"""
    d = classify("17岁的女孩为什么要纹这么多身？只有胳膊不行，满身都是")
    assert not d.keep and d.reason == "default"


def test_drop_long_offtopic_no_signal() -> None:
    """长但完全跑题且无任何信号词 → default(其实命不到 C,因没数字)。"""
    d = classify("车主啊你可别瞎折腾啦，不然真得吃官司的，听我一句劝准没错")
    assert not d.keep
    # 不要求具体 reason,只要丢就行(可能是 default 也可能是 D 取决于长度)


# ------------------------------ split 行为 ------------------------------


def test_split_preserves_order_and_pairs_reasons() -> None:
    posts = [
        _Post("$BTC 突破 $73000"),                       # keep A
        _Post("梭哈了"),                                  # drop D
        _Post("ETH 现货 ETF 已通过，合约持仓 24h +2.0%"),  # keep B
        _Post("兄弟们觉得现在能进吗"),                     # drop D
    ]
    kept, dropped = split(posts)
    # 顺序保持
    assert [p.content for p in kept] == [posts[0].content, posts[2].content]
    # 丢弃带原因
    assert [c for _, c in dropped] == ["D:length<20", "D:length<20"]
    # post 引用应等同
    assert dropped[0][0] is posts[1]
    assert dropped[1][0] is posts[3]


def test_split_handles_empty_input() -> None:
    kept, dropped = split([])
    assert kept == []
    assert dropped == []


def test_split_handles_missing_content_attr_gracefully() -> None:
    """没有 content 属性 / content=None 不应炸,按 drop 处理。"""

    class Empty:
        pass

    kept, dropped = split([Empty(), _Post(None)])  # type: ignore[arg-type]
    assert kept == []
    assert len(dropped) == 2
