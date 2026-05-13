# 日常运维操作手册

> 给首次接触本项目、需要运维 / 调试的人看。用 Android 开发者熟悉的概念类比，
> 不涉及数据分析术语。

---

## 0. 这是个啥系统？一句话版

它是一个**后台常驻进程**，每 30 秒醒一次，从数据库几张表里拿新消息，加工后
写到另几张表。最终产物是一份**"最近 15 分钟加密圈谁在被热议"的排行榜**。

对应 Android 开发：就是一个永不退出的 `Service` + `AlarmManager`，读 Room
数据库 A 几张表、写 Room 数据库 B 几张表。就这么朴素。

### 系统有两条独立的流水线

```
┌─ 老链路（调 Ollama LLM 做文本摘要）──────────────────────────────┐
│  twitter_posts/binance_square_posts/discord_messages             │
│    → Level1Service →  summary_level1                             │
│    → Level2Service →  summary_level2                             │
└──────────────────────────────────────────────────────────────────┘

┌─ 新链路（Phase 1，纯统计，不调 LLM）────────────────────────────┐
│  twitter_posts/binance_square_posts/discord_messages             │
│    → NormalizerService    → normalized_messages                  │
│    → EntityExtractor      → entity_mentions                      │
│    → HotnessService       → hotness_snapshots（最终产品）        │
└──────────────────────────────────────────────────────────────────┘
```

两条链路**共用一个后台线程串行跑**（避免 Ollama 模型反复 swap），但彼此
数据隔离、互不影响。任何时候想"砍掉"新链路回到只跑老链路，改 `main.py`
里一行就行（见 `docs/rollback_plan.md`）。

---

## 1. Android 开发 → 本项目概念对照

| 本项目 | Android 类比 | 要点 |
|---|---|---|
| `main.py` | 一个开机自启 Service 的 `onCreate` | 跑起来就常驻，直到 Ctrl+C |
| `scheduler/jobs.py` | `AlarmManager.setRepeating` 的回调 | 每 `poll_interval_seconds` 跑一轮 |
| worker 线程 | 一个单独的 HandlerThread | 所有任务串行，不会卡 UI（这里就是主线程）|
| PostgreSQL | Room 数据库 | 只是跑在 `192.168.1.219` 那台机器上 |
| ORM 表类（db/models.py）| Room `@Entity` 类 | 字段定义清单 |
| `.venv/bin/python` | 项目专用的解释器路径 | 不要用系统 python，`.venv/bin/pip` 也别用（shebang 指向老路径） |
| `logs/service.log` | Logcat 日志 | `tail -f` 看实时 |
| `config/settings.py` | `BuildConfig` / `res/values/config.xml` | 所有运行参数的唯一源 |
| Ollama HTTP API | Retrofit 调一个 REST API | 给文本、拿文本，超时就失败重试下一轮 |

---

## 2. 最小可用三件套（90% 日常足够）

### 启动

```bash
cd /Users/ye/Work/Crypto/PomsAI
.venv/bin/python main.py
```

正常启动日志长这样（按顺序，看到最后一行就对了）：

```
数据库连接已初始化
老链路已关闭（settings.disable_legacy_pipeline=True）：跳过 Ollama 客户端与 Level1/Level2 Service 初始化
词典就绪：tickers=57 chains=0 narratives=0 kols=0 aliases=64
SlidingCounter backfill 结束：ok=True total=<N> elapsed=<X.X>s
HotnessService(1h)  启动：top_k=20 smoothing=2.0  baseline_days=7 min_baseline_count=100
HotnessService(6h)  启动：top_k=20 smoothing=5.0  baseline_days=7 min_baseline_count=200
HotnessService(24h) 启动：top_k=20 smoothing=10.0 baseline_days=8 min_baseline_count=500
AlertTriggerService 启动：growth_threshold=20.0 cooldown=60min escalation×1.5 heartbeat=6h
服务启动成功:worker 只跑新链路 (Phase 1 new services=6，空闲 sleep 30s)
```

> 说明：`disable_legacy_pipeline` 默认 `True`，只跑新链路（Phase 1 热度排行榜）。
> 想把老链路 LLM 摘要打开，改 `config/settings.py` 里 `disable_legacy_pipeline: bool = True` → `False`，
> 然后重启服务。代码本身不用动。
>
> **Phase 2.1 新增**：`HotnessService` 现在跑三个实例（1h / 6h / 24h），分别产出
> 短中长三档窗口的排行榜，全部写到同一张 `hotness_snapshots` 表（`window_type` 列区分）。
> 任一窗口实例可通过 `hotness_6h_enabled` / `hotness_24h_enabled` 配置关闭（详见 §6.2）。
> 6h/24h 实例构造失败时只 log.error 不阻塞启动，1h 必需。
>
> AlertTriggerService 是 Phase 2.2 新增的 Telegram 告警服务。`telegram_bot_token` /
> `telegram_chat_id` 任一为空时不会出现这一行启动日志，会变成
> `Telegram 告警未配置（token/chat_id 为空），已禁用`，告警系统整体跳过初始化，
> hotness 主流程不受影响。详见 §6.1 "Telegram 告警调参"。
>
> **AlertTriggerService 当前只读 1h 榜**，新增的 6h / 24h 榜对它完全透明。
> 未来 Phase 2.2.1 会扩展成多通道告警（给 6h / 24h 各配独立 threshold）。

后台跑：

```bash
nohup .venv/bin/python main.py > /dev/null 2>&1 &
```

### 看日志

```bash
# 实时跟踪（Ctrl+C 退出）
tail -f logs/service.log

# 只看 WARN 和 ERROR
grep -E "WARNING|ERROR" logs/service.log | tail -50

# 查某个环节最近一轮有没有干活
grep "normalizer 本轮"       logs/service.log | tail -3
grep "entity_extractor 本轮" logs/service.log | tail -3
grep "hotness window_end"    logs/service.log | tail -3
```

### 停止

前台跑：`Ctrl+C`（优雅停机，worker 走到当前 service 完成后退出）。

后台跑：

```bash
kill -INT $(pgrep -f "python.*main.py")
# 等 15 秒它自然退出；如果仍在再 kill -9（极少见）
```

**不要直接 `kill -9`**，那会跳过 `jobs.shutdown`，可能留下半提交事务。

---

## 3. "它在干活吗"自检（每次看 30 秒搞定）

打开数据库客户端（psql / DBeaver / pgAdmin 都行），连到 `192.168.1.219:5432`，
库 `all_new`，用户 `all_new`，密码 `123qwe`（`config/settings.py` 里都有）。

跑这 5 条 SQL：

```sql
-- 新链路三张表的"最近一次更新时间"
SELECT 'normalized_messages' AS tbl, max(created_at) FROM normalized_messages
UNION ALL
SELECT 'entity_mentions',       max(ts)               FROM entity_mentions
UNION ALL
SELECT 'hotness_snapshots',     max(window_end)       FROM hotness_snapshots;

-- 老链路的"最近一次更新时间"
SELECT 'summary_level1', max(created_at) FROM summary_level1
UNION ALL
SELECT 'summary_level2', max(created_at) FROM summary_level2;
```

**判断标准**：

- 5 行时间戳都在最近 30 分钟内 → 健康
- 新链路 hotness 时间戳 > 30 分钟未更新 → 见"场景 A"排查
- 所有时间戳都是几天前 → 多半进程死了，回头查日志

### 看最新一份热榜

```sql
SELECT rank, entity, entity_type, count_short,
       round(cast(growth_rate as numeric), 2) AS growth,
       cross_source,
       round(cast(final_score as numeric), 2) AS score,
       is_new_entity
FROM hotness_snapshots
WHERE window_end = (
  SELECT max(window_end) FROM hotness_snapshots WHERE window_type='1h'
)
  AND window_type = '1h'
ORDER BY rank ASC;
```

各列含义：

| 列 | 含义 |
|---|---|
| `rank` | 名次 1~20 |
| `entity` | 被讨论的对象（币符号、公链、合约地址等） |
| `count_short` | 过去 1 小时被提到的次数 |
| `growth_rate` | 短窗提及量 / 近 7 天每小时平均量，越大越"突然热" |
| `cross_source` | 在几个数据源出现过（1~3），越多越"共识度高" |
| `final_score` | `growth × (1 + 0.3 × (cross_source - 1))`，最终排序依据 |
| `is_new_entity` | 基线期 0 次、短窗 ≥ 5 次的"新冒头"实体 |

---

## 4. 调试三大场景（出问题时对照）

### 场景 A：hotness_snapshots 长时间不更新

优先级从上到下：

```bash
# 1. 进程还活着吗？
pgrep -fl "python.*main.py"

# 2. 最近 worker 还在转吗（应该每 30s 有输出）
tail -n 100 logs/service.log

# 3. hotness 明确跳过的原因
grep "hotness skipped" logs/service.log | tail -10
```

**两种最常见的"hotness skipped"**：

- `sliding counter not ready` → 启动回填还没完成，等一两轮（每轮 30s）自动好
- `baseline data insufficient (count=<N> < 100)` → 基线数据不够。这是冷启动正常
  现象，`entity_mentions` 表的 7 天记录累积到 100 条后就开始出榜。不想等：
  改 `config/settings.py` 把 `hotness_min_baseline_count: 100` 调小（比如 20），
  重启服务。

**如果都不是上面两种**：grep ERROR 看真实异常：

```bash
grep "ERROR" logs/service.log | tail -20
```

### 场景 B：entity_mentions 表不涨（上游卡了）

先确认 `normalized_messages` 有没有新数据：

```sql
SELECT count(*), max(ts) FROM normalized_messages;
```

- 如果 `normalized_messages` 也不涨 → 问题在 Normalizer，进一步查三张原始表：
  `SELECT count(*), max(created_at) FROM twitter_posts WHERE id NOT IN (SELECT raw_id FROM normalized_messages WHERE raw_source='twitter');`
  如果返回 0，说明上游根本没新数据，不是我们的问题（跟产 Twitter 数据的
  服务沟通）
- 如果 `normalized_messages` 涨了但 `entity_mentions` 不涨 → EntityExtractor
  卡了，去 `grep "entity_extractor" logs/service.log | tail -20` 找 ERROR

### 场景 C：老链路 summary_level1/level2 产出下降

这是硬约束：Phase 1 绝不能让老链路产出变少。如果看到这个现象：

1. 立刻走 `docs/rollback_plan.md` 的流程回滚（改 `main.py` 移除新 service 后重启）
2. 回滚后观察老链路是否恢复
3. 如果恢复了 → 说明确实是新链路抢了资源，联系开发者定位
4. 如果没恢复 → 可能是 Ollama 服务端本身问题，检查 `http://192.168.1.219:11434`

---

## 5. 手动触发单个环节（调试神器）

这是 Android 开发里"写个按钮手动调用调试方法"的同款思路。想看某个 service
跑一轮实际会干啥，不用等 30 秒自动轮询：

### 手动跑一次 Normalizer

```bash
.venv/bin/python <<'EOF'
from config.settings import get_settings
from db.connection import Database
from db.repositories.normalized_messages_repo import NormalizedMessagesRepo
from services.l0_dedup import Deduplicator
from services.l0_normalizer import NormalizerService

settings = get_settings()
db = Database(settings)
dedup = Deduplicator(
    hamming_threshold=settings.dedup_hamming_threshold,
    window_hours=settings.dedup_window_hours,
)
dedup.backfill_from_db(db)

svc = NormalizerService(
    db=db,
    normalized_repo=NormalizedMessagesRepo(),
    dedup=dedup,
    batch_size=settings.normalizer_batch_size,
    timezone=settings.timezone,
)
result = svc.run_once()
print("有没有扫到新数据？", result)
EOF
```

屏幕上的 INFO 日志会告诉你"扫描 X 条 → 写入 Y 条（重复 Z 条，空内容跳过 W 条）"。

### 手动跑一次 EntityExtractor + HotnessService

```bash
.venv/bin/python <<'EOF'
from config.settings import get_settings
from db.connection import Database
from db.repositories.entity_mentions_repo import EntityMentionsRepo
from db.repositories.hotness_snapshots_repo import HotnessSnapshotsRepo
from db.repositories.normalized_messages_repo import NormalizedMessagesRepo
from services.l1_entity_extractor import EntityExtractor
from services.l2_hotness import HotnessService
from services.l2_sliding_counter import SlidingCounter

settings = get_settings()
db = Database(settings)
sc = SlidingCounter()
sc.backfill_from_db(db)

extractor = EntityExtractor(
    db=db,
    normalized_repo=NormalizedMessagesRepo(),
    mentions_repo=EntityMentionsRepo(),
    sliding_counter=sc,
    batch_size=settings.entity_extractor_batch_size,
)
for _ in range(3):
    if not extractor.run_once():
        break
print("EntityExtractor 跑完了")

hotness = HotnessService(
    db=db,
    mentions_repo=EntityMentionsRepo(),
    hotness_repo=HotnessSnapshotsRepo(),
    sliding_counter=sc,
    top_k=settings.hotness_top_k,
    smoothing=settings.hotness_smoothing,
    short_hours=settings.hotness_short_hours,
    baseline_days=settings.hotness_baseline_days,
    min_baseline_count=settings.hotness_min_baseline_count,
    timezone=settings.timezone,
)
result = hotness.run_once()
print("Hotness 出榜了吗？", result)
EOF
```

---

## 6. 改参数（后台调优）

所有配置都在 `config/settings.py`，是个 `@dataclass`。流程：

1. 先停服务（`Ctrl+C` 或 `kill -INT`）
2. 编辑 `config/settings.py`，改字段默认值
3. 重新启动

### 常用调优点速查

| 想干嘛 | 改哪个字段 | 说明 |
|---|---|---|
| 打开 / 关闭老链路 LLM 摘要 | `disable_legacy_pipeline: True` ↔ `False` | True=只跑新链路；False=老+新并行；关了老链路 Ollama 压根不会被初始化 |
| 冷启动期想尽快看到排行榜 | `hotness_min_baseline_count: 100 → 20` | 降低"基线样本数"门槛；生产环境记得改回来 |
| worker 空闲时醒得更频繁（调试用） | `poll_interval_seconds: 30 → 5` | 30s 太慢看不到效果；生产建议 30~60s |
| SimHash 判重更激进 | `dedup_hamming_threshold: 3 → 5` | 数值越大越容易判成"重复"；上限 6 就差不多了 |
| 热榜条数从 20 改成 50 | `hotness_top_k: 20 → 50` | |
| 每轮归一化多扫点 | `normalizer_batch_size: 500 → 2000` | 积压大的时候加速消化；看 DB 能不能扛住 |
| 换老链路 LLM 模型 | `ollama_model_level1: "qwen3:8b" → "qwen3:30b"` | 先确认 Ollama 端 `ollama pull qwen3:30b` 过了；且 `disable_legacy_pipeline=False` 才生效 |
| 延长 LLM 超时 | `ollama_timeout_level1: 600 → 1200` | 上了大模型 CPU 慢就得加；同样仅 `disable_legacy_pipeline=False` 时生效 |
| **关闭 Telegram 告警** | `telegram_bot_token: "" / telegram_chat_id: ""` | 任一为空整个告警服务不构造，hotness 主流程不变（详见下面 6.1 小节） |
| **告警太频 / 太稀** | `alert_growth_threshold: 20.0 → 10.0 或 50.0` | 数字越大告警越稀。先观察 1 周再调（详见下面 6.1 小节） |
| **告警冷却时长** | `alert_cooldown_minutes: 60 → 30` | 缩短同实体两次告警的最小间隔；常规情况下 60 分钟够用 |
| **告警升级灵敏度** | `alert_escalation_growth_multiplier: 1.5 → 2.0` | 数字越大"升级告警"越难触发，越保守 |
| **持续热点心跳间隔** | `alert_heartbeat_hours: 6 → 12` | 持续热点最长不告警时长；调长则"持续 Nh"提醒更稀 |
| **关闭 6h 中期榜** | `hotness_6h_enabled: True → False` | False 时跳过该实例构造，零运行时开销；详见 §6.2 |
| **关闭 24h 长期榜** | `hotness_24h_enabled: True → False` | 同上 |
| **6h 榜灵敏度** | `hotness_6h_smoothing: 5.0 → 3.0`（更敏感）/ `→ 8.0`（更稳） | smoothing 是 growth 公式分母平滑值，**调小**让冷启动期 growth 更激进、调大更稳健 |
| **24h 榜灵敏度** | `hotness_24h_smoothing: 10.0 → 5.0` 或 `→ 20.0` | 同上 |
| **24h 榜屏蔽 BTC/ETH** | `hotness_24h_exclude_entities: ("USDT","USDC","DAI","BTC","ETH")` | 默认 24h 不屏蔽 BTC/ETH（看宏观信号），如果觉得吵就加回去 |

**注意**：改 DB 配置（`db_host` / `db_port` 等）后重启前先 `psql` 测一下新地址
通不通，免得进程起来又挂。

### 6.1 Telegram 告警调参（Phase 2 新增）

Telegram 告警的所有配置都在 `config/_alerts.py`（不在主 `settings.py`，是个分组）。
关闭整个告警系统：把 `telegram_bot_token` 或 `telegram_chat_id` 改成空字符串，
重启服务即可——AlertTriggerService 不会被构造，启动日志显示
`Telegram 告警未配置（token/chat_id 为空），已禁用`。

#### 调 threshold 的节奏

部署后 24 小时内**不要急着调**。先按默认 20.0 跑一周，观察告警频率：

| 现象 | 原因 | 动作 |
|---|---|---|
| 一周告警 ≥ 5 次 | 默认值合理 | 继续观察，不动 |
| 一周告警 0 次 | threshold 太高 | 调成 10.0；或先 SQL 看 99% 分位 |
| 一天告警 ≥ 10 次 | threshold 太低 | 调成 30.0~50.0；或调长 cooldown |

用 SQL 看实际 growth_rate 分布，决定 threshold 设多少：

```sql
SELECT
  percentile_cont(0.95) WITHIN GROUP (ORDER BY growth_rate) AS p95,
  percentile_cont(0.99) WITHIN GROUP (ORDER BY growth_rate) AS p99
FROM hotness_snapshots
WHERE window_end >= now() - INTERVAL '7 days';
-- p99 大约就是"每天告警 1 次左右"的 threshold
```

#### 联调验证（确认链路可达）

修改 token / chat_id 后，验证 Telegram 链路连通性最快的办法是临时把
`alert_growth_threshold` 调成 1.0 + 重启 → 下一份榜出来时几乎所有实体都会
触发告警，确认收到任意一条后立刻把 threshold 改回 20.0 + 再次重启。整个
过程 < 30 分钟。

也可以用纯 Python 一行验证（不重启服务）：

```bash
.venv/bin/python -c "
from config.settings import get_settings
from notifications.telegram_client import TelegramClient
s = get_settings()
client = TelegramClient(bot_token=s.telegram_bot_token, chat_id=s.telegram_chat_id)
print('send result:', client.send_text('PomsAI 联调测试'))
"
```

返回 `True` 说明 token/chat_id/网络全 OK。返回 `False` → 看日志里
`telegram http error` / `telegram network error` 哪一种，对应排查（详见
`docs/faq_design_decisions.md` Q6）。

#### 看告警相关日志

```bash
# 是否真的发出过告警
grep "alert sent" logs/service.log | tail -10

# 哪些实体被冷却跳过
grep "alert skipped" logs/service.log | tail -10

# Telegram API 报错
grep "telegram .*error" logs/service.log | tail -10
```

### 6.2 多窗口热度排行榜调参（Phase 2.1 新增）

Phase 2.1 把 `HotnessService` 由"单实例（1h）"扩展为"三实例（1h / 6h / 24h）"，
三份榜同时写到 `hotness_snapshots` 表，靠 `window_type` 列区分：

```
1h  ← Phase 1 已有，沿用全部默认值（不动）
6h  ← Phase 2.1 新增，中期信号（半天级趋势）
24h ← Phase 2.1 新增，长期信号（宏观新闻级事件）
```

#### 默认参数对照表

| 字段 | 1h（Phase 1）| 6h | 24h | 设计意图 |
|---|---|---|---|---|
| `enabled` | 永远开 | `True` | `True` | 6h/24h 可独立关闭 |
| `top_k` | 20 | 20 | 20 | 三窗口同样宽度 |
| `smoothing` | 2.0 | 5.0 | 10.0 | smoothing 等比放大，避免冷启动期 growth 虚高 |
| `baseline_days` | 7 | 7 | **8** | 24h 必须 ≥ 8（数学约束：`baseline_days*24 - short_hours > 0`）|
| `min_baseline_count` | 100 | 200 | 500 | 长窗信号需要更多样本才稳定 |
| `exclude_entities` | 屏蔽 7 种 | 同 1h | **只屏蔽稳定币** | 24h 维度的 BTC/ETH 大新闻是真信号 |

#### 选哪个窗口看什么

| 维度 | 用途 | 例子 |
|---|---|---|
| 1h | 立刻冒头的瞬间热点 | `$WIFHAT` 5 分钟前突然被刷屏 |
| 6h | 半天级中期趋势 | 某个叙事下午开始有人聊，到晚上还在烧 |
| 24h | 全天级宏观信号 | BTC 跌破关键支撑，全天讨论量翻 5 倍 |

#### 看三窗口数据的 SQL

```sql
-- 看每个窗口的最新榜单各 5 名
SELECT window_type, rank, entity, growth_rate, count_short, cross_source
FROM hotness_snapshots
WHERE (window_type, window_end) IN (
  SELECT window_type, MAX(window_end)
  FROM hotness_snapshots
  GROUP BY window_type
)
  AND rank <= 5
ORDER BY window_type, rank;

-- 看三窗口共振（同一 entity 在三个窗口都进 Top-10 = 强信号）
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

#### 冷启动期注意事项

24h 榜需要 `entity_mentions` 累计 ≥ 500 条才出榜，新部署服务后**头 8~12 小时**
24h 榜会是空的，日志里会看到：

```
hotness skipped: baseline data insufficient (count=<N> < 500)
```

这是正常行为不是 bug。等数据攒够自然出榜；想强行看可以临时改
`hotness_24h_min_baseline_count` 到 100，重启 + 下一个 quarter 就有。

#### 关闭某个窗口

```python
# config/_new.py
hotness_6h_enabled: bool = False   # 关掉 6h 榜
hotness_24h_enabled: bool = False  # 关掉 24h 榜
```

重启后日志会显示 `HotnessService(6h) 未启用（hotness_6h_enabled=False）`，
该实例不构造，零运行时开销。**1h 不能关**——它是核心窗口，关了等于没用。

#### 看多窗口相关日志

```bash
# 看三个窗口的 hotness 触发
grep "hotness window_end" logs/service.log | tail -10

# 看 24h 窗口的基线不足跳过
grep "baseline data insufficient" logs/service.log | tail -10

# 看 6h/24h 实例构造失败（一般是 settings 配错触发 __post_init__ raise）
grep "HotnessService(.*) 构造失败" logs/service.log | tail -5
```

---

## 7. 跑测试（验证改动没破坏东西）

改了代码 / 改了配置后，强烈建议先跑测试：

```bash
.venv/bin/python -m pytest tests/ --ignore=tests/test_ollama_client.py -q
```

应看到：

```
135 passed, 1 skipped in X.XXs
```

> 测试基线演进：Phase 1 109 → +3（黑名单）= 112 → Phase 2.2 +5（telegram_client）
> +11（l2_alert_trigger）= 128 → Phase 2.1 多窗口 +1（sliding_counter '6h'）
> +5（hotness 多窗口）+1（alert 兼容性回归）= **135 passed**。
> 改了代码后 pass 数应只增不减。

只跑某个模块的测试：

```bash
# 只测 Hotness 逻辑
.venv/bin/python -m pytest tests/test_l2_hotness.py -v

# 只测某个具体用例
.venv/bin/python -m pytest tests/test_l2_hotness.py::test_growth_rate_formula -v

# 只测 Phase 2 Telegram 客户端
.venv/bin/python -m pytest tests/test_telegram_client.py -v

# 只测 Phase 2 告警触发服务
.venv/bin/python -m pytest tests/test_l2_alert_trigger.py -v
```

### 7.1 验证 Telegram 告警生效（不动主流程）

改完告警相关参数想确认它真的生效，最快办法是看日志里 AlertTriggerService
启动那一行的 threshold/cooldown 是不是新值：

```bash
grep "AlertTriggerService 启动" logs/service.log | tail -3
```

应该看到类似：

```
AlertTriggerService 启动：growth_threshold=20.0 cooldown=60min escalation×1.5 heartbeat=6h
```

如果 threshold 跟你刚改的不一致 → 进程没重启（`@lru_cache(maxsize=1)` 只在
进程启动时读一次配置），跑 `./scripts/restart.sh --bg` 让 lru_cache 失效。

---

## 8. 常见坑（踩过的）

### 坑 1：`.venv/bin/pip` 装包失败

这个 venv 的 `pip` shebang 指向了旧路径。用这个代替：

```bash
.venv/bin/python -m pip install <package>
```

永远别直接 `pip install ...`（会装到系统 python）。

### 坑 2：Ollama 服务挂了，老链路狂刷 ERROR 日志

新链路不调 LLM，这种情况只影响老链路。日志里大量：

```
[twitter] 一次摘要失败：Connection refused
```

→ 去 `192.168.1.219` 那台机器重启 Ollama 服务。新链路不受影响继续跑。

### 坑 3：DB 连接池耗尽（`connection pool exhausted`）

默认 `pool_size=5 + overflow=5 = 10`。如果同时启动了多个 Python 脚本连同一
个 DB（比如你一边手动跑调试脚本一边后台进程在跑），可能短时间超额。解法：
关掉一些并发访问，或者临时在 `db/connection.py` 把 `pool_size` 调大。

### 坑 4：冷启动看不到 hotness_snapshots

上面场景 A 已讲：需要 entity_mentions 累积到 100 条才出榜。调试时可以：

```sql
-- 看当前 entity_mentions 已经累积多少条
SELECT count(*) FROM entity_mentions
WHERE ts >= now() - INTERVAL '7 days';
```

不够 100 条时要么等要么改 `hotness_min_baseline_count`。

---

## 9. 文档索引

| 文档 | 看什么时候 |
|---|---|
| `README.md` | 老链路（Phase 0）的介绍，架构背景 |
| `docs/operations_guide.md`（本文）| 你现在看的：日常运维 + 调试 |
| `docs/gate1_checklist.md` | Phase 1 验收期怎么逐条确认指标 |
| `docs/rollback_plan.md` | 新链路出大问题时如何快速回滚 |
| `.kiro/specs/crypto-narrative-radar/requirements.md` | 需求文档（功能规格） |
| `.kiro/specs/crypto-narrative-radar/design.md` | 架构设计文档 |
| `.kiro/specs/crypto-narrative-radar/tasks.md` | 实施任务清单 |
| `.kiro/specs/crypto-narrative-radar/handoff.md` | AI 会话切换用的交接简报 |

---

## 10. 还不会的时候怎么办

1. 先看日志（`tail -f logs/service.log` + `grep ERROR logs/service.log`）
2. 本文档场景 A/B/C 对照
3. 本项目的 README + specs 目录里四份文档
4. 实在搞不定：把错误日志最后 50 行 + 你做了什么操作 一起贴出来问开发者

**一句话原则**：这个系统的设计就是"出了问题回滚很便宜"（见 rollback_plan），
遇到搞不定的状况先回滚保老链路，有时间再慢慢定位新链路。
