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
Ollama 客户端就绪
词典就绪：tickers=57 chains=0 narratives=0 kols=0 aliases=64
SlidingCounter backfill 结束：ok=True total=<N> elapsed=<X.X>s
summary worker 启动:level1=3,level2=3,new=3,空闲 sleep 30s
服务启动成功
```

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
| 冷启动期想尽快看到排行榜 | `hotness_min_baseline_count: 100 → 20` | 降低"基线样本数"门槛；生产环境记得改回来 |
| worker 空闲时醒得更频繁（调试用） | `poll_interval_seconds: 30 → 5` | 30s 太慢看不到效果；生产建议 30~60s |
| SimHash 判重更激进 | `dedup_hamming_threshold: 3 → 5` | 数值越大越容易判成"重复"；上限 6 就差不多了 |
| 热榜条数从 20 改成 50 | `hotness_top_k: 20 → 50` | |
| 每轮归一化多扫点 | `normalizer_batch_size: 500 → 2000` | 积压大的时候加速消化；看 DB 能不能扛住 |
| 换老链路 LLM 模型 | `ollama_model_level1: "qwen3:8b" → "qwen3:30b"` | 先确认 Ollama 端 `ollama pull qwen3:30b` 过了 |
| 延长 LLM 超时 | `ollama_timeout_level1: 600 → 1200` | 上了大模型 CPU 慢就得加 |

**注意**：改 DB 配置（`db_host` / `db_port` 等）后重启前先 `psql` 测一下新地址
通不通，免得进程起来又挂。

---

## 7. 跑测试（验证改动没破坏东西）

改了代码 / 改了配置后，强烈建议先跑测试：

```bash
.venv/bin/python -m pytest tests/ --ignore=tests/test_ollama_client.py -q
```

应看到：

```
109 passed, 1 skipped in X.XXs
```

只跑某个模块的测试：

```bash
# 只测 Hotness 逻辑
.venv/bin/python -m pytest tests/test_l2_hotness.py -v

# 只测某个具体用例
.venv/bin/python -m pytest tests/test_l2_hotness.py::test_growth_rate_formula -v
```

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
