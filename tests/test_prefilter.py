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
    """
    长帖+数字:不带 $ 与币名也保留,覆盖宏观/财经长文。

    注意：素材必须避开 tickers.yaml 里所有 ticker 的别名（中英文都要避），
    否则会优先命中 B 规则（B:coin_dict）。这里改用一段纯宏观主题、不涉及
    任何具体公司 / 币种的文本，保证只有 C 规则能匹配。
    """
    text = (
        "近日发布的最新经济数据显示，居民消费品价格指数同比下行 0.3 个百分点，"
        "餐饮、零售两个分项均出现连续 6 个月环比走弱的现象，市场普遍关注后续"
        "宏观刺激政策的出台节奏与力度，机构对全年增速预测下调约 0.5 个百分点。"
    )
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


# ============================================================================
# Task 2.4 新增：classify 实体抽取测试（requirements.md Req 4.1~4.4, 4.9）
# ----------------------------------------------------------------------------
# 约定：以下 case 全部依赖真实的 dictionaries/tickers.yaml
# （BTC 在词典里、XYZABC 不在词典里）。测试不 mock 词典，因为：
# 1. Task 1.4 已独立覆盖了 loader 逻辑
# 2. 这里要验证的正是"classify + 真实词典"的集成行为
# 3. 词典是启动期加载的单例，mock 它反而会让行为不贴近真实运行
# ============================================================================


from services.prefilter import Entity, FilterDecision  # noqa: E402


def test_classify_returns_entities_for_dollar_ticker() -> None:
    """
    Req 4.2 + 4.3：$BTC 应命中 1 个 Entity，BTC 在词典里所以 confidence=1.0。
    """
    d = classify("$BTC 突破了 73000 美元")
    btc = [e for e in d.entities if e.name == "BTC"]
    assert len(btc) == 1, f"BTC 应只出现一次，实际：{d.entities}"
    assert btc[0].entity_type == "ticker"
    assert btc[0].confidence == 1.0  # 词典命中


def test_classify_returns_entities_for_unknown_ticker() -> None:
    """
    Req 4.2：词典外的 $TICKER 走正则路径，confidence=0.95。
    XYZABC 不在 tickers.yaml 里，所以只由 _DOLLAR_RE 正则抽出。
    """
    d = classify("$XYZABC 是个新币，看看能不能拉")
    xyz = [e for e in d.entities if e.name == "XYZABC"]
    assert len(xyz) == 1, f"XYZABC 应被抽出，实际：{d.entities}"
    assert xyz[0].entity_type == "ticker"
    assert xyz[0].confidence == 0.95  # 正则命中


def test_classify_dollar_price_not_treated_as_ticker() -> None:
    """
    Phase 2.8 回归：$0.0837 / $95000 / $1.5B 这种"$ + 数字"是价格，
    不应被当成 ticker 抽进 entities（否则 hotness 榜会被价格污染成 entity）。

    背景：_DOLLAR_RE 第二个分支 `\\d[\\d.]*[BMK]?` 是为了保留"提到价格"
    这种强信号让消息不被 keep/drop 过滤误杀；但**价格不是 ticker**。
    _extract_regex_entities 必须跳过首字符是数字的命中。
    """
    cases = [
        "BTC 跌到 $0.0837 真便宜",     # 小数价格
        "$95000 是阻力位",               # 整数价格
        "$1.5B 流动性进场",              # 带 B 后缀的金额
        "$0.07 也守不住 $0.0837 完了",   # 多个价格混排
        "$73K 来了",                     # K 后缀
    ]
    for text in cases:
        d = classify(text)
        # 提取所有 ticker 类型的 entity name
        ticker_names = [
            e.name for e in d.entities if e.entity_type == "ticker"
        ]
        # 任何 entity name 都不应该是纯数字开头（容忍带字母混合的特殊情况）
        for name in ticker_names:
            assert not name[0].isdigit(), (
                f"价格被当成 ticker 抽出！text={text!r} "
                f"误抽 ticker={name!r} 全部 entities={d.entities}"
            )
        # 消息本身仍应被 keep（保持 $ + 数字的强信号语义）
        assert d.keep is True, f"$ + 数字消息应被 keep，实际 {d}"


def test_classify_dedup_regex_and_dict_hit() -> None:
    """
    Req 4.4：同一 name 被正则与词典双命中时，只保留一条且 confidence=1.0。
    `$BTC 比特币` 同时：正则抽到 BTC (0.95) + 词典抽到 BTC (1.0) → 合并成 1 条 1.0。
    """
    d = classify("$BTC 比特币 稳了，今晚决胜")
    btc = [e for e in d.entities if e.name == "BTC"]
    assert len(btc) == 1, f"BTC 必须去重成 1 条，实际：{d.entities}"
    assert btc[0].confidence == 1.0, f"词典应覆盖正则，实际：{btc[0]}"


def test_classify_chinese_alias_maps_to_standard_name() -> None:
    """
    Req 4.2 + 4.4：中文别名（"比特币" / "以太坊"）抽取后应用英文标准名。
    保证 entity_mentions 统计口径统一——不会一会儿"比特币"一会儿"BTC"算两次。
    """
    d = classify("今天比特币涨了 3%，以太坊也稳了")  # F 规则 keep
    names = {e.name for e in d.entities}
    assert "BTC" in names, f"比特币 应映射到 BTC，实际 names={names}"
    assert "ETH" in names, f"以太坊 应映射到 ETH，实际 names={names}"
    # 且绝不能出现中文名本身作为 entity.name
    assert "比特币" not in names
    assert "以太坊" not in names


def test_classify_evm_address() -> None:
    """
    Req 4.2：0x 开头的 EVM 合约地址被抽成 project 类型，confidence=0.95。
    """
    d = classify(
        "USDT 合约地址是 0xdAC17F958D2ee523a2206206994597C13D831ec7，靠谱"
    )
    addrs = [e for e in d.entities if e.entity_type == "project"]
    assert len(addrs) == 1, f"预期 1 个 project 实体，实际：{d.entities}"
    assert addrs[0].name == "0xdAC17F958D2ee523a2206206994597C13D831ec7"
    assert addrs[0].confidence == 0.95


def test_classify_solana_address() -> None:
    """
    Req 4.2：base58 的 Solana 地址（32~44 字符）被抽成 project 类型。
    使用 Solana 官方 USDC 代币的合约地址，这是一个真实可验证的 base58 字符串。
    """
    sol = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    d = classify(f"USDC 在 Solana 上的合约是 {sol} 这个")
    sol_entities = [e for e in d.entities if e.name == sol]
    assert len(sol_entities) == 1
    assert sol_entities[0].entity_type == "project"
    assert sol_entities[0].confidence == 0.95


def test_classify_evm_not_confused_with_solana() -> None:
    """
    Task 2.3a 的关键边界：EVM 地址（0x 开头）不应被 Solana 正则二次命中。
    base58 字母表与 EVM hex 有重叠字符（a-f），所以需要显式保护。
    """
    evm = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
    d = classify(f"看看这个地址 {evm}，是 USDT 的")
    evm_hits = [e for e in d.entities if e.name.startswith("0x")]
    sol_hits = [
        e for e in d.entities
        if e.entity_type == "project" and not e.name.startswith("0x")
    ]
    assert len(evm_hits) == 1
    assert len(sol_hits) == 0, (
        f"EVM 地址不应被 Solana 正则再次捕获，实际额外命中：{sol_hits}"
    )


def test_classify_empty_entities_still_returns_decision() -> None:
    """
    Req 4.1：FilterDecision 即使 entities 为空也要正常返回（不抛错）。
    空字符串 → D:length<20，且 entities=[]。
    """
    d = classify("")
    assert d.keep is False
    assert d.reason == "D:length<20"
    assert d.entities == []


def test_classify_drops_message_still_carries_entities() -> None:
    """
    Task 2.3b 设计决策：被 drop 的消息也要带上 entities（便于 Phase 2 debug）。
    下游 EntityExtractor 自己会用 is_duplicate 过滤，不会造成冗余写入。
    """
    # 这条很短但包含 $BTC → A 规则 keep，改个场景：长但跑题 + 含 $FAKE 假代币
    d = classify("其实吧我觉得 $FAKE 这币不咋样别冲，风险太大完了完了肯定归零")
    # $FAKE 正则会命中 ticker 抽出
    # keep 判断：有 $ 信号 → A 规则 keep
    assert d.keep is True
    assert d.reason == "A:$symbol"
    fake = [e for e in d.entities if e.name == "FAKE"]
    assert len(fake) == 1, f"FAKE 应被抽出，实际：{d.entities}"


def test_filter_decision_backward_compat() -> None:
    """
    Req 4.9：两参数构造 FilterDecision(True, 'A') 必须继续可用，
    entities 字段带默认空列表，不 TypeError。
    """
    d = FilterDecision(True, "A:$symbol")  # 老代码风格
    assert d.keep is True
    assert d.reason == "A:$symbol"
    assert d.entities == []


def test_filter_decision_default_entities_are_independent() -> None:
    """
    field(default_factory=list) 行为验证：两次空构造返回独立 list，
    不是共享引用。避免"改了一个 entity 污染另一个 FilterDecision"。
    """
    d1 = FilterDecision(False, "X")
    d2 = FilterDecision(False, "Y")
    assert d1.entities is not d2.entities


def test_classify_entity_type_hard_constrained_to_five(
) -> None:
    """
    Req 4.3：Phase 1 entity_type 必须是 ticker / chain / narrative / project / kol 之一。
    这里抽检一批实际 classify 输出的 entity_type，确认全部落在白名单内。
    """
    allowed = {"ticker", "chain", "narrative", "project", "kol"}
    inputs = [
        "$BTC 比特币 今天很强",                  # ticker
        "$XYZABC 这是啥",                       # ticker (regex-only)
        "0xdAC17F958D2ee523a2206206994597C13D831ec7 大家看看",  # project (EVM)
    ]
    for text in inputs:
        d = classify(text)
        for e in d.entities:
            assert e.entity_type in allowed, (
                f"非法 entity_type={e.entity_type!r} 来自 text={text!r}, entity={e}"
            )
