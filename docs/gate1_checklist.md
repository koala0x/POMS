# Gate 1 验收操作手册

> Phase 1（crypto-narrative-radar）的 72 小时验收执行清单。
>
> 基于 `.kiro/specs/crypto-narrative-radar/requirements.md` 的 Success Metrics
> 7 条指标，逐条给出**观测命令**、**通过阈值**、**失败诊断流程**。
>
> **验收窗口起算点**：requirements.md 明确规定从"首份有效排行榜产出时刻"
> 起算 72 小时，**不是**从进程启动时刻起算。冷启动期的 2~12 小时基线累积
> 期间没有排行榜产出是预期行为（Req 7.7 降级），不计入窗口。

---

## 0. 验收前置检查

Gate 1 窗口开启之前必须确认：

- [ ] `dictionaries/tickers.yaml` 至少含 60 个 ticker（从老 prefilter 迁移而来）
- [ ] `dictionaries/{chains,narratives,kols}.yaml` 至少骨架存在（可为空 `{}`）
- [ ] Alembic 已迁至 revision `001`（`alembic current` 显示 `001`）
- [ ] `main.py` 启动日志含下列关键行（证明新链路已挂载）：

```
词典就绪：tickers=... chains=... narratives=... kols=... aliases=...
SlidingCounter backfill 结束：ok=True total=... elapsed=...s
summary worker 启动:level1=3,level2=3,new=3,空闲 sleep 30s
```

- [ ] `.venv/bin/python -m pytest tests/ --ignore=tests/test_ollama_client.py -q`
      显示 `109 passed, 1 skipped`（测试基线）

若以上任一项未满足，先修复再开启 Gate 1。

---

## 1. 指标 1：稳定性（72 小时不崩溃）

> requirements.md Success Metrics §1

### 通过阈值

- 连续运行 ≥ 72 小时，worker 主循环未退出
- PostgreSQL 连接未泄漏（同一时刻 `pg_stat_activity` 中该服务的连接数 ≤ 20）
- 进程 RSS 内存增长不超过启动初期的 2 倍

### 观测方式

**进程存活 + worker 循环活动（每小时执行一次）**：

```bash
# 1. 进程还在
pgrep -fl "python.*main.py" || echo "进程已退出，立刻查日志"

# 2. worker 还在跑（查最近 5 分钟是否有 run_once 相关日志）
tail -n 500 logs/service.log | grep -E "normalizer|entity_extractor|hotness" | tail -5
```

**PG 连接监控（每 4 小时执行一次）**：

```sql
SELECT count(*) AS conn_count, state
FROM pg_stat_activity
WHERE application_name LIKE '%PomsAI%' OR usename = 'all_new'
GROUP BY state;
-- 期望：active + idle 合计 ≤ 20；若看到 idle 在 transaction 持续大量堆积，
-- 说明某个 session 没 commit/rollback，优先排查 l1_entity_extractor / l2_hotness
```

**内存观测（每天一次）**：

```bash
# macOS
ps -o rss= -p $(pgrep -f "python.*main.py") | awk '{print $1/1024 " MB"}'

# Linux
ps -o rss= -p $(pgrep -f "python.*main.py") | awk '{print $1/1024 " MB"}'
```

记录启动 1 小时后的 RSS 作为 baseline，72 小时观测点不超过 `2 × baseline`。

### 失败诊断流程

| 现象 | 下一步排查 |
|---|---|
| 进程退出 | `tail -500 logs/service.log` 找 traceback，按 5 条硬约束逐个复核（通常是 DB 连接异常 / 词典加载失败） |
| worker 循环停 | 查最近 WARN/ERROR 日志；特别看 "summary worker 已停止"（说明 `_stop_event` 被意外 set） |
| 连接数爆炸 | 每个新 service 的 `run_once` 内部是否存在 `get_session()` 未正确释放；回查 `l2_hotness._compute_records` 对每 entity 的两次 DB 查询是否 session 未关 |
| 内存持续涨 | 先看 SlidingCounter 的 `_store` 规模（`len(sc._store['7d'])`）；7d 窗口超过 10 万 entity 可能是词典命中过泛 |

---

## 2. 指标 2：排行榜产出节奏（72 小时 ≥ 5472 条）

> Success Metrics §2：每 15 分钟稳定产出，允许 ±2 分钟抖动

### 通过阈值

- 72 小时累计 `hotness_snapshots` 条数 ≥ **5472**（= 72 × 4 × 20 × 0.95）
- 相邻两次 `window_end` 间隔在 [13, 17] 分钟区间

### 观测方式

**累计条数**：

```sql
SELECT count(*) AS total_snapshots,
       count(DISTINCT window_end) AS total_windows,
       min(window_end) AS first_window,
       max(window_end) AS last_window
FROM hotness_snapshots
WHERE window_end >= <Gate1 起始时刻>
  AND window_type = '1h';
-- 通过条件：
-- total_snapshots >= 5472
-- total_windows * 20 ≈ total_snapshots（每个 window_end 应有 20 条）
```

**产出节奏（找缺失 / 延迟的 window）**：

```sql
WITH windows AS (
  SELECT DISTINCT window_end
  FROM hotness_snapshots
  WHERE window_end >= <Gate1 起始时刻>
    AND window_type = '1h'
  ORDER BY window_end
),
gaps AS (
  SELECT window_end,
         window_end - lag(window_end) OVER (ORDER BY window_end) AS gap
  FROM windows
)
SELECT window_end, gap
FROM gaps
WHERE gap IS NOT NULL
  AND (gap > INTERVAL '17 minutes' OR gap < INTERVAL '13 minutes');
-- 期望：返回 0 行（所有相邻间隔都在 [13, 17] 分钟内）
```

### 失败诊断流程

| 现象 | 下一步排查 |
|---|---|
| 总条数不够 | 先看 `total_windows`，若 windows 数够但每个 window < 20，说明候选 entity 不足 → 词典太稀疏；若 windows 数就不够，回到"产出节奏"查缺失 |
| 某段时间无产出 | `grep 'hotness skipped' logs/service.log`，区分是 "counter not ready"（Req 7.8 一过性）还是 "baseline data insufficient"（Req 7.7，通常是 Gate 1 窗口太靠前） |
| 产出间隔 > 17min | `grep '>60s 警告' logs/service.log`，若频繁出现说明 `_compute_records` 对单 entity 的两次 DB 查询累积耗时超标，考虑 Phase 2 改成 GROUP BY 聚合（design.md §3.7 性能注）|

---

## 3. 指标 3：排行榜命中率 ≥ 60%

> Success Metrics §3：人工判断 Top-20 中有多少个实体出现在"当前 Twitter 热门"

### 通过阈值

每天 9:00 / 14:00 / 21:00 三个整点采样，命中率 ≥ 60%（即 Top-20 里 ≥ 12 个命中）。
**连续 3 天**取平均，均值 ≥ 60%。

### 观测方式

每个采样时刻执行：

```sql
SELECT rank, entity, entity_type,
       count_short, count_baseline, growth_rate,
       cross_source, final_score, is_new_entity
FROM hotness_snapshots
WHERE window_end = <对齐到 :00 / :15 / :30 / :45 的采样整点>
  AND window_type = '1h'
ORDER BY rank ASC
LIMIT 20;
```

拿到 20 行后，打开 Twitter 同时刻的"探索"页 / 主要 KOL 的推文流，人工数出
这 20 条里有多少个实体确实是当时在被讨论的话题（包括代币符号、公链名、
叙事主题、大合约地址等）。

记录到 `gate1_daily_log.md`（自建，不强制格式）：

```
2026-05-12 09:00 采样：
  Top-20: BTC ETH SOL LINK UNI ARB PEPE SHIB RENDER AI16Z ...
  命中: BTC ETH SOL UNI PEPE AI16Z ...（13 个）
  未命中: LINK ARB SHIB RENDER ...（7 个 → 其中 LINK 是冷启动噪音 / ARB 是昨日余热）
  命中率: 13/20 = 65% ✅
```

### 失败诊断流程

| 现象 | 下一步排查 |
|---|---|
| 命中率 < 60% 但有明显"Twitter 真热但我榜上没有" | 看词典：热点实体的 ticker / 别名是否在 `tickers.yaml`？Phase 2 扩词典可补；Phase 1 期若缺严重，考虑延期 Gate 1 待词典补齐 |
| 命中率 < 60% 且榜上一堆 "没听过的名字" | 多半是 `$TICKER` 正则抓到了噪音（像 $AAA $BBB 这种虚假币名）；回查 Phase 2 可加 "ticker 长度 ≤ 6 + 必须大写" 校验 |
| 榜上很多 EVM/Solana 合约地址占位 | 正则误伤：查 `tests/test_prefilter.py::test_classify_solana_address` 附近的边界，Phase 2 可加 base58 有效性校验 |

---

## 4. 指标 4：去重有效性（10% ~ 60%）

> Success Metrics §4：既要证明 SimHash 工作（下界），也要防止阈值过松错杀（上界）

### 通过阈值

Gate 1 窗口内：

```
dup_ratio = count(is_duplicate = TRUE) / count(*)
10% <= dup_ratio <= 60%
```

### 观测方式

**每天一次**：

```sql
SELECT
  count(*)                                       AS total,
  count(*) FILTER (WHERE is_duplicate = TRUE)    AS dup,
  (count(*) FILTER (WHERE is_duplicate = TRUE))::float / nullif(count(*), 0) AS dup_ratio
FROM normalized_messages
WHERE ts >= <Gate1 起始时刻>;
```

### 失败诊断流程

| 现象 | 下一步排查 |
|---|---|
| `dup_ratio < 10%` | SimHash 阈值可能太严；先抽样几条"看起来是转发但 `is_duplicate=FALSE`" 的消息，人工算它们的汉明距离（可在 Python REPL 里调 `Deduplicator.compute_simhash` + `_hamming`）；若发现真重复但距离 > 3，考虑 Phase 2 把 `settings.dedup_hamming_threshold` 调到 5 |
| `dup_ratio > 60%` | 阈值太松 + 真内容被误杀；拉几条 `is_duplicate=TRUE` 的消息，看它们和 `dup_of` 指向的原版在内容上是否真的重复；若发现"只是行业话题相近但不是转发"的误判，把阈值调小到 2 |
| `dup` 一直是 0 | **严重**：Deduplicator backfill 可能完全失败，回查启动日志 `deduplicator backfill failed:` |

---

## 5. 指标 5：词典命中可观测

> Success Metrics §5：confidence=1.0（词典）与 0.95（正则）两条路径都必须有产出

### 通过阈值

Gate 1 窗口内同时满足：

```
count(confidence = 1.0) > 0
count(confidence = 0.95) > 0
```

### 观测方式

```sql
SELECT
  confidence,
  count(*)                           AS mentions,
  count(DISTINCT entity)             AS distinct_entities,
  count(DISTINCT msg_id)             AS distinct_messages
FROM entity_mentions
WHERE ts >= <Gate1 起始时刻>
GROUP BY confidence
ORDER BY confidence DESC;
-- 期望：两行结果，confidence=1.0 和 0.95 都有 mentions > 0
```

### 失败诊断流程

| 现象 | 下一步排查 |
|---|---|
| `confidence = 1.0` 为 0 | 词典加载失败或 `alias_index` 为空；`grep '词典加载完成' logs/service.log` 看 aliases 数是否 > 0 |
| `confidence = 0.95` 为 0 | 走正则的实体一条都没抽到，多半是正则本身有 bug；跑 `pytest tests/test_prefilter.py -k "regex or evm or solana" -v` 看是否绿 |

---

## 6. 指标 6：老链路不退化（每日产出下降 ≤ 10%）

> Success Metrics §6：Phase 1 不抢老链路的 DB 连接 / Ollama 资源

### 通过阈值

Phase 1 部署前后（各取一周均值），`summary_level1` / `summary_level2` 每日
产出条数变化：

```
下降比例 = (部署前日均 - 部署后日均) / 部署前日均 <= 10%
```

### 观测方式

**获取部署前 baseline**（Gate 1 开启之前跑，记录下来）：

```sql
SELECT source,
       date_trunc('day', created_at) AS day,
       count(*)                      AS daily_count
FROM summary_level1
WHERE created_at >= now() - INTERVAL '7 days'
GROUP BY source, day
ORDER BY source, day DESC;

-- 同 summary_level2
```

记录每个 source 的 7 日均值作为 baseline（`docs/gate1_daily_log.md`）。

**Gate 1 窗口内每日对比**：

```sql
SELECT source,
       count(*) AS count_24h
FROM summary_level1
WHERE created_at >= now() - INTERVAL '24 hours'
GROUP BY source;

-- 把结果和 baseline 对比：|new - baseline| / baseline <= 0.10
```

### 失败诊断流程

| 现象 | 下一步排查 |
|---|---|
| `summary_level1` 日产出下降 > 10% | 先查老链路日志看是否报"数据库连接获取超时" → 多半是新链路抢连接池；把 `Database.engine` 的 `pool_size` 从 5 调到 10 |
| `summary_level2` 下降 > 10% 但 l1 正常 | 多半是 `Level2Service` 的 `threshold` 没到，但 level1 产出减少导致；这种情况随 level1 恢复会自愈，连续观察 3 天 |
| 两者同时大幅下降 | **触发回滚预案**（见 `docs/rollback_plan.md`）|

---

## 7. 指标 7：LLM 调用量验证（严格 = 0）

> Success Metrics §7：Phase 1 新流水线对 `OllamaClient.chat` 的调用次数必须是 0

### 通过阈值

Gate 1 窗口内，**新链路相关**的 `OllamaClient.chat` 调用 = 0。
老链路 Level1/Level2 的调用量不受此约束（它们就是要调的）。

### 观测方式

方法 A：**日志计数**（最简单，但依赖日志格式稳定）

```bash
# Ollama 客户端在 chat 开始时的典型日志片段（按实际实现调整）
grep -c "OllamaClient.chat" logs/service.log

# 分组看来源：老链路会带 source=twitter/binance_square/discord 上下文
grep "OllamaClient.chat" logs/service.log | grep -vE "level1|level2" | wc -l
# 期望：输出 0
```

方法 B：**代码层静态检查**（更严谨）

```bash
# 新链路 5 个文件不得 import ollama_client
grep -l "ollama_client\|OllamaClient" services/l0_dedup.py services/l0_normalizer.py \
    services/l1_entity_extractor.py services/l2_sliding_counter.py services/l2_hotness.py

# 期望：仅在 docstring / 注释里出现 "ollama" 字样（可用 grep -n 逐个人工确认），
# 真实的 import 语句一条都不应有
```

方法 C：**单测回归**（已在 Task 8.5 覆盖，CI 天然验证）

```bash
.venv/bin/python -m pytest tests/test_phase1_pipeline.py -v
# test_phase1_pipeline_end_to_end 内部 assert mock_chat.call_count == 0
# test_phase1_pipeline_zero_llm_import_smoke 验证 import 期不触发调用
```

### 失败诊断流程

若方法 A 发现非老链路来源的 chat 调用：

1. `grep -B5 "OllamaClient.chat" logs/service.log` 看调用栈
2. 找到是哪个模块引入的（通常是误 import 传染）
3. 立刻修代码 + **拒绝 Gate 1 通过**（这是核心承诺，不能放过）

---

## 8. 验收通过标准（综合）

全部满足才算 Gate 1 通过：

- [ ] 指标 1 稳定性：72h 不崩溃、连接/内存无泄漏
- [ ] 指标 2 产出节奏：累计 ≥ 5472 条，无 > 17min 缺口
- [ ] 指标 3 命中率：连续 3 天均值 ≥ 60%
- [ ] 指标 4 去重率：10% ≤ ratio ≤ 60%
- [ ] 指标 5 两通路可观测：`confidence ∈ {1.0, 0.95}` 均有记录
- [ ] 指标 6 老链路：日产出变化 ≤ 10%
- [ ] 指标 7 零 LLM：新链路 chat 调用 = 0

任何一项未达标 → 不通过，按 `docs/rollback_plan.md` 流程处理，或根据失败
诊断修复后重启 Gate 1 窗口（重新起算 72h）。
