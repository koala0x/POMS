# 配置参数速查手册

> 系统所有 60 个运行时参数的速查表，按"我现在想干嘛"组织。
>
> **改参数流程**：找到对应字段所在的 `config/_*.py` 文件 → 改默认值 → 重启服务。
> 配置都在 `frozen=True` 的 dataclass 里，**不读 `.env` / 环境变量**。
>
> 配置文件物理拆分如下：
>
> | 文件 | 分组类 | 管什么 | 参数数 |
> |---|---|---|---|
> | `config/_database.py` | `DatabaseSettings` | PG 连接 | 5 |
> | `config/_runtime.py` | `RuntimeSettings` | 日志 / 时区 / worker 调度 | 4 |
> | `config/_llm.py` | `LLMSettings` | Ollama 服务（仅 BriefingService 用） | 3 |
> | `config/_alerts.py` | `AlertSettings` | Telegram + 整点告警 + 实时通道 | 14 |
> | `config/_new.py` | `NewPipelineSettings` | 业务流水线参数 | 34 |
>
> 全部通过 `config/settings.py` 多继承组装为扁平 `Settings`，业务代码用
> `settings.<field>` 直接访问。

---

## 目录

1. [总开关清单（一眼看清谁开了谁关了）](#1-总开关清单一眼看清谁开了谁关了)
2. [DatabaseSettings — PG 连接](#2-databasesettings--pg-连接)
3. [RuntimeSettings — 日志 / 时区 / worker](#3-runtimesettings--日志--时区--worker)
4. [LLMSettings — Ollama 服务](#4-llmsettings--ollama-服务)
5. [AlertSettings — Telegram + 整点告警 + 实时](#5-alertsettings--telegram--整点告警--实时)
6. [NewPipelineSettings — 业务流水线](#6-newpipelinesettings--业务流水线)
   - [6.1 L0 NormalizerService](#61-l0-normalizerservice)
   - [6.2 L0 Deduplicator](#62-l0-deduplicator)
   - [6.3 L1 EntityExtractor](#63-l1-entityextractor)
   - [6.4 L2 SlidingCounter 启动回填](#64-l2-slidingcounter-启动回填)
   - [6.5 L2 HotnessService(1h)](#65-l2-hotnessservice1h)
   - [6.6 L2 HotnessService(6h)](#66-l2-hotnessservice6h)
   - [6.7 L2 HotnessService(24h)](#67-l2-hotnessservice24h)
   - [6.8 L3 CooccurrenceService](#68-l3-cooccurrenceservice)
   - [6.9 L5 BriefingService](#69-l5-briefingservice)
7. [常见调参场景](#7-常见调参场景)

---

## 1. 总开关清单（一眼看清谁开了谁关了）

| 开关 | 文件 | 默认 | 作用 |
|---|---|---|---|
| `telegram_bot_token` / `telegram_chat_id` | `_alerts.py` | 已填值 | 任一为空 → AlertTriggerService 不构造，整个告警系统跳过 |
| `realtime_enabled` | `_alerts.py` | `True` | False → RealtimeAlertService 不构造，实时通道关闭 |
| `hotness_6h_enabled` | `_new.py` | `True` | False → 6h 榜不算（hotness_snapshots 表里 `window_type='6h'` 不写） |
| `hotness_24h_enabled` | `_new.py` | `True` | False → 24h 榜同上 |
| `cooccur_enabled` | `_new.py` | `True` | False → CooccurrenceService 不构造，entity_cooccurrence 表不写 |
| `briefing_enabled` | `_new.py` | `True` | False → BriefingService 不构造，不调 LLM，entity_briefings 表不写 |

**最小可用配置**（只要核心榜单不要告警/简报）：把上面 6 个开关全关，只留 1h 榜。

---

## 2. DatabaseSettings — PG 连接

文件：`config/_database.py`，5 个字段。改完重启。

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `db_host` | str | `192.168.1.219` | PG 主机；可填 IP / 域名 / 容器名 |
| `db_port` | int | `5432` | PG 端口 |
| `db_name` | str | `all_new` | 业务库名；与上游 API 服务共享同一库 |
| `db_user` | str | `all_new` | 连接用户名 |
| `db_password` | str | `123qwe` | 明文密码；生产环境建议改成走环境变量（暂未实现） |

**注意**：改完务必先用 `psql` 测一下新地址通不通，再重启服务，免得起来又挂。

---

## 3. RuntimeSettings — 日志 / 时区 / worker

文件：`config/_runtime.py`，4 个字段。

| 字段 | 类型 | 默认 | 取值 | 何时调 |
|---|---|---|---|---|
| `poll_interval_seconds` | int | `30` | 5~300 | worker 空闲 sleep 间隔；调试期可调成 5；生产 30~60 |
| `log_path` | str | `./logs/service.log` | — | 日志路径，按天滚动 |
| `log_retention_days` | int | `30` | 1~90 | 旧日志保留天数，超过自动清理 |
| `timezone` | ZoneInfo | `UTC` | 任何合法 tz | 影响 `window_end` 显示，**不影响存储**（DB 列是 TIMESTAMPTZ） |

**为啥不在这放 `disable_legacy_pipeline`**：老链路 2026-05 已淘汰（详见 FAQ Q1），开关已删。

---

## 4. LLMSettings — Ollama 服务

文件：`config/_llm.py`，3 个字段。**只有 BriefingService 用**。

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `ollama_base_url` | str | `http://192.168.1.219:11434` | Ollama 服务地址；★ 必须监听 `0.0.0.0` 而非 `127.0.0.1`（在 Ollama 机器上 `OLLAMA_HOST=0.0.0.0:11434 ollama serve`） |
| `ollama_model_level5` | str | `qwen3:8b` | BriefingService 用的模型；30b 质量更高但单条 ~90s 太慢，保持 8b 即可 |
| `ollama_timeout_level5` | int | `600` | 单次请求超时（秒）；实测平均 30s/次，600s 是兜底 |

**关闭整个 LLM 链路**：把 `briefing_enabled=False`（在 `_new.py`），LLM 客户端都不构造。

---

## 5. AlertSettings — Telegram + 整点告警 + 实时

文件：`config/_alerts.py`，14 个字段。

### 5.1 Telegram 凭据（3 个）

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `telegram_bot_token` | str | （已填）| BotFather `/newbot` 给的 token；空 = 禁用告警 |
| `telegram_chat_id` | str | （已填）| getUpdates API 拿的 chat_id；私聊正整数，群组负整数；空 = 禁用 |
| `telegram_timeout_seconds` | int | `10` | HTTP 超时；Telegram API 一般 < 1s，10s 防卡死 |

### 5.2 整点告警触发门槛（3 个）

三道门槛**与门**关系，全满足才进入冷却判断；任一不满足直接跳过。

| 字段 | 类型 | 默认 | 调大效果 | 调小效果 |
|---|---|---|---|---|
| `alert_growth_threshold` | float | `5.0` | 告警更稀，只接 super hot | 告警更频，可能噪音多 |
| `alert_min_count_short` | int | `2` | 过滤"提及很少但 growth 高"的边缘信号 | 边缘信号也告警，噪音多 |
| `alert_min_cross_source` | int | `1` | 只接多源共振信号；调成 2 → 必须 ≥2 个源同时聊 | 单源也告警 |

> 调参节奏：部署 1 周后查 hotness_snapshots 实际 growth 99% 分位再调。

### 5.3 智能冷却 4 路径决策树（3 个）

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `alert_cooldown_minutes` | int | `60` | 同实体冷却期；期间只在"质变"时再告警 |
| `alert_escalation_growth_multiplier` | float | `1.5` | growth 升级倍数；本次 ≥ 上次 × 此值 → [升级] |
| `alert_heartbeat_hours` | int | `6` | 持续热点最长不告警时长；超过即便没质变也再发一次 [持续 Nh] |

### 5.4 消息渲染（1 个）

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `alert_message_template` | str | `_DEFAULT_TEMPLATE`（多行模板）| 占位符：`{alert_type}` `{entity}` `{entity_type}` `{growth_rate}` `{count_short}` `{cross_source}` `{is_new_entity_mark}` `{window_end}` `{rank}` |

**LLM 简报附加**：模板渲染完后，AlertTriggerService 自动追加 `📰 narrative \| catalyst` 一行（如果 `briefing_enabled=True` 且查到了上一轮 briefing）。

### 5.5 实时通道（4 个）

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `realtime_enabled` | bool | `True` | False → 整个实时通道关闭，整点通道不受影响 |
| `realtime_burst_threshold` | int | `50` | 累积多少条新提及触发一次实时计算；低流量期 5~10 轮（25~50s）攒满 |
| `realtime_growth_threshold` | float | `30.0` | 比整点严：分钟级抖动大，不建议 < 10 |
| `realtime_min_count_short` | int | `5` | 比整点严：防"3 条 KOL 同话题转发"误触发 |

> 共享冷却 dict：实时通道与整点通道写**同一个** `_alert_records`（main.py 注入同一引用），同 entity 60 分钟内最多发 1 条（不分通道）。

---

## 6. NewPipelineSettings — 业务流水线

文件：`config/_new.py`，34 个字段。按 service 分组列。

### 6.1 L0 NormalizerService

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `normalizer_batch_size` | int | `500` | 每轮三源各扫多少条；500×3=单轮最多 1500 条；积压大可调到 2000 |

### 6.2 L0 Deduplicator

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `dedup_hamming_threshold` | int | `3` | SimHash 汉明距离 ≤ 此值视为重复；调大（→5）更激进，上限 6 |
| `dedup_window_hours` | int | `24` | 判重历史窗口（内存桶）；老于此值的桶自动清理 |

### 6.3 L1 EntityExtractor

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `entity_extractor_batch_size` | int | `500` | 每轮从 normalized_messages 取多少条未处理消息 |

### 6.4 L2 SlidingCounter 启动回填

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `sliding_counter_backfill_max_seconds` | int | `600` | 硬上限，超过强制中止回填 |
| `sliding_counter_backfill_warn_seconds` | int | `120` | < 此值算快速成功（INFO）；区间内算慢速成功（WARN） |

### 6.5 L2 HotnessService(1h)

短窗 1h，对应 `window_type='1h'`，**必需**（构造失败 main.py 直接 raise）。

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `hotness_top_k` | int | `20` | 排行榜 Top-K |
| `hotness_smoothing` | float | `2.0` | growth 公式分母平滑值；避免基线=0 时除零 |
| `hotness_short_hours` | int | `1` | 短窗时长（小时）；与 `window_type='1h'` 必须自洽 |
| `hotness_baseline_days` | int | `7` | 基线窗（天）；调短对突变更敏感 |
| `hotness_min_baseline_count` | int | `100` | 近 7 天 entity_mentions 总数 < 此值跳过本轮（冷启动期保护） |
| `hotness_exclude_entities` | tuple | 7 个稳定币+巨头 | 输出黑名单：BTC/ETH/SOL/BNB/USDT/USDC/DAI；只过滤输出，不影响基线统计 |

**冷启动想尽快出榜**：把 `hotness_min_baseline_count` 临时改 100 → 20，重启即出榜。

### 6.6 L2 HotnessService(6h)

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `hotness_6h_enabled` | bool | `True` | 总开关 |
| `hotness_6h_top_k` | int | `20` | Top-K |
| `hotness_6h_smoothing` | float | `5.0` | 6h 噪音比 1h 小，smoothing 等比放大 |
| `hotness_6h_baseline_days` | int | `7` | 基线（天）；6h baseline_hours=7×24-6=162 |
| `hotness_6h_min_baseline_count` | int | `200` | 比 1h 翻倍 |
| `hotness_6h_exclude_entities` | tuple | 与 1h 同 | 6h 黑名单 |

### 6.7 L2 HotnessService(24h)

24h 默认**不**屏蔽 BTC/ETH（24h 维度它们的 growth 突变是真信号），只屏蔽稳定币。

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `hotness_24h_enabled` | bool | `True` | 总开关 |
| `hotness_24h_top_k` | int | `20` | Top-K |
| `hotness_24h_smoothing` | float | `10.0` | 最大；避免冷启动 growth 爆炸 |
| `hotness_24h_baseline_days` | int | `8` | ★ **必须 ≥ 8**（baseline_days×24 - short_hours > 0） |
| `hotness_24h_min_baseline_count` | int | `500` | 长窗需要更多样本；冷启动期 24h 榜空 8~12 小时是预期 |
| `hotness_24h_exclude_entities` | tuple | 仅 USDT/USDC/DAI | 24h 黑名单 |

### 6.8 L3 CooccurrenceService

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `cooccur_enabled` | bool | `True` | 总开关 |
| `cooccur_window_type` | str | `"24h"` | 共现窗口；1h 噪音太大，24h 才稳定 |
| `cooccur_top_pairs` | int | `100` | 每窗口写 Top-K pair（按 PMI 降序） |
| `cooccur_min_cooccur_count` | int | `3` | 共现 1~2 次属偶然，3 次起算趋势 |
| `cooccur_min_pmi` | float | `1.0` | PMI ≥ 此值才写库；1.0 ≈ 共现概率是独立预期的 e≈2.7 倍 |
| `cooccur_min_window_msgs` | int | `50` | 窗口内消息数 < 此值跳过本轮（数据稀疏保护） |

> 部署 1~2 周后查 `entity_cooccurrence.pmi` 99% 分位再调 `cooccur_min_pmi`。

### 6.9 L5 BriefingService

每 15 分钟整点取最新 1h 榜 Top-N，给 growth ≥ 阈值的实体调 LLM 出 JSON 简报。

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `briefing_enabled` | bool | `True` | 总开关 |
| `briefing_top_n` | int | `5` | 每轮处理 1h 榜 Top-N；调大 → 单轮耗时长，可能拖累 worker |
| `briefing_min_growth` | float | `5.0` | growth ≥ 此值才调 LLM；过滤温和上涨节省推理 |
| `briefing_evidence_count` | int | `10` | 喂 LLM 的代表消息数；10 条 × 200 字 ≈ 2000 token，远低于 16384 上下文 |

> Ollama 模型 / 超时去 §4 LLMSettings 调（`ollama_model_level5` / `ollama_timeout_level5`）。

---

## 7. 常见调参场景

### 7.1 想让告警更频 / 更稀

```python
# config/_alerts.py
alert_growth_threshold: 5.0 → 3.0   # 更频；3.0 ≈ 当前数据流量下中等热度
alert_growth_threshold: 5.0 → 20.0  # 更稀；只接超热信号
```

调完看 `grep "alert sent" logs/service.log | wc -l` 一周计数对比。

### 7.2 想让 LLM 简报覆盖更广

```python
# config/_new.py
briefing_top_n: 5 → 10           # 每轮多处理 5 个
briefing_min_growth: 5.0 → 2.0   # 让"温和上涨"也带简报
```

副作用：单轮 worker 时长 ~2.5min → ~5min；CPU 占用上升。

### 7.3 想换更大的 LLM 模型

```python
# config/_llm.py
ollama_model_level5: "qwen3:8b" → "qwen3:30b"
ollama_timeout_level5: 600 → 900   # 30b 推理 ~90s/次，给足余量
```

注意：先在 Ollama 端 `ollama pull qwen3:30b`，否则启动时第一次调用会拉取（~20GB 下载）。

### 7.4 想关掉某个可选服务

```python
# config/_alerts.py
realtime_enabled: True → False     # 关实时通道
telegram_bot_token: "..." → ""     # 关整个告警

# config/_new.py
hotness_6h_enabled: True → False   # 关 6h 榜
hotness_24h_enabled: True → False  # 关 24h 榜
cooccur_enabled: True → False      # 关共现网络
briefing_enabled: True → False     # 关 LLM 简报（连 OllamaClient 都不构造）
```

任一开关关掉 → 对应 service 不进 worker，**hotness 主链路完全不受影响**。

### 7.5 想加速冷启动看到产出

```python
# config/_new.py
hotness_min_baseline_count: 100 → 20      # 1h 榜首日就出
hotness_24h_min_baseline_count: 500 → 100 # 24h 榜首日就出（数据稀薄会有噪音）
poll_interval_seconds: 30 → 5             # worker 醒得更频繁（仅调试用）
```

冷启动期过后**记得改回默认值**（基线门槛太低 = 噪音爆炸）。

### 7.6 想做"安静运行"（最小输出）

```python
# config/_new.py
briefing_enabled: False
cooccur_enabled: False
hotness_6h_enabled: False
hotness_24h_enabled: False

# config/_alerts.py
realtime_enabled: False
# telegram_bot_token / chat_id 留着，AlertTriggerService 还会启动整点告警
```

这样 worker 只跑 Normalizer / EntityExtractor / Hotness(1h) / AlertTrigger 4 个 service，**资源占用最小**。

---

## 附录：参数总览（按字段名字母序）

按字母序检索某个字段所在文件 / 默认值，便于"突然记不起这个字段在哪"。

| 字段名 | 文件 | 默认值 |
|---|---|---|
| `alert_cooldown_minutes` | `_alerts.py` | `60` |
| `alert_escalation_growth_multiplier` | `_alerts.py` | `1.5` |
| `alert_growth_threshold` | `_alerts.py` | `5.0` |
| `alert_heartbeat_hours` | `_alerts.py` | `6` |
| `alert_message_template` | `_alerts.py` | `_DEFAULT_TEMPLATE` |
| `alert_min_count_short` | `_alerts.py` | `2` |
| `alert_min_cross_source` | `_alerts.py` | `1` |
| `briefing_enabled` | `_new.py` | `True` |
| `briefing_evidence_count` | `_new.py` | `10` |
| `briefing_min_growth` | `_new.py` | `5.0` |
| `briefing_top_n` | `_new.py` | `5` |
| `cooccur_enabled` | `_new.py` | `True` |
| `cooccur_min_cooccur_count` | `_new.py` | `3` |
| `cooccur_min_pmi` | `_new.py` | `1.0` |
| `cooccur_min_window_msgs` | `_new.py` | `50` |
| `cooccur_top_pairs` | `_new.py` | `100` |
| `cooccur_window_type` | `_new.py` | `"24h"` |
| `db_host` | `_database.py` | `"192.168.1.219"` |
| `db_name` | `_database.py` | `"all_new"` |
| `db_password` | `_database.py` | `"123qwe"` |
| `db_port` | `_database.py` | `5432` |
| `db_user` | `_database.py` | `"all_new"` |
| `dedup_hamming_threshold` | `_new.py` | `3` |
| `dedup_window_hours` | `_new.py` | `24` |
| `entity_extractor_batch_size` | `_new.py` | `500` |
| `hotness_24h_baseline_days` | `_new.py` | `8` |
| `hotness_24h_enabled` | `_new.py` | `True` |
| `hotness_24h_exclude_entities` | `_new.py` | 仅稳定币 |
| `hotness_24h_min_baseline_count` | `_new.py` | `500` |
| `hotness_24h_smoothing` | `_new.py` | `10.0` |
| `hotness_24h_top_k` | `_new.py` | `20` |
| `hotness_6h_baseline_days` | `_new.py` | `7` |
| `hotness_6h_enabled` | `_new.py` | `True` |
| `hotness_6h_exclude_entities` | `_new.py` | 7 个 |
| `hotness_6h_min_baseline_count` | `_new.py` | `200` |
| `hotness_6h_smoothing` | `_new.py` | `5.0` |
| `hotness_6h_top_k` | `_new.py` | `20` |
| `hotness_baseline_days` | `_new.py` | `7` |
| `hotness_exclude_entities` | `_new.py` | 7 个 |
| `hotness_min_baseline_count` | `_new.py` | `100` |
| `hotness_short_hours` | `_new.py` | `1` |
| `hotness_smoothing` | `_new.py` | `2.0` |
| `hotness_top_k` | `_new.py` | `20` |
| `log_path` | `_runtime.py` | `"./logs/service.log"` |
| `log_retention_days` | `_runtime.py` | `30` |
| `normalizer_batch_size` | `_new.py` | `500` |
| `ollama_base_url` | `_llm.py` | `"http://192.168.1.219:11434"` |
| `ollama_model_level5` | `_llm.py` | `"qwen3:8b"` |
| `ollama_timeout_level5` | `_llm.py` | `600` |
| `poll_interval_seconds` | `_runtime.py` | `30` |
| `realtime_burst_threshold` | `_alerts.py` | `50` |
| `realtime_enabled` | `_alerts.py` | `True` |
| `realtime_growth_threshold` | `_alerts.py` | `30.0` |
| `realtime_min_count_short` | `_alerts.py` | `5` |
| `sliding_counter_backfill_max_seconds` | `_new.py` | `600` |
| `sliding_counter_backfill_warn_seconds` | `_new.py` | `120` |
| `telegram_bot_token` | `_alerts.py` | （已填）|
| `telegram_chat_id` | `_alerts.py` | （已填）|
| `telegram_timeout_seconds` | `_alerts.py` | `10` |
| `timezone` | `_runtime.py` | `UTC` |
