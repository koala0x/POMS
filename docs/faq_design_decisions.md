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
3. [Q3：程序跑起来后，我在哪儿看跑出来的数据？](#q3)
4. [Q4：narratives 和 tickers 怎么区分？我不知道某个名字该归到哪里](#q4)
5. [Q5："热点"和"提到最多"有什么区别？](#q5)
6. [Q6：Telegram 告警为什么冷却 60 分钟而不持久化？没收到告警怎么排查？](#q6)
7. [Q7：为什么需要三个时间窗口（1h / 6h / 24h），不能只看 1h 吗？](#q7)
8. [Q8：实时告警通道（Phase 2.4）和整点告警怎么协同？会不会刷屏？](#q8)
9. [Q9：实体共现网络（Phase 2.5）解决了什么问题？为什么用 PMI 而不是简单数共现次数？](#q9)

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


---

## <a id="q3"></a>Q3：程序跑起来后，我在哪儿看跑出来的数据？

**短答**：三个地方——**实时日志**（看在干活）、**数据库 3 张新表**（看具体数据）、
**`scripts/check_status.py` 一键自检脚本**（看整体状态）。

新链路所有"产出"都进 PostgreSQL，没有 UI 没有 Web 接口，全靠 SQL 或脚本看。

### 三个查看入口

#### 入口 1：实时日志（看系统是否在干活）

```bash
tail -f logs/service.log
```

每 30 秒会循环打出三行 INFO（对应新链路的三个环节）：

```
normalizer 本轮：扫描 X 条（tw=N bn=N dc=N）→ 写入 Y 条（重复 Z 条）
entity_extractor 本轮：处理 X 条消息 → 产出 Y 条实体提及
hotness window_end=2026-05-13 15:15:00 top_k=20 elapsed=0.5s
```

注意 `hotness window_end` 这行**只在每 15 分钟整点出现**（`:00 / :15 / :30 / :45`），
其他时间不出榜，是设计行为不是 bug。

退出 `tail -f` 用 Ctrl+C。

#### 入口 2：数据库（看具体数据）

新链路写 3 张表（其他 5 张是老链路 + 原始数据）：

| 表 | 内容 | 看点 |
|---|---|---|
| `normalized_messages` | 三源消息归一化 + SimHash 判重后的统一表 | 看系统消化了多少原始数据 |
| `entity_mentions` | 从消息里抽出来的实体（`$BTC` / `RWA` / 合约地址）| 看哪些实体被讨论 |
| `hotness_snapshots` | **最终产品** —— Top-20 排行榜，每 15 分钟一份 | 看现在谁最热 |

**连数据库的方式**：用图形客户端（推荐 [DBeaver](https://dbeaver.io/download/) /
TablePlus / Navicat），连接参数从 `config/settings.py` 抄：

```
Host:     192.168.1.219
Port:     5432
Database: all_new
User:     all_new
Password: 123qwe
```

**最常用的 3 条 SQL**：

```sql
-- 1. 看最新一份排行榜（你最想看的）
SELECT
  rank,
  entity,
  entity_type,
  count_short                              AS "1h提及次数",
  round(cast(count_baseline as numeric), 2) AS "基线时均",
  round(cast(growth_rate as numeric), 2)   AS "增长倍数",
  cross_source                             AS "跨源数",
  round(cast(final_score as numeric), 2)   AS "总分",
  is_new_entity                            AS "新冒头"
FROM hotness_snapshots
WHERE window_end = (
  SELECT max(window_end) FROM hotness_snapshots WHERE window_type='1h'
)
  AND window_type = '1h'
ORDER BY rank ASC;

-- 2. 看最近 1 小时被提到最多的 Top 20 实体
SELECT entity, entity_type, count(*) AS mentions
FROM entity_mentions
WHERE ts >= now() - INTERVAL '1 hour'
GROUP BY entity, entity_type
ORDER BY mentions DESC
LIMIT 20;

-- 3. 看三源原始表近期入库速率（判断上游抓取服务是否在跑）
SELECT 'twitter' AS src,        max(created_at), count(*) FILTER (WHERE created_at > now() - INTERVAL '10 minutes') AS last_10min FROM twitter_posts
UNION ALL
SELECT 'binance_square',        max(created_at), count(*) FILTER (WHERE created_at > now() - INTERVAL '10 minutes') FROM binance_square_posts
UNION ALL
SELECT 'discord',               max(created_at), count(*) FILTER (WHERE created_at > now() - INTERVAL '10 minutes') FROM discord_messages;
```

#### 入口 3：一键自检脚本（最省事）

```bash
.venv/bin/python scripts/check_status.py
```

输出三段：

1. **3 张表的总行数 + 最近更新时间** —— 一眼判断系统是否在干活
2. **最近 1 小时实体提及 Top 20** —— 看实体抽取阶段产出
3. **最新一份排行榜** —— 看最终产品

不用记 SQL，开发调试时常用。

### 排行榜各列含义（看不懂数字时回来对照）

| 列 | 含义 | 怎么用 |
|---|---|---|
| `rank` | 名次 1~20 | 排序就靠这个 |
| `entity` | 被讨论的对象（币符号 / 公链 / 合约地址 / 叙事名） | 这就是当前最热的话题 |
| `count_short` | 过去 1 小时被提到次数 | 绝对热度，越高越火 |
| `count_baseline` | 近 7 天每小时平均提及次数 | 当作"日常水位" |
| `growth_rate` | `count_short / max(baseline_per_hour, 2.0)` | **越大越"突然热"**，是 Phase 1 的核心信号 |
| `cross_source` | 在几个数据源出现过（1~3） | 越多说明"多平台共振"，更可信 |
| `final_score` | `growth_rate × (1 + 0.3 × (cross_source - 1))` | 最终排序依据 |
| `is_new_entity` | 基线 0 次、短窗 ≥5 次的"新冒头"实体 | True 时往往是新 meme / 新概念 |

### 一份"健康榜单"和"稀薄榜单"的对比

健康状态（数据流量充足时）：

```
rank  entity   count_short  growth  cross_source  score
 1    BTC          87         12.3        3          16.0   ← 跨源=3，多平台共振
 2    AI16Z        45          7.8        2           8.8
 3    RWA          32          5.1        2           5.7
```

稀薄状态（你 2026-05-13 15:15 那份榜单）：

```
rank  entity   count_short  growth  cross_source  score
 1    BTC           3         1.26        1          1.26   ← 跨源全是 1
 2    OP            2         1.00        1          1.00
 3    ETH           2         0.73        1          0.73
```

**`cross_source` 全是 1 是个强信号**：说明数据基本只来自一个源（多半是
Twitter）。要么上游 Binance / Discord 抓取服务没启动，要么三源数据
体量本身就不均衡。这不影响系统功能，但榜单质量会打折。

### 排行榜不出来 / 数据稀薄时的排查路径

#### 现象 A：`hotness_snapshots` 总行数 = 0

最可能原因：

1. **还没到第一个整点**：服务启动时不一定正好赶在 `:00 / :15 / :30 / :45`，
   等到下一个整点才会出第一份。等就行
2. **基线数据不足**：`grep "baseline data insufficient" logs/service.log`，
   `entity_mentions` 累计不够 100 条会跳过。等数据攒够；想强行看，
   改 `config/settings.py` 的 `hotness_min_baseline_count: 100 → 20`
3. **SlidingCounter backfill 失败**：`grep "sliding-counter backfill" logs/service.log`，
   看是不是 ERROR

#### 现象 B：榜单出来了但 `count_short` 都是个位数 + `cross_source` 全是 1

最可能原因：**上游抓取服务不在跑 / 速率很低**。先跑入口 2 的第 3 条 SQL
看三源 `last_10min`：

| 三源 last_10min 表现 | 含义 | 动作 |
|---|---|---|
| 全部 0 | 上游抓取服务都停了 | 找抓取服务那边重启 |
| Twitter 几十条，Binance/Discord 都是 0 | 只 Twitter 在抓 | 找 Binance/Discord 抓取服务那边排查 |
| 三源都有几十~几百条 | 上游正常 | 那就是词典识别率问题，跳到现象 C |

#### 现象 C：消息进来了但 `entity_mentions` 不涨

最可能原因：**词典 + 正则没识别到**。这是词典缺口，不是 bug。看哪些
消息没被抽到实体：

```sql
SELECT nm.text
FROM normalized_messages nm
LEFT JOIN entity_mentions em ON em.msg_id = nm.id
WHERE em.id IS NULL
  AND nm.is_duplicate = FALSE
  AND nm.l1_processed_at IS NOT NULL
ORDER BY random()
LIMIT 20;
```

人眼扫一遍：

- 真噪音（"早安"、"睡了"）→ 漏过没问题
- 应该被抓到但没抓（比如"ai agent 起飞了"）→ 词典缺口，去补 `dictionaries/narratives.yaml`

详见 [Q2 场景 B/C](#q2)。

### 一句话结论

看数据 = **`scripts/check_status.py` 跑一遍**。看不懂某个列 → 回到本节
"排行榜各列含义"对照表。榜单稀薄不代表系统坏 → 先用入口 2 的 SQL 3
看上游数据流量。


---

## <a id="q4"></a>Q4：narratives 和 tickers 怎么区分？我不知道某个名字该归到哪里

**短答**：

- **ticker** = "**它**"（一个具体可交易的资产，有交易对）
- **narrative** = "**一类东西**"（一个主题/赛道，下面有很多 ticker）

记不住就**回想你跟朋友聊起这个东西时怎么说**：

- "**$XXX 涨了 10%**" → ticker（带 `$`、有价格）
- "**XXX 是下一个 narrative**" → narrative（不带 `$`、是趋势）
- "**XXX 链上 TVL 很高**" → chain（讨论生态，归 `chains.yaml`）

### 特征对比表

| ticker 的特征 | narrative 的特征 |
|---|---|
| 在交易所能买卖、有 `$` 符号 | 没有交易对，是个抽象概念 |
| 是一个**专有名词**（BTC、SOL）| 是一个**类别名/主题词**（DeFi、AI Agent）|
| 单数：一只币 / 一支股票 | 复数：一群项目的集合 |
| 用户讨论时常带 `$`：`$BTC 涨了` | 用户讨论时不带 `$`：`AI agent 是下一个风口` |
| 跌了你能直接亏到钱 | "热"了你需要再选一个具体 ticker 才能买 |

### 决策树（自己往下问）

遇到一个名字，自己问自己：

**Q1：交易所能买卖吗？能直接买的有 `$` 前缀吗？**
- 是 → **ticker**（写到 `tickers.yaml`）
- 否 → 继续下一题

**Q2：它是一类项目的统称吗？说出这个名字时，能联想到至少 3~5 个具体 token？**
- 是 → **narrative**（写到 `narratives.yaml`）
- 否 → 继续下一题

**Q3：它是一条公链吗？**
- 是 → **chain**（写到 `chains.yaml`，独立分类）
- 否 → 可能是 KOL / 项目方公司名 / 其他，按场景决定

### 典型例子

#### 100% ticker

| 名字 | 理由 |
|---|---|
| BTC, ETH, SOL | 主流币，交易对就是它们本身 |
| DOGE, PEPE, WIF | meme 币，能交易 |
| AAPL, NVDA, TSLA | 美股 ticker |
| USDT, USDC | 稳定币，能买能换 |
| EIGEN, AAVE, UNI | 协议自己发的 token |

#### 100% narrative

| 名字 | 理由（联想到的 ticker） |
|---|---|
| **AI_Agent** | ai16z, virtuals, fartcoin, GAME by Virtuals |
| **RWA** | ONDO, MKR, USDY, BUIDL |
| **Restaking** | EIGEN, ETHFI, REZ |
| **DePIN** | RNDR, HNT, IO, GRASS |
| **GameFi** | AXS, IMX, SAND, RON |
| **Memecoin_Season** | 一种市场状态（"meme 季来了"），不是某个具体的币 |
| **Modular** | TIA, EIGENDA, AVAIL |

#### 模糊地带（容易搞错的）

| 名字 | 该归哪里 | 说明 |
|---|---|---|
| **EigenLayer** | tickers（EIGEN 别名） | 协议本身有 token EIGEN，所以"eigenlayer"应该是 EIGEN 的 alias。如果讨论 "eigenlayer 生态"通指整个赛道，归 Restaking narrative —— **重叠 OK，靠 keywords 上下文区分** |
| **Bitcoin Layer 2** | narratives（BTC_Ecosystem） | 一类 L2 项目的统称（Stacks、Babylon、Runes 都算），不是单一 token |
| **Solana** | chains | 它是一条公链，归 `chains.yaml`；该链上的代币是 SOL ticker |
| **Layer 2** | narratives | 是一类公链的统称（ARB、OP、BASE 都是）。但讨论时通常说具体某条 L2，所以这个 narrative 实战价值不高，可不填 |
| **DeFi** | narratives | 没问题，但词太宽泛，所有协议都算，会污染热榜。建议拆成 `Restaking` / `LSDfi` / `Perp_DEX` 这种更细的子叙事 |
| **Trump** | tickers（TRUMP 是 meme 币） | 但如果讨论的是政治本身（关税、降息），那是**事件**，超出 Phase 1 范围 |
| **MicroStrategy** | tickers（MSTR 别名） | MicroStrategy 是上市公司，对应 MSTR 股票，归 ticker。"机构买 BTC"这种是事件不是叙事 |

### 重叠的处理：用 `keywords` 加上下文

很多关键词会同时出现在 ticker 和 narrative 里，比如 "eigenlayer"。处理方式
**不是"两边都放"或"哪边都不放"**——而是让 narrative 的 keywords 加更多上下文。

**反例（启动会报"别名冲突"）**：

```yaml
# tickers.yaml
EIGEN:
  aliases: [eigenlayer, eigen]

# narratives.yaml
Restaking:
  keywords: [eigenlayer]   # ← 启动 raise："别名冲突"
```

**正例（用上下文消歧）**：

```yaml
# tickers.yaml
EIGEN:
  aliases: [eigen]   # 不要单写 eigenlayer，让 narrative 用

# narratives.yaml
Restaking:
  keywords: [restaking, eigenlayer ecosystem, LRT, liquid restaking]
```

或者反过来：

```yaml
# tickers.yaml
EIGEN:
  aliases: [eigen token, "$EIGEN"]

# narratives.yaml
Restaking:
  keywords: [restaking, eigenlayer, LRT]
```

实际上**`$EIGEN` 这种带 `$` 写法系统会用正则自动抓**，所以 ticker 词典专心
管"裸写"消歧即可。

### 实操：让数据告诉你怎么填

不要凭空想象去扩词典。让系统跑几天，用 SQL 查 narrative 命中分布：

```sql
SELECT entity, count(*) AS hits
FROM entity_mentions
WHERE entity_type = 'narrative'
  AND ts >= now() - INTERVAL '24 hours'
GROUP BY entity
ORDER BY hits DESC;
```

观察结果：

| 现象 | 含义 | 动作 |
|---|---|---|
| 某 narrative 命中数 = 0 | 关键词不准（用户实际不这么说） | 改这个 narrative 的 keywords |
| 某 narrative 命中数 > 1000 | 关键词太宽泛 | 缩窄 keywords，避免污染 |
| 某赛道你看着热闹但榜上没有 | 词典缺口 | 加新 narrative |

### 一句话结论

**短期不用纠结**。当前 `narratives.yaml` 模板里的 9 个已经覆盖主流分类。
**让数据告诉你怎么填**，比凭空想象准得多。拿不准的名字先放一边别填，
等观察出节奏后再扩。


---

## <a id="q5"></a>Q5："热点"和"提到最多"有什么区别？

**短答**：

- **提到最多** = "**总量**"——绝对数字，谁被讨论的次数多
- **热点** = "**异常**"——相对你自己的常态，谁突然被讨论得不寻常地多

这是整个系统设计的核心。两者的差别决定了"系统有没有用"。

### 用大白话讲一遍

想象你监控一个微信群：

- 每天 BTC 被聊 **100 次**（雷打不动，常态）
- 某新 meme 币 `$WIFHAT` 之前没人提，今天突然被聊 **30 次**

**"提到最多"** 的答案：**BTC**（100 > 30）
**"热点"** 的答案：**WIFHAT**（从 0 → 30，是 BTC 这种"日常底噪"完全没的爆发）

哪个对你更有用？显然是 WIFHAT。BTC 每天都被聊 100 次，这条信息不告诉你
任何"该关注什么"。WIFHAT 这种突然冒头的，才可能是赚钱机会、新风口、新事件。

**"提到最多"无聊**，因为 BTC / ETH 永远在前几名（它们是行业基础币）。
**"热点"有用**，因为它能 surface 你**还没听说过但已经热起来**的东西。

### 在系统里分别对应什么

打开 `scripts/check_status.py` 跑出来的输出，对照看：

#### 第 2 / 3 节："提到最多" —— `entity_mentions` 直接 count

实现：

```sql
SELECT entity, count(*) AS cnt
FROM entity_mentions
WHERE ts >= now() - INTERVAL '1 hour'   -- 或 24 hours
GROUP BY entity
ORDER BY cnt DESC
```

就是简单数数。**结果永远是 BTC / ETH / SOL 这种行业头部币霸榜**，
一周看下来基本没变化。

#### 第 4 节："热点" —— `hotness_snapshots` 排行榜

不是简单数数，而是用了一个公式（见 `services/l2_hotness.py`）：

```
growth_rate = 短窗提及次数 / max(基线时均提及次数, 2.0)
final_score = growth_rate × (1 + 0.3 × (跨源数 - 1))
```

翻译成人话：

| 指标 | 含义 | 价值 |
|---|---|---|
| `count_short` | 过去 1 小时被提到几次 | 绝对热度（"提到最多"那种数字）|
| `count_baseline` | 过去 7 天**平均每小时**被提到几次 | 这玩意儿的"日常水位" |
| `growth_rate` | `count_short / count_baseline` | **本质：和自己比涨了几倍** |
| `cross_source` | 在几个数据源出现过（1~3） | 多平台共振，可信度加成 |
| `final_score` | growth_rate × 跨源加成 | 最终排名依据 |

**关键是 `growth_rate` 这一步**：

- BTC：1h=100 次，baseline=每小时 100 次 → growth_rate = 100/100 = **1.0**（很无聊，跟昨天一样）
- WIFHAT：1h=30 次，baseline=每小时 0 次（新冒头）→ growth_rate = 30/2 = **15**（爆炸式增长）
- 排行榜按 growth_rate 排：**WIFHAT 排第 1，BTC 根本进不了 Top 20**

这就是为什么 hotness 榜单里有些奇奇怪怪没听过的名字——**那正是系统的价值**。
它在帮你 surface 那些"突然变热"的东西。

### 真实数据对照（来自 2026-05-13 18:30 的实跑）

#### 第 3 节"24h 提到最多"：

```
1  ETH    112        ← 行业老二，常态
2  BTC    106        ← 行业老大，常态
3  稳定币   55        ← alias 配错的副产物，已修正
4  SOL    38         ← 公链常态
```

**这些信息基本没用**。BTC、ETH 永远在前面，看不出"现在该关注什么"。

#### 第 4 节"热点排行榜"：

```
1  BNB     count_short=4   baseline=1.43  growth=2.00
2  ETH     count_short=3   baseline=2.81  growth=1.07
3  COIN    count_short=2   baseline=0.34  growth=1.00
```

这个就有意思：

- BNB 平时 1.43 次/h，这一小时被提了 4 次 → 增长 **2 倍** → 排第 1
- ETH 虽然总量也涨了，但增长才 **1.07 倍**（基本符合常态）→ 排第 2 但分数低
- COIN（Coinbase 股票）平时 0.34 次/h，这一小时 2 次 → **6 倍增长**，但因为绝对数字小排第 3

数据流量充足时**正常应该看到这种**：

```
1  AI16Z   count_short=87  baseline=2.5  growth=34.8   ← AI agent 突然爆火
2  $TRUMP  count_short=45  baseline=0    growth=22.5   ← 政治事件触发
3  EIGEN   count_short=30  baseline=1.5  growth=20.0   ← 协议有大新闻
```

**这些才是值得关注的信号**——它们是从"常态"中跳出来的异常。

### 为什么两个都要看（不能只看一个）

理论上 hotness 榜单足够用了，为啥脚本还给你看"提到最多"？两个用途：

| 看什么 | 解决什么问题 |
|---|---|
| 提到最多（24h Top 20）| **诊断系统是否健康**：BTC / ETH 应该排前列、命中数符合预期数量级 |
| 热点（hotness）| **找投资机会 / 信号**：发现你不知道的新热点 |

举例对比：

| 现象 | 含义 |
|---|---|
| 24h "提到最多" 里 BTC 不在前 5 | 你的 Twitter 抓取坏了 |
| hotness 榜每次都是同一批名字 | 数据流量太低，growth_rate 区分度不够，得增加抓取量或缩小 short_hours 窗口 |
| hotness 榜冒出一堆乱码 / 合约地址 | 词典或正则有 bug |

它们一起看，能从两个角度判断系统状态。

### 类比 Android 性能监控

如果你做过 App 性能监控，这两个概念非常熟悉：

| 这个系统 | Android 性能监控类比 |
|---|---|
| 提到最多 | "今天总崩溃次数"——数字大但没用，因为**绝对数会被 DAU 影响** |
| 热点（growth_rate）| "崩溃率突增 5 倍"——**相对自己的基线**，立刻知道出问题了 |
| cross_source | "iOS / Android / Web 三端同时增长"——跨端共振，更值得警惕 |

监控里没人天天看"今天总崩溃次数"——大家都看"环比 / 同比变化率"。
同样道理。

### 一句话结论

**"提到最多"是体检表（看系统活没活）**，**"热点"是 alpha 表（看哪儿有机会）**。
你打开脚本最该花时间的是**第 4 节排行榜的 `growth_rate` 列**，不是第 2 / 3 节
的次数列。


---

## <a id="q6"></a>Q6：Telegram 告警为什么冷却 60 分钟而不持久化？没收到告警怎么排查？

**短答**：

- **冷却为什么 60 分钟而不持久化**：进程内 dict 实现冷却 = 0 复杂度；持久化到
  DB 的代价是建表 + 迁移 + 读写时序，换来"重启后冷却仍生效"的微小价值。
  最坏情况——进程重启时一个实体被多发 1 次告警——完全可接受
- **没收到告警的排查路径**：5 个分层判断点，从下到上排，覆盖率 99%

### Q6.1 为什么冷却用进程内 dict 而不持久化

#### 持久化方案的"成本 vs 收益"

| 持久化方案要做的事 | 进程内 dict 的代价 |
|---|---|
| 加一张 `alert_history` 表 | 0 |
| 写 alembic 迁移 | 0 |
| 每次 `_decide_alert` 多查一次 DB | 0 |
| 每次推送成功多写一次 DB | 0 |
| 错误处理（DB 写失败时是否还要告警？要不要重试？）| 0 |
| **可重启 / 可多副本** | **进程重启冷却失效** |

可重启 / 可多副本的价值有多大？

- **重启频率**：本服务平均 1 周重启 < 1 次（部署 / 改 settings）
- **重启时点**：用户能控制，避开告警高峰即可
- **多副本**：本服务是 Phase 1 单 worker 设计，**根本没在跑多副本**，未来
  Phase 3 真要做 HA 再说

**最坏情况**——进程重启 → 某实体在重启前 30 分钟被告警过 → 重启后冷却 dict
是空的 → 该实体再被告警 1 次。**就这。** 单实体多收 1 条 Telegram 消息，
没有任何业务损失。

### Q6.2 智能冷却的 4 路径决策树

进程内 dict 不影响"质变升级"功能。系统不简单地"60 分钟内不告警"，而是
按下面 4 路径决策（按优先级，**顺序不能变**）：

| 优先级 | 触发条件 | 标签 | 设计意图 |
|---|---|---|---|
| 1. 首次 | `_alert_records[entity]` 不存在 | `[首次]` | 第一次见这个实体就告警 |
| 2. 心跳 | 距上次告警 ≥ `alert_heartbeat_hours`（默认 6h） | `[持续 Nh]` | 持续热点 6h 一次"我还在烧"提醒 |
| 3. growth 升级 | 本次 growth ≥ 上次 × `escalation_growth_multiplier`（默认 1.5）| `[升级 → growth ×X.X]` | 突然加速的信号必须刷新 |
| 4. 跨源升级 | cross_source 增加 | `[跨源升级 +N]` | 多平台共振的信号必须刷新 |
| —（不告警）| 60min 内 + 无以上质变 | （静默） | 防刷屏 |
| 5. 重新触发 | 出 60min 冷却 + 仍达阈值 | `[重新触发]` | 持续热点的常规告警节奏 |

**心跳必须放在 growth 升级之前**——一个持续 6 小时但 growth 没大变的热点
如果心跳放后面，会落到"60min 内 + 无质变"分支被静默掉。

### Q6.3 没收到告警怎么排查（5 个分层判断点）

按下面顺序排查，找到第一个"不通"的就停。

#### Step 1：服务在跑吗

```bash
pgrep -fl 'python.*main.py'
```

没输出 → 进程死了，跑 `./scripts/restart.sh --bg` 重启。

#### Step 2：AlertTriggerService 启动了吗

```bash
grep "AlertTriggerService 启动" logs/service.log | tail -1
```

| 输出 | 含义 |
|---|---|
| `AlertTriggerService 启动：growth_threshold=20.0 ...` | OK，继续下一步 |
| `Telegram 告警未配置（token/chat_id 为空），已禁用` | token 或 chat_id 没填，去 `config/_alerts.py` 填值后重启 |
| 啥都没匹配到 | 本次启动还没到 AlertTriggerService 那一段；或日志被滚动了，找今天的日志文件 |

#### Step 3：hotness_snapshots 有产出吗

最新的 hotness window_end 多久前？

```sql
SELECT max(window_end) FROM hotness_snapshots WHERE window_type='1h';
```

- < 30 分钟前 → OK，继续下一步
- > 30 分钟前 → hotness 主流程卡了，先排查 hotness（见 `docs/operations_guide.md` §4 场景 A），
  hotness 不出榜，告警自然没东西可推

#### Step 4：有实体满足三道门槛吗

最新窗口里有没有 growth ≥ threshold 的记录？

```sql
SELECT entity, growth_rate, count_short, cross_source
FROM hotness_snapshots
WHERE window_end = (SELECT max(window_end) FROM hotness_snapshots)
  AND growth_rate >= 20.0      -- 跟你的 alert_growth_threshold 对齐
  AND count_short >= 3         -- alert_min_count_short
  AND cross_source >= 1        -- alert_min_cross_source
ORDER BY growth_rate DESC;
```

- 0 行 → **没有合格实体**，是"今天没新热点"，不是 bug。如果一周持续 0 行，
  考虑把 `alert_growth_threshold` 调小（参考 `docs/operations_guide.md` §6.1）
- ≥ 1 行 → 应该会告警，继续下一步

#### Step 5：是被冷却跳过 / Telegram 推送失败了吗

```bash
# 是不是被冷却跳过了
grep "alert skipped" logs/service.log | tail -10

# 是不是 Telegram 推送失败了
grep "telegram .*error" logs/service.log | tail -10

# 真的发出过告警吗
grep "alert sent" logs/service.log | tail -10
```

| 现象 | 原因 | 动作 |
|---|---|---|
| 大量 `alert skipped: ... 60min 内无质变` | 本来就在冷却中 | 等冷却到期或等"质变"，是正确行为 |
| `telegram http error: 401 Unauthorized` | Token 错了 | BotFather `/revoke` 重发 token，更新 `config/_alerts.py` |
| `telegram http error: 400 Bad Request` | chat_id 错了 / Bot 没被加进群 | 私聊场景：先在 Telegram 给 Bot 发一条消息再用 getUpdates 拿 chat_id |
| `telegram network error` | 服务器到 api.telegram.org 不通 | 国内环境检查 VPN；试 `curl -m 5 https://api.telegram.org/bot<TOKEN>/getMe` |
| `alert sent` 有，但你手机没收到 | Telegram 客户端通知静音了 | 检查手机端 Bot 通知设置；或换台手机看 |

### Q6.4 端到端联调最快验证方式

不想等下一份榜出来？直接调 TelegramClient 测一条：

```bash
.venv/bin/python -c "
from config.settings import get_settings
from notifications.telegram_client import TelegramClient
s = get_settings()
client = TelegramClient(bot_token=s.telegram_bot_token, chat_id=s.telegram_chat_id)
print('result:', client.send_text('PomsAI 告警链路联调测试'))
"
```

- `result: True` + 手机收到消息 → 链路完全 OK，问题在上面 Step 3/4（hotness
  没产出 / 没合格实体）
- `result: True` + 手机没收到 → 看 Telegram 通知设置 / 手机网络
- `result: False` → 看终端的 `telegram .*error` 日志，对照上面 Step 5 的表

### Q6.5 一句话结论

**冷却用进程内 dict 而不持久化**——成本几乎为 0，最坏代价（重启多发 1 条）
完全可接受。**没收到告警**按上面 Step 1→5 顺序排查，99% 落在 Step 3/4
（hotness 自身没产出 / 没合格实体），跟告警系统本身无关。



---

## <a id="q7"></a>Q7：为什么需要三个时间窗口（1h / 6h / 24h），不能只看 1h 吗？

**短答**：

- **1h 窗口的本质局限**：噪音大、看不到中期趋势、看不到宏观信号——一份榜
  没法兼顾"最快冒头""半天演进""全天宏观"三种不同时间尺度的信号
- **三窗口的工程代价几乎为零**：DB 体积一年多 4 GB（PG 实例还有 100 GB 富余）；
  CPU 多 5%（每 15 分钟多算两份榜）；Phase 1 已有的 SlidingCounter 天然支持多窗口
- **三窗口产出语义互补**：1h surface 即时热点、6h surface 中期趋势、24h surface
  宏观事件；三个窗口对同一 entity 的 growth_rate 自然衰减，从分布就能读出"信号
  到底有多强"

### Q7.1 用大白话讲三个窗口看什么

想象你监控一个微信群：

| 维度 | 它告诉你的事 | 真实例子 |
|---|---|---|
| 1h | 最近 1 小时谁在被疯狂提及 | `$WIFHAT` 5 分钟前突然被刷屏 |
| 6h | 最近半天谁在持续被讨论 | 某叙事下午开始有人聊，到晚上还在烧 |
| 24h | 最近一整天谁在做主线 | BTC 跌破关键支撑，全天讨论量翻 5 倍 |

**为什么 1h 不够**：1h 维度的 BTC 永远在被讨论，growth_rate ≈ 1.0，全天看上去都
"很无聊"——但 BTC 真有大新闻时，**24h 维度**的提及量会翻 5~10 倍，那才是真信号。
反过来，新 meme 币上线 1 小时内就爆火，**1h 维度**才能立刻 surface 它，6h/24h
窗口要等很久才反应过来。

### Q7.2 公式怎么自然衰减

三窗口共用 Phase 1 同一个 hotness 公式：

```
growth_rate = short_count / max(baseline_per_hour, smoothing)
final_score = growth_rate * (1 + 0.3 * (cross_source - 1))
```

只有 `short_count` 和 `smoothing` 两个值随窗口变化：

| 窗口 | short_count 是 | smoothing 默认 | 效果 |
|---|---|---|---|
| 1h | 过去 1h 提及次数 | 2.0 | 短窗信号最尖锐，新热点 growth 直接拉满 |
| 6h | 过去 6h 提及次数 | 5.0 | 中等敏感度 |
| 24h | 过去 24h 提及次数 | 10.0 | 最稳健，宏观信号才能拉高 growth |

举个例子，假设某 entity 三窗口的提及次数分别是 40 / 60 / 80 次（baseline ≈ 100 次），
三窗口的 growth_rate 会自然衰减成 **20.0 → 12.0 → 8.0**——short_count 涨得没有
窗口长度涨得快，所以**长窗的 growth 自然小一档**。这就是为什么 smoothing 要等比
放大：让冷启动期不会出现"24h 窗口 growth 虚高 ×100"的奇怪现象。

设计意图：**三窗口的 final_score 量纲一致**，可以直接拿来横向比较 "同一个 entity
在哪个窗口最热"。

### Q7.3 24h 榜为什么默认不屏蔽 BTC/ETH

1h 榜默认黑名单含 BTC/ETH/SOL/BNB/USDT/USDC/DAI——因为它们在 1h 维度
**永远在被讨论**，growth_rate ≈ 1.0，留在榜上只占位、还会模糊其它实体。

但 24h 维度不一样：

- BTC 平时一天 baseline ~50 次/h，**真有大新闻时 24h 提及总量飙到 2000+**，
  growth_rate = 2000 / 50 / 24 ≈ 1.7（24h 平均后），但单看绝对量就翻了 5~10 倍
- 这是宏观叙事信号——"BTC 突破历史新高"、"美联储议息影响 BTC"，应该被 surface

所以 24h 默认黑名单**只屏蔽稳定币**：

```python
hotness_24h_exclude_entities = ("USDT", "USDC", "DAI")
```

如果你看着觉得吵（BTC/ETH 天天在 24h 榜首），加回去重启即可：

```python
hotness_24h_exclude_entities = ("USDT","USDC","DAI", "BTC","ETH","SOL","BNB")
```

### Q7.4 24h 冷启动期 8~12 小时空榜，为什么可以接受

24h 榜的基线公式是 `baseline_total / (baseline_days*24 - short_hours)`，
默认 `baseline_days=8` 时基线期是过去 8 天。新部署服务时 `entity_mentions` 表
要先攒够 500 条才出榜（`hotness_24h_min_baseline_count=500` 保护）。

按你当前流量（每天产生 2000~5000 条 entity_mentions），**8~12 小时**就能攒够
500 条。这段时间日志会看到：

```
hotness skipped: baseline data insufficient (count=400 < 500)
```

是正常行为不是 bug。等数据够自然出榜。

为啥不把门槛降到 100（让首日就有榜）？因为基线样本太少时 growth_rate 全是噪音——
某 entity 平时一天 1 次，今天 5 次就被算成 5× growth，但那可能只是随机波动。
500 条门槛保证榜单出来时**信号置信度足够**。

如果你确定要看首日的"半成品 24h 榜"，临时把 `hotness_24h_min_baseline_count`
改成 100 重启即可。

### Q7.5 为什么不每窗口都跑独立的告警通道

本任务**只铺多窗口榜单**，告警仍然只读 1h 榜。这是有意的：

- 1h 告警通道是 Phase 2.2 已上线的成熟链路，不动它降低风险
- 6h / 24h 是新维度，需要观察 1~2 周才知道"6h growth_rate 多少算异常""24h 应该
  alert 在什么阈值"——这些经验得有数据才能调
- 未来 Phase 2.2.1 真要加多通道告警时，AlertTriggerService 加个 `window_type`
  参数 + 起 3 个实例就行，本任务已经把 hotness_snapshots 表准备好了

所以**现阶段最佳实践**：

- 用 `scripts/check_status.py` 或 SQL 看 6h/24h 榜，每周扫一两次
- 1h 告警继续负责"瞬间冒头"的实时推送
- 等观察出 6h/24h 的有效阈值再考虑接告警

### Q7.6 三窗口同时上榜 = 强信号

最有意思的副产品：某 entity 在三个窗口**同时**进 Top-10 时，意味着它"既是
即时热点、又是中期趋势、还是全天主线"——这是非常强的信号。SQL 一条就能查：

```sql
WITH latest AS (
  SELECT window_type, MAX(window_end) AS window_end
  FROM hotness_snapshots
  GROUP BY window_type
)
SELECT entity, ARRAY_AGG(window_type ORDER BY window_type) AS hits
FROM hotness_snapshots h
JOIN latest l USING (window_type, window_end)
WHERE h.rank <= 10
GROUP BY entity
HAVING COUNT(DISTINCT window_type) = 3;
```

返回的 entity 就是三窗口共振信号，比任何单窗口的 Top-1 都更值得关注。这是
Phase 2.2.2 想做"共振告警"的基础，本任务先把数据铺好。

### Q7.7 一句话结论

**1h 是闪电、6h 是涟漪、24h 是潮水**——三个时间尺度看到的是同一份数据的不同
层次。多窗口让系统从"只能看到突发热点"升级到"能看到信号在不同时间维度上的
形态"。工程代价几乎为零（DB 廉价 + CPU 多 5%），但产品维度直接 ×3。


---

## <a id="q8"></a>Q8：实时告警通道（Phase 2.4）和整点告警怎么协同？会不会刷屏？

**短答**：

- **协同方式**：实时通道挂在 EntityExtractor 末尾的 `notify(N)` hook 上，写完
  一批新提及就同步触发；整点通道在每 :00/:15/:30/:45 扫 hotness_snapshots 表。
  两套通道**共享同一个 `_alert_records` 冷却 dict**，同一 entity 60 分钟内
  最多发 1 条 Telegram 消息（不分通道），所以不会刷屏
- **为什么不写 hotness_snapshots 表**：实时计算的 window_end 是分钟级（如 10:23:47），
  写进表会污染"对齐到 :00/:15/:30/:45 整点"这条 Phase 2.1 的核心约束
- **为什么实时阈值（30）比整点（5）严**：分钟级窗口的 growth 抖动比整点榜大很多，
  双层防刷屏（共享冷却 + 更严阈值）保证整体频率可控

### Q8.1 协同的全景图

```
EntityExtractor.run_once()
  └─ 写完 N 条 entity_mentions
        ├─► realtime_trigger.notify(N) ◄── 实时通道入口
        │      _pending_count += N
        │      够 burst_threshold 就跑 _trigger_immediate()
        │            └─► 内存里跑 1h 公式 → 命中阈值 → Telegram (带 [实时] 标签)
        │                  └─► 推送成功 → 写共享 _alert_records
        │
        └─► （worker 继续轮转，最终走到 :00/:15/:30/:45）
              AlertTriggerService.run_once()
                  └─► 读 hotness_snapshots(1h, 最新窗口)
                        └─► 命中阈值 → 决策树（含读共享 _alert_records）
                              └─► 不在冷却 → Telegram (带 [首次]/[升级] 等标签)
                                    └─► 推送成功 → 写共享 _alert_records
```

**关键不变量**：`AlertTriggerService._alert_records` 和
`RealtimeAlertService.shared_alert_records` 在 `main.py Step 5e` 通过
**同一引用**注入：

```python
realtime_service = RealtimeAlertService(
    ...,
    shared_alert_records=alert_service._alert_records,   # ← 同一 dict 对象
    ...,
)
```

不是 deepcopy、不是新建空 dict——**绝不允许**让两边各自维护自己的冷却记录。
要不然实时刚发完 OP，2 分钟后整点又会再发一条 OP 给你。

### Q8.2 为什么实时不写 hotness_snapshots 表

Phase 2.1 多窗口榜单有一条核心约束：**所有 window_end 必须对齐到
:00/:15/:30/:45 整刻钟**。这条约束保证：

- 三窗口（1h/6h/24h）window_end 总是相同时刻 → 横向比较有意义
- 历史回放、跨日期对比、定期任务时间对齐都很简洁

实时榜的 window_end 是 `datetime.now(UTC)`，可能是 10:23:47、12:08:12 这种
分钟秒级时刻。如果写进 hotness_snapshots：

- 表里多出大量"非整刻钟"行，破坏对齐约束
- `SELECT MAX(window_end) WHERE window_type='1h'` 这种最常用查询会拿到非整点时刻
- 跨窗口对比、check_status.py 输出全乱

所以实时通道**只在内存里算 + 直接推送**，结果不落库。代价是"看不到实时榜的
历史"——但实时榜的产品定位本来就是"瞬间冒头的实体在第一时间被推送"，回放
价值很低，整点榜的历史已经够看。

### Q8.3 为什么实时阈值（30）比整点（5）严

整点榜窗口宽度 1h，实时榜也用 1h 公式（design §3.1 沿用同一个 `short_hours`）。
**为什么阈值还要不同？**

差别不在窗口宽度，而在**触发时机**：

- 整点：每 15 分钟在固定时刻扫一次，相当于对 1h 提及量做了"15 分钟级采样"
- 实时：可能在任意分钟秒触发，相当于实时观察 1h 提及量的瞬时值

举个例子，某 entity 的 1h 累计提及次数曲线如下：

```
时刻 t:    10:00  10:05  10:10  10:15  10:20  ...  10:55  11:00
1h count:    8      9     12     11     10    ...    9     10
            (整点)                (整点)               (整点)
```

整点采样看到的是 8 / 11 / 10 / ... 这种相对平稳的序列，growth_rate 比较稳定。
但实时观察 10:08 那一瞬间可能是 13（一个 KOL 突然连发 5 条），growth 暂时拉
得很高，1 分钟后就回落到 9。**这种瞬时尖刺如果阈值松，会导致告警空响**——
推送出去用户去看时，已经回归常态了。

所以实时阈值 = 整点阈值 × 1.5~3 是合理的工程取舍：

| 实时阈值策略 | 效果 |
|---|---|
| `realtime_growth >= alert_growth × 1.0`（不严） | 大量 1~5 分钟的瞬时尖刺被推 |
| `realtime_growth >= alert_growth × 1.5`（略严） | 过滤掉 50% 噪音，仍能 surface 真热点 |
| `realtime_growth >= alert_growth × 3.0`（默认 30 vs 整点 ~5~10） | 只接"真的爆了"的信号，可能漏掉一些温和爆发 |
| `realtime_growth >= alert_growth × 10.0`（很严） | 接近"只接 super hot"，实时通道几乎不发，用处不大 |

默认值（实时 30 vs 整点 5）的设计赌注是"用户更怕被刷屏，少发几条比多发几条好"。
真有持续热点，整点通道在最坏 14~15 分钟内会兜底推送。

### Q8.4 双层防刷屏：共享冷却 + 更严阈值

实时通道天然比整点频繁（每攒够 50 条 mention 就跑一次，可能几分钟一次）。如果
没有防护，最坏情况：同一 entity 每 5 分钟被推一次，1 小时被推 12 条。

实际上有**两层防护**让这种情况不会发生：

**第一层：共享冷却 dict**

实时和整点写同一份 `_alert_records`。某 entity 被实时通道推过后：
- 60 分钟内：决策树走到"60min 内 + 无质变 → 不告警"分支，整点通道也跳过
- 除非 growth 翻 1.5 倍 / cross_source 增加 / 距上次 6 小时心跳触发

整点通道反过来也一样，先发 → 实时通道 60 分钟内被冷却。

**第二层：更严阈值**

实时阈值 30 远高于整点阈值 5，意味着只有"真的炸"的实体才会触发实时。绝大
多数温和上涨的实体走整点通道，实时通道默认沉默。

两层叠加的效果：单 entity 每小时被推 ≤ 1 条，每天最多 ~ 24 条；整体每天被推
~50 条（取决于热点数量），完全在用户可接受范围内。

### Q8.5 实时和整点谁先到？

无固定顺序，谁先达到自己的触发条件谁先到。但因为：

- 实时通道每 5~50 秒就可能跑一次（取决于 burst_threshold + 流量）
- 整点通道每 15 分钟才跑一次

**绝大多数情况实时先到**——这正是 Phase 2.4 的产品价值。整点变成"兜底"角色：
万一实时被流量稀薄拖到没触发（比如连续几小时只有零星新提及），整点一定会
扫一遍 hotness_snapshots 表。

唯一能让整点先到的情况：实时通道因为 `realtime_burst_threshold` 没攒够而沉默，
直到下一个整点 hotness 把这个 entity 算进 1h 榜，整点通道直接推。这种情况下
实时通道的 60min 冷却也会同步生效，下一个 _trigger_immediate 不会重发。

### Q8.6 实时通道挂了影响整点吗？

不影响。整点通道**完全独立**——它只读 hotness_snapshots 表，不依赖 `RealtimeAlertService`
的任何状态。具体保证：

- 实时通道异常被 `try/except` 隔离在 `_trigger_immediate()` 内，只 `log.error` 不
  向上抛
- `EntityExtractor.run_once()` 末尾的 `notify(N)` 也用 `try/except` 隔离，挂掉
  不影响 EntityExtractor 主流程
- 实时通道不存在时（`realtime_enabled=False` 或三个启用条件之一不满足），
  `entity_extractor.realtime_trigger = None`，`run_once` 末尾的 hook 一行 `if
  is not None` 直接跳过

所以最坏情况是"实时通道全挂、退化为 Phase 2.2 整点告警"——延迟回到 14~15 分钟，
功能不受损。这是 Phase 2.4 的设计契约：**配置缺失即降级**。

### Q8.7 一句话结论

**整点是基线、实时是增益**——实时通道把"急性信号"前置到 1~2 分钟，整点通道
继续扫所有"温和但显著"的信号兜底。共享冷却 dict + 更严阈值的双重防护让两通道
不会刷屏，未配置 / 异常时实时自动降级、整点继续工作。

---

## <a id="q9"></a>Q9：实体共现网络（Phase 2.5）解决了什么问题？为什么用 PMI 而不是简单数共现次数？

**短答**：

- **解决的问题**：单实体榜（hotness_snapshots）只能看到"谁在变热"，看不到"谁在
  跟谁一起变热"。新叙事萌芽时，参与的几个 token 单看 growth 都不显眼，但**同期
  被一起讨论**才是真信号。共现网络从"节点视角"切到"边视角"，专门捕获这种信号
- **为什么 PMI 而不是 cooccur_count**：纯共现次数会被巨头主导（BTC + USDT 共现
  1000 次完全没意义），PMI 把"共现频率/独立预期"做归一化后，新叙事萌芽的弱信号
  才能浮出水面
- **为什么本任务不接 Telegram**：避免和现有单实体激增告警在用户视角下混淆；
  数据先沉淀 1 周再决定共现告警阈值，留 Phase 2.5.1 单独做

### Q9.1 单实体榜看不到的信号长啥样

想象一个真实场景：restaking 叙事复苏。第 N 天的实际数据可能是：

| Entity | 1h growth | 24h growth | 在 hotness 榜的表现 |
|---|---|---|---|
| EIGEN | 1.4× | 1.6× | 排在 50 名开外，毫无存在感 |
| ETHFI | 1.3× | 1.5× | 同样毫无存在感 |
| REZ | 1.2× | 1.4× | 同样毫无存在感 |

单看每个 token：growth 都没破阈值，没人会注意。但**同时**：

- 24h 内 EIGEN 和 ETHFI 一起出现在 12 条消息里
- 24h 内 EIGEN 和 REZ 一起出现在 8 条消息里
- 7 天 baseline 期：这三对从未一起出现过

这是 restaking 叙事复苏的**铁证**——三个 token 都属于这个赛道，被人一起讨论
说明话题在升温。但单实体榜彻底看不到这个信号，因为每个 token 单看都很平庸。

共现网络解决这件事：

```
entity_cooccurrence:
  entity_a=EIGEN  entity_b=ETHFI  cooccur_count=12  pmi=3.18  is_new_pair=TRUE
  entity_a=EIGEN  entity_b=REZ    cooccur_count=8   pmi=2.96  is_new_pair=TRUE
  entity_a=ETHFI  entity_b=REZ    cooccur_count=6   pmi=2.74  is_new_pair=TRUE
```

三个 `is_new_pair=TRUE` 的强对扎堆出现 → 一眼看出 restaking 叙事复苏。Phase 2.6
做实体聚类时，这三对在 PMI 加权图上自然形成一个连通子图，自动识别为一个簇 =
"restaking 叙事候选"。

### Q9.2 PMI 比 cooccur_count 强在哪

公式：`PMI(a, b) = log( cooccur × N / (count_a × count_b) )`

意义：把"a 和 b 一起出现的次数"和"独立预期下应该出现的次数"做比值，再取 log。

- PMI = 0：互不相关（独立预期）
- PMI = 1：共现概率是独立预期的 e≈2.7 倍
- PMI = 3：约 20 倍（强信号）

举两个对比例子（来自 design.md §3.3 实测）：

| Pair | cooccur 次数 | count_a × count_b | 期望共现 | PMI | 解读 |
|---|---|---|---|---|---|
| (BTC, USDT) | 1000 | 5000 × 4500 = 22.5M | ≈ 800 | log(1000/800) ≈ **0.22** | 巨头一起聊，PMI 接近 0，是噪音 |
| (EIGEN, ETHFI) | 12 | 25 × 18 = 450 | ≈ 0.5 | log(12/0.5) ≈ **3.18** | 低频共振，PMI 高，是叙事候选 |

如果只看 cooccur_count：BTC+USDT 的 1000 远高于 EIGEN+ETHFI 的 12——榜首永远
是巨头，新叙事永远埋没。PMI 给了我们"按概率归一化"的尺子，让真正的信号浮出来。

### Q9.3 为什么默认 24h 窗口（而不是 1h）

简单算一下：

- 假设 1h 窗口内有 50 条带实体的消息（典型流量）
- 候选实体 ≈ 100 个
- 平均每条消息 2.5 个实体 → 50 × C(2.5, 2) ≈ 150 对共现机会
- 每对独立 count_a / count_b ≈ 1~3，cooccur ≈ 1~2
- N=50

随便一对：count_a=2, count_b=2, cooccur=1, N=50 → PMI = log(1×50 / 4) ≈ 2.5
**几乎所有对的 PMI 都看起来很高**——但这是统计学上的"小样本噪音"。1h 窗口下，
任何巧合都会变成"显著信号"，导致榜单全是噪音。

24h 窗口下：

- 24h 内消息数 ≈ 800（典型流量）
- 候选实体 ≈ 200
- 每个 entity count_a / count_b 平均 5~30
- N=800

同一对真正"突然成对"的情况：count_a=10, count_b=10, cooccur=8 → PMI = log(8×800 /
100) ≈ 4.16，远高于真随机共现的 PMI（< 1）。**信号噪音比足够大**才能区分真假。

实测下来，1h 共现榜里 PMI 中位数在 2~3 之间但全是噪音，24h 榜里 PMI 中位数 0.5
但 PMI ≥ 2 的对 80%+ 是真信号——这是设计选 24h 的关键工程理由。

### Q9.4 候选集为什么用 active_entities('24h')

不限候选集会有 n² 爆炸：所有出现过的实体（可能上千个）两两组合 = 几十万对。
24h 内活跃过的 entity 集合（实测 < 1000）作候选 → C(1000, 2) ≈ 50 万对，
配合 `min_cooccur_count >= 3` 过滤，最终进 PMI 计算的对 < 1000。

**关键不变量**：CooccurrenceService 共享 HotnessService 的同一个 `SlidingCounter`
实例（main.py Step 5e 注入）。如果没共享，两边 active_entities 不一致，hotness
和 cooccurrence 数据就对不上。

### Q9.5 为什么 baseline=0 + 短窗≥3 才标 is_new_pair

两条都必要：

- **只看 baseline=0**：短窗 1 次共现也算"新对"——但 1 次大概率是偶然，不是信号
- **只看 短窗≥3**：BTC + ETH 长期一起聊，今天也一起聊 ≥3 次 → 误标成"新对"
- **两条结合**：从没一起出现过 + 现在 24h 内 ≥3 次 = 真正的"突然成对"

3 次是经验值（一次偶然、两次巧合、三次趋势）。如果你觉得 3 次太严，可以临时
把 `cooccur_min_cooccur_count` 调到 2 观察一周——但默认 3 是更稳妥的"既不漏真
信号也不收太多噪音"折中点。

### Q9.6 为什么不在本任务接 Telegram

技术上能做到（共享 TelegramClient），但产品上不该做：

- **阈值耦合**：单实体激增告警 `growth_threshold=5` 与共现 PMI 阈值 `min_pmi=1.0`
  是不同维度，合并到 `AlertTriggerService` 会让冷却逻辑（`AlertRecord` 当前以
  entity 为 key）失效，需要额外抽象 key 类型
- **用户视角混淆**：单实体告警 "🔥 实体 BTC growth 25x" 已形成习惯，再混入
  "🔥 [新对] EIGEN+ETHFI" 会让用户分不清两类信号；Phase 2.5.1 单独通道更清晰
  （甚至可以用不同 emoji 如 "🕸️ [新叙事候选]"）
- **数据先稳定再加通道**：先观察 1 周 PMI 分布，看 99% 分位实际是多少，再决定
  告警阈值；这一周数据通过 entity_cooccurrence 沉淀，Phase 2.5.1 直接复用

Phase 2.5.1 预期接口：新建 `CooccurAlertTriggerService` 读 entity_cooccurrence，
复用现有 TelegramClient，冷却 key 改为 `(entity_a, entity_b)` tuple，阈值
`cooccur_alert_min_pmi=3.0`（比写库阈值 1.0 严）。本任务交付的字段
（pmi / is_new_pair / cooccur_count）足够支撑那一套，schema 不需要改。

### Q9.7 一句话结论

**单实体榜看节点、共现网络看边**——叙事级共振只能在边视角看到；用 PMI 而不是
cooccur_count 是为了把巨头噪音从信号里剔除；本任务先产数据、不接 Telegram 是
为了让 1 周观察期沉淀真实分布，避免上线即调阈值。
