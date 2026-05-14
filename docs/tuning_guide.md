# 调参指南：从猜测到数据驱动

> 73 个配置参数让你晕了？这份文档不解释每个字段做什么（那些在
> `docs/configuration.md` 里），而是回答更核心的问题：
> **"我怎么知道当前配置调得对不对？"**

---

## 1. 核心痛点诊断

### 1.1 为什么参数多但调不动

参数多本身不是问题，**没有反馈循环**才是。

现在的调参流程长这样：

```
改 alert_growth_threshold: 5.0 → 3.0
  ↓
重启服务
  ↓
等 6 小时
  ↓
凭体感判断"好像告警变多了"
  ↓
再调 → 再等 → 再凭感觉
```

这是**开环系统**。每次决策都基于体感（"昨天好像挺吵"），不基于数据，所以越调越懵。

### 1.2 真正需要的是闭环

```
改之前先看：当前 7 天数据 growth 分布是什么样
  ↓
跑 SQL：阈值 = X 时过去 7 天会触发多少次告警？
  ↓
拿到具体数字（"阈值 5 → 38 次/周 ≈ 5 次/天"）
  ↓
按"频率目标"反推阈值（"我要每天 ≤ 3 次 → 阈值需要 ≥ 8"）
  ↓
重启 → 1~2 天后再次跑 SQL 验证实际触发数 ≈ 预期
```

这是**闭环**。每次改都有数据支撑，不再瞎猜。

---

## 2. 四步调参方法论

### Step 1：先看分布，别看单点

最常见的错误：看到一条 `growth=145` 的告警就觉得"阈值太低了"。**单点不能反映整体**。先跑一条 SQL 看分位数：

```sql
SELECT
  window_type,
  COUNT(*) AS total_records,
  ROUND(percentile_cont(0.50) WITHIN GROUP (ORDER BY growth_rate)::numeric, 2) AS p50,
  ROUND(percentile_cont(0.75) WITHIN GROUP (ORDER BY growth_rate)::numeric, 2) AS p75,
  ROUND(percentile_cont(0.90) WITHIN GROUP (ORDER BY growth_rate)::numeric, 2) AS p90,
  ROUND(percentile_cont(0.95) WITHIN GROUP (ORDER BY growth_rate)::numeric, 2) AS p95,
  ROUND(percentile_cont(0.99) WITHIN GROUP (ORDER BY growth_rate)::numeric, 2) AS p99,
  ROUND(MAX(growth_rate)::numeric, 2) AS max_val
FROM hotness_snapshots
WHERE window_end >= NOW() - INTERVAL '7 days'
GROUP BY window_type
ORDER BY window_type;
```

输出例子：

| window_type | total | p50 | p75 | p90 | p95 | p99 | max |
|---|---|---|---|---|---|---|---|
| 1h | 13440 | 1.5 | 2.8 | 4.2 | 6.8 | 18.3 | 145.0 |
| 3h | 13440 | 1.8 | 3.2 | 5.5 | 8.1 | 15.5 | 62.0 |
| 6h | 13440 | 2.1 | 3.8 | 6.2 | 9.0 | 14.0 | 38.5 |
| 24h | 13440 | 1.3 | 2.0 | 3.1 | 4.5 | 8.0 | 22.0 |

读这张表的方法：

- **p50（中位数）**：一半的数据 ≤ 这个值。是"日常水平"
- **p95**：只有 5% 的数据超过它。是"轻度异常"
- **p99**：只有 1% 的数据超过它。是"明显异常"
- **max**：极端值。是"史诗级事件"

### Step 2：按"频率目标"反推阈值

阈值不是凭空选的，要先回答："**我每天能接受多少条告警**？"

| 期望频率 | 对应分位数 | 含义 |
|---|---|---|
| 每天 1~2 条（只接超热）| p99 | 一周 7~14 条 |
| 每天 5 条（适度）| p97 | 一周 ~35 条 |
| 每天 10 条（密切关注）| p95 | 一周 ~70 条 |
| 每天 30 条（不睡觉了）| p90 | 一周 ~210 条 |

按上面那张分布表，假设你想 1h 榜每天接 5 条：
- 看 p97 ≈ 8.5 → 阈值设 **8.5**

24h 榜每天接 1 条：
- 看 p99 ≈ 8.0 → 但 24h 数据少（每天只 96 条快照）→ p99 = 1 条/天 → 阈值设 **8.0**

### Step 3：用 backtest SQL 验证你的选择

改阈值前先 backtest 一下，确认你的选择会不会让告警数太离谱：

```sql
-- 模拟阈值 X 在过去 7 天的触发次数
-- 注意：这只算"通过 growth 阈值"，没考虑 cooldown（实际告警数会更低）
SELECT
  window_type,
  COUNT(*) FILTER (WHERE growth_rate >= 3)  AS at_3,
  COUNT(*) FILTER (WHERE growth_rate >= 5)  AS at_5,
  COUNT(*) FILTER (WHERE growth_rate >= 7)  AS at_7,
  COUNT(*) FILTER (WHERE growth_rate >= 10) AS at_10,
  COUNT(*) FILTER (WHERE growth_rate >= 15) AS at_15,
  COUNT(*) FILTER (WHERE growth_rate >= 20) AS at_20
FROM hotness_snapshots
WHERE window_end >= NOW() - INTERVAL '7 days'
GROUP BY window_type
ORDER BY window_type;
```

输出例子：

| window_type | at_3 | at_5 | at_7 | at_10 | at_15 | at_20 |
|---|---|---|---|---|---|---|
| 1h | 1240 | 380 | 158 | 67 | 24 | 12 |
| 3h | 980 | 290 | 125 | 50 | 18 | 9 |
| 6h | 720 | 180 | 75 | 28 | 10 | 5 |
| 24h | 56 | 14 | 6 | 2 | 1 | 0 |

按这张表能直接看出：
- 1h 阈值 5 → 380 次 / 7 天 ≈ **54 次/天（太吵）**
- 1h 阈值 10 → 67 次 / 7 天 ≈ **10 次/天（合理）**

注意：**实际告警数还会被 cooldown 进一步压缩**（同 entity 60 分钟内只发一条），上面的数字是上界，真实告警通常是它的 1/3 ~ 1/5。

### Step 4：观察 1~2 天再调下一个参数

改完一个参数，**等 24~48 小时**再调下一个。原因：

- 加密热点有日内周期（亚洲盘 / 美盘活跃度差几倍）
- 单看几小时容易把"短期波动"当"系统问题"
- 频繁改参数 = 反馈被打断 = 永远停在试错期

如果你 6 小时改一次参数，你测的不是"配置好不好"，是"参数变化的副作用"。

---

## 3. 调参顺序：先粗后细

### 第一周：把告警频率调到舒服的范围

只关心一个目标：**"每天告警数稳定在我能接受的范围"**。

只调这 4 个：

```python
alert_growth_threshold       # 1h
alert_3h_growth_threshold    # 3h
alert_6h_growth_threshold    # 6h
alert_24h_growth_threshold   # 24h
```

按 Step 2 推荐方法选阈值，目标是每天 5~15 条告警（看你的活跃度偏好）。

### 第二周：把"我不关心的币"屏蔽掉

观察一周后，你会发现某些币老是被推但你根本不关心（比如 BTC 24h 宏观信号你一看价格就懂，不需要 push）。

加到 `alert_exclude_entities`：

```python
alert_exclude_entities: ("BTC", "ETH", "SOL", "BNB", "USDT", "USDC", "DAI")
```

### 第三周（可选）：精调 cooldown / 心跳

如果觉得"持续热点提醒太频繁"或"太稀疏"：

```python
alert_cooldown_minutes: 60     # 同 entity 冷却期
alert_heartbeat_hours: 6       # 持续热点心跳
alert_growth_delta_pct: 0.3    # cooldown 内涨 30% 就升级
```

这一档是**精修**，没观察够 1~2 周就动这些参数，多半在试错。

### 第四周（可选）：考虑 hotness 黑名单

`alert_exclude_entities` 还不够 → 某些币连 Digest 都不想看：

```python
hotness_exclude_entities: 加更多
hotness_3h_exclude_entities: 加更多
hotness_6h_exclude_entities: 加更多
hotness_24h_exclude_entities: 加更多
```

注意 24h 默认只屏蔽稳定币，因为 24h 的 BTC/ETH 突变是宏观信号，比 1h 更值得保留。

---

## 4. 调参禁忌（这些事不要做）

### 4.1 不要同时改多个参数

改一个等 1~2 天看效果，再改下一个。同时改 5 个，告警数变了你都不知道是哪个参数起的作用。

### 4.2 不要改 smoothing / baseline_days / min_baseline_count

这三个是**数学约束**，不是体感参数：

- `smoothing` 已按窗口长度等比放大（2/3/5/10），改了反而让冷启动期 growth 飙到天上
- `baseline_days` 24h 必须 ≥ 8（数学公式约束），其他窗口 7 天是行业标准
- `min_baseline_count` 是"数据少时跳过本轮"的保护，调低 = 把噪音放上榜

如果你觉得需要调这些，多半是别处出了问题，先别动它们。

### 4.3 不要在凌晨 / 周末调

加密市场活跃度有强周期性：

- 美东早 9 点（北京 21~22 点）：流量高峰，告警自然多
- 周末：流量比工作日低 30~50%
- 凌晨 3~6 点：流量低谷

**周二~周四白天**调参最准确，能拿到代表性的反馈数据。

### 4.4 不要追求"完美阈值"

阈值是经验值，**没有最优解**。市场结构每隔几个月会变（新热点周期、新链兴起），3 个月前的"完美阈值"半年后可能完全不适用。

接受"阈值需要每季度复盘一次"，比追求"调一次管一辈子"更实际。

---

## 5. 我的推荐工作流

### 5.1 每次调参前的 3 分钟仪式

```bash
# 1. 看分布
.venv/bin/python scripts/tune_helper.py

# 2. 思考"我想要的告警频率"
#    例：每天 1h 榜 ~10 条，3h 榜 ~5 条，6h 榜 ~3 条，24h 榜 ~1 条

# 3. 按 helper 推荐改 4 个阈值
vim config/_alerts.py

# 4. 重启
./scripts/restart.sh
```

### 5.2 每周一次的复盘

每周一上班，跑一次：

```sql
-- 看上周告警实际触发了多少次
SELECT
  DATE(window_end) AS day,
  window_type,
  COUNT(*) FILTER (WHERE growth_rate >= 5)  AS hits_5,
  COUNT(*) FILTER (WHERE growth_rate >= 10) AS hits_10
FROM hotness_snapshots
WHERE window_end >= NOW() - INTERVAL '7 days'
GROUP BY day, window_type
ORDER BY day DESC, window_type;
```

如果某天某窗口异常多 / 异常少，回头看日志确认原因（是真热点还是数据流入抖动）。

### 5.3 每月一次的全面复盘

每月最后一天：

1. 跑一次完整的 `tune_helper.py`
2. 对比 1 个月前的分布数据，看市场结构有没有变
3. 决定要不要调参（如果分位数偏移 > 30%，说明市场变了，需要重调）

---

## 6. 一句话结论

**不是参数太多，是缺反馈。**

你不需要"理解每个参数"，你需要：
1. 一个能 5 秒看到分布的工具（`scripts/tune_helper.py`）
2. 一套"先看分布，后定阈值，再 backtest，最后等 1~2 天"的纪律
3. 接受"调参是季度复盘事项，不是每天的事"

我们准备一起调参那天，按这个方法论走，最多 30 分钟就能把 4 个核心阈值定下来。


---

## 7. 漏斗式调试：从原始消息到 Telegram 告警

> 前面 6 节是"出口侧调参"——你接受流水线，只调最后的阈值。
> 这一节是"漏斗侧调试"——当你觉得"明明该有信号怎么没出来"时，
> 用这个方法定位**问题在哪一层**。

### 7.1 流水线 7 层全景

消息从原始抓取到 Telegram 告警，要穿过 7 层。每一层都可能筛掉数据，
也都对应不同的配置参数：

```
┌─ 原始抓取（外部，不是本项目控制）
│   twitter_posts / binance_square_posts / discord_messages
│
├─ ① NormalizerService           参数：normalizer_batch_size
│   筛选：几乎不筛，只是搬运 + 字段统一
│
├─ ② Deduplicator (SimHash 去重)  参数：dedup_hamming_threshold / dedup_window_hours
│   筛选：重复消息 → is_duplicate=TRUE，不送下游
│
├─ ③ prefilter.classify           参数：无（规则在 services/prefilter.py 硬编码）
│   筛选：长度 < 20 直接 drop；命中"梭哈/亏麻"等噪音模式 drop
│        含 $TICKER 或词典命中 → 强 keep
│
├─ ④ 实体抽取（正则 + 词典）       参数：dictionaries/*.yaml 内容
│   筛选：keep 的消息里抠出 entity；抠不到的不写 entity_mentions
│
├─ ⑤ SlidingCounter（内存计数）   参数：WINDOWS_SECONDS（不需要调）
│   筛选：不筛，只统计
│
├─ ⑥ HotnessService（每 15 分钟）  参数：hotness_*_smoothing / baseline / exclude / top_k
│   筛选：count_short=0 跳过；exclude 黑名单跳过；
│        基线样本 < min_baseline_count 整轮跳过；只留 top_k 条
│
├─ ⑦ AlertTriggerService          参数：alert_*_growth_threshold / cooldown / exclude
│   筛选：三道门槛 + 4 路径决策树 + 全局 alert_exclude_entities
│
└─ Telegram 推送
```

### 7.2 用 pipeline_inspect.py 一眼看清

```bash
.venv/bin/python scripts/pipeline_inspect.py --hours 6
```

这个工具会帮你算出**每一层的输入 / 输出 / 转化率**，输出 4 段：

1. **🚿 消息漏斗**：原始 → 归一化 → 去重 → 抽实体 各层数字 + 进度条
2. **🏷 实体类型分布**：ticker/chain/narrative/project 占比，看词典命中分布
3. **📈 hotness → 告警**：每个窗口写入了多少快照，多少通过当前阈值
4. **🔬 异常诊断**：自动给出问题层级 + 改哪个参数的建议

### 7.3 典型异常和对应参数

| 现象 | 哪一层有问题 | 改哪个参数 |
|---|---|---|
| 原始三张表过去时段都没新数据 | 上游抓取（外部）| 不是项目控制，去查抓取服务 |
| 归一化产出率 < 50% | ① Normalizer 跟不上 | `normalizer_batch_size` 调大；`poll_interval_seconds` 调小 |
| 去重率 > 60% | ② SimHash 太松 | `dedup_hamming_threshold` 从 3 调到 2 |
| 去重率 < 5% 但流量正常 | ② SimHash 太严或漏判 | `dedup_hamming_threshold` 从 3 调到 4 |
| 实体抽取覆盖率 < 10% | ④ 词典 + prefilter 不够 | 看 🏷 分布；补 `dictionaries/*.yaml` |
| 词典命中率 < 30% | ④ 多数命中靠正则 $XXX | 把高频新币加到 `tickers.yaml`（运行一周后再补）|
| hotness 一直跳过（日志 `hotness skipped`）| ⑥ 基线不足 | `hotness_min_baseline_count` 调小（冷启动期）|
| 1h 榜全是某几个大币 | ⑥ exclude 黑名单不够 | `hotness_exclude_entities` 加币 |
| 24h 榜全是 ETH 推送 | ⑦ alert 黑名单空 | `alert_exclude_entities` 加 BTC/ETH 等 |
| 阈值看起来对，但告警还是 0 | ⑦ cooldown 撞到 | 第一条 [首次] 后 60 分钟冷却中；或 `alert_min_count_short` 过严 |

### 7.4 漏斗调试流程

每次出问题，按这个顺序走：

```
跑 pipeline_inspect.py
   ↓
看哪一层数字异常（红字 ⚠️）
   ↓
对照 §7.3 改对应参数
   ↓
重启 → 24~48 小时后再跑
   ↓
对比前后转化率，确认改对了
```

不要一上来就改阈值。**80% 的"告警太少"问题不在阈值，在前面层级**。
比如你的实体抽取覆盖率只有 18%，意味着 82% 的消息进库了但抽不到 entity——
再怎么调阈值也是无米之炊。

### 7.5 两个工具的分工

| 工具 | 看什么 | 何时用 |
|---|---|---|
| `pipeline_inspect.py` | 漏斗每层转化率 | 觉得"信号该有的没出来"，先用这个定位问题层 |
| `tune_helper.py`      | 出口 growth 分布 | 流水线 OK，只想精调阈值（§2 那套方法）|

**典型工作流**（我们调参那天会走这套）：

1. `pipeline_inspect.py --hours 6`：先确认管道健康，问题不在前面层级
2. 如果有 ⚠️：按 §7.3 改前面层级的参数（词典/批量/dedup 阈值），重启
3. 等 24~48 小时积累新数据
4. `pipeline_inspect.py --hours 24`：再次确认前面层级正常
5. `tune_helper.py --days 7`：看出口分布，按 ~5 条/天那一列填阈值
6. 重启，再等 24~48 小时收尾验证

### 7.6 不用 pipeline_inspect.py 也行：手写 SQL

如果你想用 SQL 直接查每一层（比如服务还没起来、想在远端 DB 看），核心 6 条：

```sql
-- ① 过去 1h 三张原始表新增量
SELECT 'twitter' AS src, COUNT(*) FROM twitter_posts WHERE created_at >= NOW()-INTERVAL '1h'
UNION ALL
SELECT 'binance_square', COUNT(*) FROM binance_square_posts WHERE created_at >= NOW()-INTERVAL '1h'
UNION ALL
SELECT 'discord', COUNT(*) FROM discord_messages WHERE created_at >= NOW()-INTERVAL '1h';

-- ② 过去 1h normalized_messages 新增 + 去重比例
SELECT is_duplicate, COUNT(*)
FROM normalized_messages
WHERE created_at >= NOW() - INTERVAL '1h'
GROUP BY is_duplicate;

-- ③ 过去 1h 至少抽到 1 个实体的消息数
SELECT COUNT(DISTINCT msg_id) FROM entity_mentions WHERE ts >= NOW() - INTERVAL '1h';

-- ④ 过去 1h 词典 / 正则命中比例
SELECT
  CASE WHEN confidence = 1.0 THEN '词典' ELSE '正则' END AS hit_type,
  entity_type,
  COUNT(*)
FROM entity_mentions
WHERE ts >= NOW() - INTERVAL '1h'
GROUP BY hit_type, entity_type
ORDER BY COUNT(*) DESC;

-- ⑤ 过去 1h hotness 写入了多少快照
SELECT window_type, COUNT(*) AS snapshots, COUNT(DISTINCT entity) AS entities
FROM hotness_snapshots
WHERE window_end >= NOW() - INTERVAL '1h'
GROUP BY window_type;

-- ⑥ 过去 1h 各窗口 growth ≥ 当前阈值的快照数
-- （把 2.0 / 5.0 / 5.0 / 3.0 换成你 _alerts.py 里的实际值）
SELECT
  COUNT(*) FILTER (WHERE window_type='1h'  AND growth_rate >= 2.0) AS h1_pass,
  COUNT(*) FILTER (WHERE window_type='3h'  AND growth_rate >= 5.0) AS h3_pass,
  COUNT(*) FILTER (WHERE window_type='6h'  AND growth_rate >= 5.0) AS h6_pass,
  COUNT(*) FILTER (WHERE window_type='24h' AND growth_rate >= 3.0) AS h24_pass
FROM hotness_snapshots
WHERE window_end >= NOW() - INTERVAL '1h';
```

这 6 条 SQL 就是 `pipeline_inspect.py` 的核心查询，纯 psql 也能用。

### 7.7 一句话结论

**不知道阈值该多少 → 用 tune_helper.py（出口侧）；
不知道为什么没告警 → 先用 pipeline_inspect.py（漏斗侧）。**

调参的本质是把"系统体感"换成"数据反馈"，两个工具配合 5 分钟就能定位 90%
的"告警太少 / 太吵 / 错过真信号"问题。
