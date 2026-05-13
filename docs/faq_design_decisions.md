# 设计决策 FAQ

> 面向项目新参与者。把几个"看起来反直觉但其实有理由"的设计选择集中
> 说清楚，避免未来重复解释。
>
> 本文不讲 how（怎么跑/怎么调试），那是 `docs/operations_guide.md` 的职责；
> 本文讲 why（为什么这么设计）。

---

## 目录

1. [Q1：为什么让两条链路都跑，不能只跑新链路吗？](#q1)
2. [Q2：tickers.yaml 里这些币 / 股票要是都不是当下的热点怎么办？](#q2)

---

## <a id="q1"></a>Q1：为什么让两条链路都跑，不能只跑新链路吗？

**短答**：技术上完全可以，**现在**也已经默认只跑新链路了（见下面"当前实际配置"）。
但是"砍掉老链路的代码"和"关掉老链路的开关"是两件事——前者不建议做，后者已经做了。

### 先分清"两条链路到底各做啥"

| | 老链路（Level1Service / Level2Service） | 新链路（Phase 1，crypto-narrative-radar） |
|---|---|---|
| 输入 | 三源原始消息 | 三源原始消息 |
| 方法 | 调 Ollama LLM 把一批原文**写成中文摘要** | 纯统计：抽实体 + 算提及频次 + 算增长率 |
| 产出 | `summary_level1` / `summary_level2`：一段**中文自然语言**描述"最近发生了啥" | `hotness_snapshots`：**Top-20 实体排行榜**，告诉你"谁被讨论得最多" |
| 示例 | "BlackRock IBIT 单日净流入 23 亿美元创新高，SOL 链上交易量突破 52 亿美元..." | `rank=1 BTC count=87 growth_rate=12.3 ...` |
| 回答问题 | "过去这段时间发生了什么" | "现在谁最热" |

用 App 类比：老链路像"每日新闻摘要推送"，新链路像"热搜榜"。同一份原始数据
能出两种**不同形态的产品**，而不是新产品是老产品的升级版。

### 为什么一度设计成并行

Phase 1 规划时的四个考虑：

1. **产品形态不同**：不是谁替代谁，而是两个独立需求
2. **新链路需要生产验证**：Phase 1 新写的，还没经过实际数据的检验（需过 Gate 1 的 7 条指标，见 `docs/gate1_checklist.md`）
3. **老链路有契约**：可能有其他下游系统在读 `summary_level1 / summary_level2` 两张表
4. **能力差距**：Phase 1 新链路目前没用 LLM，只产数字榜单，不产语义摘要；产品能力上是"子集"不是"超集"

### 但是——现在项目决定先聚焦新链路

实际情况是：

1. 老链路本身也不成熟，不急着用
2. 项目重心确定在"实体热度排行榜"这个产品形态
3. 保留老链路会占 Ollama 资源、拖慢整体进度

所以 2026-05 我们做了折中方案：**加开关，不删代码**。

### 当前实际配置

打开 `config/settings.py`，找到：

```python
# 5.1 老链路总开关
disable_legacy_pipeline: bool = True
```

- **`True`（默认）**：启动时**跳过**老链路所有初始化（Ollama 客户端、Level1/Level2 Service、原始表 repo），
  Jobs 的 `level1_services` / `level2_services` 都传空列表，worker 只迭代新链路的 3 个 service
- **`False`**：老 + 新并行跑（回到 Phase 1 刚上线时的行为）

这个开关的工程价值：

- **零代码删除** → 回滚成本为 0。哪天 Phase 2 要在排行榜基础上加 LLM 简报时，
  老链路的经验（prompt 模板、Ollama 客户端、错误处理）可以直接复用
- **关掉即零成本** → `True` 时 Ollama 连接都不建，CPU/内存/启动时间都省了
- **改一行就能切换** → 改 `True/False` 重启，15 秒搞定

### 什么时候才真的"删掉老链路代码"

当且仅当以下全部满足：

- [ ] 确认没有任何下游系统还在读 `summary_level1 / summary_level2`
- [ ] Phase 2 / 3 规划确定不再复用老链路的组件（prompt、ollama_client 等）
- [ ] 新链路通过 Gate 1 且稳定运行至少几周

现在都不满足，所以"代码保留 + 开关关闭"是最稳的状态。

### 一句话结论

**"并行跑"是过去式**——现在只跑新链路。**"完全删掉老链路"是未来式**——等
Phase 2 稳定后再考虑。现阶段的"代码保留 + 开关关闭"是一个有意识的中间
状态，给未来留灵活性。

---

## <a id="q2"></a>Q2：tickers.yaml 里的币 / 股票要是都不是当下热点怎么办？

**短答**：词典不是"必须命中清单"，而是"识别清单的补强"。系统有两条识别路径，
互相兜底。**词典外的热点**大概率能被抓到，**真正的盲区**是"叙事主题"类讨论。

### 系统怎么识别"谁在被讨论"

代码在 `services/prefilter.py` 的 `classify()` / `_extract_regex_entities()`。
两条识别路径：

#### 路径 1：正则识别（**不依赖词典**，自动捕获新币）

只要推文里出现下面任一模式，自动抽成实体：

| 模式 | 例子 | 识别结果 |
|---|---|---|
| `$XXX`（美元符号 + 任意字符） | `$WIF` / `$POPCAT` / `$MOODENG` / `$币安人生` | 抽为 `ticker`，confidence=0.95（词典外）或 1.0（词典内） |
| `0x` + 40 位十六进制 | `0xdAC17F958D2ee523a2206206994597C13D831ec7` | 抽为 `project`（EVM 合约地址），confidence=0.95 |
| base58 32~44 位字符串 | `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` | 抽为 `project`（Solana 合约地址），confidence=0.95 |

**关键**：只要用户规范地打出 `$XXX`，不管是老币、新币、蓝筹、土狗、meme，
系统都能抓到。**词典里没有 ≠ 识别不到**。

#### 路径 2：词典识别（补齐那些不带 `$` 的讨论）

有些讨论里不一定加 `$` 前缀，比如：
- "比特币今天跌了" → 词典里 `BTC` 的 aliases 含"比特币"，命中 BTC
- "大饼要破 7 万了" → aliases 含"大饼"，命中 BTC
- "BNB 这波起飞" → 裸写的英文 ticker 也能命中（因为 BNB 在词典里）

### 四种"热点不在词典里"的场景 & 各自的处理

#### 场景 A：新 meme 币爆火（$WIF、$MOODENG、$POPCAT ...）

**系统能自动抓到**，只要推文有 `$XXX` 规范写法。

运行时会发生什么：
1. `entity_mentions` 表写入 `entity='MOODENG' entity_type='ticker' confidence=0.95`
2. 该实体之前从未出现过 → `count_baseline=0`，短窗 count 突然飙升
3. `growth_rate` 非常高 + `is_new_entity=True`
4. 大概率**排进 Top-20**

用户看到一个从没听过的 ticker 冒出来 → **这正是系统想 surface 给你的新信号**。

**不需要你手动做任何事**。

#### 场景 B：讨论的是叙事 / 主题而不是具体币（AI agent、RWA、restaking ...）

这类讨论推文一般写成"AI 赛道起飞了"、"restaking 是下一个 narrative"，
**不会**写成 `$AI_AGENT`。

**系统会漏掉**——这是词典的盲区。

解决：填 `dictionaries/narratives.yaml`。例：

```yaml
AI_Agent:
  keywords: [AI agent, autonomous agent, agent protocol, ai16z, virtuals]
RWA:
  keywords: [real world asset, tokenized asset, RWA]
Restaking:
  keywords: [restaking, eigen layer, eigenlayer]
```

填完重启服务，系统会把这些短语作为 substring 匹配，命中即产出
`entity_type='narrative'` 的 mention。

#### 场景 C：老币讨论但没加 `$` 前缀，且该币不在词典里

比如"Immutable X 今天 +30%"——没加 `$IMX` 也不在词典里，系统会漏。

解决：把你关心的币加进 `tickers.yaml`：

```yaml
IMX:
  type: layer2
  aliases: [immutable, immutable x, imx]
```

#### 场景 D：美股 / 非加密资产的热点

这套系统定位是"**加密行业**热点"，不是全市场监控。若数据源里混着大量
美股讨论且你想追踪：

- 做法 1：把关心的股票 ticker 加进 `tickers.yaml`（反正正则已经能抓到
  `$NVDA` `$AAPL`，加词典主要是让"英伟达"、"老黄"这些中文别名也命中）
- 做法 2：**不管它**——股票和加密混在一个榜里往往信噪比下降，还不如专注

### 建议的工作流：不要一次性填全词典

**阶段 1：不动词典先跑一到两周**

保持 `tickers.yaml` 现在的 57 条，`chains` / `narratives` / `kols` 都空着。
跑起来观察每天整点的 Top-20。

会出现两种"不对劲"的情况：

| 观察到 | 含义 | 动作 |
|---|---|---|
| 热榜出现你没听过的 ticker（`$MOGGE` `$GIGGLE` 等） | 系统帮你发现新热点 | 去搜它是啥，这是系统的核心价值 |
| 榜上漏掉了你知道满屏在聊的话题（"AI"、"GameFi"）| 词典盲区（场景 B） | **记下来**，不当场改 |
| 榜上漏掉了你知道的老币 | 词典盲区（场景 C） | **记下来**，不当场改 |

**阶段 2：每周整理观察清单，一次性补词典**

周末花 30 分钟：
- 把这周观察到的"缺失点"归类
- 每个分类挑 3~5 个加进对应 yaml
- 提交一次 commit：`chore(dict): 补充 narratives/tickers 词条 W46`

**阶段 3：定期复盘**

每 2~4 周看一下词典的健康度：
- `grep -c "^[A-Z]" dictionaries/tickers.yaml` 统计条数
- 用 `entity_mentions` 的 confidence 分布看词典命中率：

```sql
SELECT confidence, count(*)
FROM entity_mentions
WHERE ts >= now() - INTERVAL '7 days'
GROUP BY confidence;
-- confidence=1.0 占比越高，说明词典越丰富；
-- 占比一直很低说明正则在单打独斗，词典需要补
```

### 为什么设计成这样（工程权衡）

三选一：

| 方案 | 优点 | 缺点 |
|---|---|---|
| A. 词典写死所有实体，严格清单制 | 没噪音 | 漏新热点，词典维护累死人 |
| B. 完全 AI 抽取（NER 模型） | 理论全面 | 噪音爆炸（"是"、"今天"都被当实体），Phase 1 说好不引入 LLM |
| **C. 正则强信号（`$XXX`）+ 词典补语义** ← 当前 | 新热点自动识别，词典做减法 | 需要约定俗成："`$` 前缀"需要用户习惯 |

选 C 的赌注是：**加密圈约定俗成用 `$` 前缀指代 ticker 是近乎普适的规范**。
实际观察下来这条规范在 Twitter / Discord 的加密讨论里非常牢固，覆盖了
80%+ 的"谁热"场景。剩下 20%（叙事、非标准写法）靠词典逐步补。

如果你运行一段时间发现这个 80/20 不成立（数据源用户根本不打 `$`）——
那得回头反思架构，可能要上方案 B 的 LLM 抽取。但这是 Phase 3 考虑的事。

### 具体例子走完全流程

场景：最近 Sui 上新 meme `$BLUB` 爆火，词典没有。

**t=0**：用户发推文"$BLUB 起飞了，SUI 生态今天很热"

**t=0+δ（几秒）**：抓取服务写入 `twitter_posts`

**t=0+30s**：worker 醒来，NormalizerService 把原文归一化到 `normalized_messages`

**t=0+60s**：EntityExtractor 调 `prefilter.classify()` →
- 正则命中 `$BLUB` → `Entity(name='BLUB', entity_type='ticker', confidence=0.95)`
- 词典命中 SUI（假设词典里有 SUI）→ `Entity(name='SUI', ..., confidence=1.0)`

两条 entity_mention 写入 `entity_mentions`，SlidingCounter 同步 add(BLUB)
和 add(SUI)。

**t=下一个 :00/:15/:30/:45**：HotnessService 触发
- BLUB 之前从没出现过 → `count_baseline=0`，短窗 `count_short=N`（N 取决于
  这段时间被提了多少次）
- `growth_rate = N / max(0, smoothing=2) = N/2`，非常高
- `is_new_entity = (baseline==0 && short>=5)` = True
- **大概率 rank=1 或前 5**

用户下次看榜：发现榜首是个叫 BLUB 的陌生名字，标着"新实体"。去搜一下、
决定要不要跟进。**系统做对了它该做的事——surface unknown signal**。

### 一句话结论

词典**不是**"必命中清单"，它是"给正则漏掉的地方打补丁"。`$XXX` 正则是
主力打工人，词典是辅助。你担心的"热点不在词典里会被漏掉"这件事：

- 对加密 ticker（场景 A）：**不会发生**，正则兜底
- 对合约地址（场景 A 变体）：**不会发生**，正则兜底
- 对叙事/主题（场景 B）：**会发生**，填 `narratives.yaml` 几行解决
- 对老币裸写（场景 C）：**会发生**，填 `tickers.yaml` 的 aliases 解决
- 对美股（场景 D）：**看你要不要追**，要就加进 tickers.yaml

所以现阶段别陷在"词典写不全"的焦虑里。**先跑，再观察，再填**，这是更健康
的节奏。
