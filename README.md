# PomsAI

一个常驻后台运行的 Python 服务,把外部抓取到的原始社交数据(Twitter / 币安广场 / Discord)
按数据源独立做"两级摘要",最终把高密度的简报落库,供下游系统消费。

整体由两个独立进程组成:

- **HTTP 接入服务** (`api_main.py`):暴露 `POST /ingest` 接口,外部抓取脚本把原始数据
  以数组形式 POST 进来,直接落入对应的原始表。
- **摘要 worker** (`main.py`):轮询原始表,触发本地 Ollama 做一次摘要(level1)与
  二次摘要(level2),写入 `summary_level1` / `summary_level2`。

两个进程共享同一份 PostgreSQL,互不依赖、可独立重启。

## 功能概览

- **HTTP 接入** (`POST /ingest`):单接口接收三类数据,根据 `source` 字段派发到对应表,
  单事务批量插入,失败整批回滚。
- **预过滤** (`services/prefilter.py`):规则引擎在送入 LLM 前丢掉明显噪音(纯口水、
  跑题、过短),保留含 `$X` / 币名 / 财经百分比的强信号帖子,降低 LLM 成本。
- **一次摘要 (level1)**:每个 source 独立处理,累计未处理原始数据 ≥ `batch_size`
  时按 `created_at` 升序取最早一批,拼 prompt → 调 Ollama → 写 `summary_level1`,
  并把这批原始数据 `is_summarized` 标 TRUE。
- **二次摘要 (level2)**:每个 source 独立处理,未做二次摘要的 level1 累计
  ≥ `level2_threshold` 时,按 `created_at` 升序取一批,拼 prompt → 调 Ollama →
  写 `summary_level2`,并把消费过的 level1 `is_summarized_l2` 标 TRUE。
- **串行 worker**:level1 + level2 共用一个线程串行触发,避免本机 Ollama 模型 swap。
- **事务一致性**:Service 层显式 commit / rollback,LLM 失败时不更新标记,下次仍可重试。
- **连接自愈**:`pool_pre_ping=True` + `pool_recycle=1800`,无需手写重连。

## 技术栈

| 层 | 技术 |
|----|------|
| HTTP 框架 | **Flask 3.x** |
| ORM | **SQLAlchemy 2.x** (`Mapped[]` 风格) |
| DB 驱动 | psycopg2-binary |
| 调度 | 自实现单线程 worker (`scheduler/jobs.py`) |
| LLM | Ollama HTTP API (`/api/chat`) |
| 日志 | loguru (按天滚动) |
| 配置 | dataclass(直接写在 `config/settings.py`,**不读 `.env`**) |

## 目录结构

```
.
├── main.py                       # 摘要 worker 入口
├── api_main.py                   # HTTP 接入服务入口
├── requirements.txt
├── api/
│   ├── __init__.py
│   └── server.py                 # Flask app 工厂 + /ingest /health 路由
├── config/
│   └── settings.py               # 配置中心,直接改字段默认值即可
├── db/
│   ├── connection.py             # SQLAlchemy Engine + Session
│   ├── models.py                 # ORM 模型(TwitterPost / BinanceSquarePost / DiscordMessage / SummaryLevel1 / SummaryLevel2)
│   └── repositories/
│       ├── raw_posts_base.py     # 原始表仓储基类(count / fetch / mark)
│       ├── twitter_repo.py
│       ├── binance_repo.py
│       ├── discord_repo.py
│       ├── level1_repo.py        # summary_level1 读写
│       └── level2_repo.py        # summary_level2 写入
├── llm/
│   └── ollama_client.py          # Ollama HTTP 调用 + 重试
├── prompts/                      # Prompt 模板,使用 {items} 占位符
│   ├── level1_twitter.txt
│   ├── level1_binance.txt
│   ├── level1_discord.txt
│   ├── level2_twitter.txt
│   ├── level2_binance.txt
│   └── level2_discord.txt
├── scheduler/
│   └── jobs.py                   # 单线程串行 worker(level1 + level2)
├── services/
│   ├── ingest_service.py         # /ingest 入库逻辑
│   ├── prefilter.py              # 规则预过滤
│   ├── level1_service.py         # 一次摘要业务逻辑
│   └── level2_service.py         # 二次摘要业务逻辑
├── scripts/
│   └── smoke_prompt.py
└── tests/
    ├── test_models.py
    ├── test_prefilter.py
    ├── test_level1_service.py
    ├── test_level2_service.py
    ├── test_repositories.py
    └── test_ollama_client.py
```

## 环境要求

- Python 3.11+(`datetime.fromisoformat` 解析 `Z` 后缀依赖 3.11)
- PostgreSQL 12+
- Ollama 已启动(默认 `http://localhost:11434`),已 pull 配置中指定的模型

## HTTP 接入服务

启动后单进程常驻,所有外部抓取脚本(Twitter 抓取、币安广场抓取、Discord 抓取)
都通过同一个 `/ingest` 接口提交数据,内部根据 `source` 派发到不同表。

### 启动

```bash
# 开发/调试(Flask 内置 server)
python api_main.py

# 生产(多 worker)
gunicorn -w 4 -b 0.0.0.0:18089 'api_main:app'
```

监听地址/端口在 `config/settings.py` 的 `api_host` / `api_port`,默认 `0.0.0.0:18089`。

### `POST /ingest`

请求头:
```
Content-Type: application/json
```

请求体:
```jsonc
{
  "source": "twitter | binance_square | discord",
  "items": [ ... ]   // 单批数据数组,可为任意长度
}
```

`items` 中每个对象的字段约定按 `source` 不同:

#### twitter / binance_square

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| content | string | ✅ | 帖子正文(非空) |
| author | string | ❌ | 作者,缺失则入库为 NULL |
| posted_at | string (ISO 8601) | ❌ | 发帖时间;支持 `Z` 与 `+08:00` 后缀;缺失则入库为 NULL |
| tweet_id | string / int | ❌ | 仅 `twitter` 使用;传入后走 UNIQUE + ON CONFLICT 去重 |
| post_id | string / int | ❌ | 仅 `binance_square` 使用;传入后走 UNIQUE + ON CONFLICT 去重 |

#### discord

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| content | string | ✅ | 消息正文(非空) |
| channel_name | string | ✅ | 频道名(不带 `#`) |
| username | string | ✅ | 发言用户名 |
| posted_at | string (ISO 8601) | ❌ | 发言时间,可选 |

`posted_at` 缺失时入库为 NULL,摘要侧会用 `created_at` 兜底渲染。

### 响应

| 状态码 | 含义 | 响应体 |
|--------|------|--------|
| 200 | 成功 | `{"ok": true, "source": "...", "inserted": N}` |
| 400 | 请求体或字段不合法 | `{"error": "<原因>"}` |
| 500 | 服务端异常(已记录堆栈) | `{"error": "internal error"}` |

事务保证:**单次请求所有 items 在同一事务内 add_all + commit,任意一条字段不合法整批拒绝,
DB 写入失败整批回滚**,不会出现"半批成功"。

### `GET /health`

存活检查,固定返回 `{"ok": true}`,**不查 DB**,适合放在 LB 健康探测里。

### 调用示例

```bash
# 提交 5 条 Twitter
curl -X POST http://localhost:18089/ingest \
  -H 'Content-Type: application/json' \
  -d '{
    "source": "twitter",
    "items": [
      {"content": "$BTC 突破新高", "author": "alice", "posted_at": "2026-05-07T10:00:00Z"},
      {"content": "ETH 也跟上来了", "author": "bob"},
      {"content": "..."},
      {"content": "..."},
      {"content": "..."}
    ]
  }'
# {"inserted":5,"ok":true,"source":"twitter"}

# 提交 10 条币安广场
curl -X POST http://localhost:18089/ingest \
  -H 'Content-Type: application/json' \
  -d '{"source":"binance_square","items":[ {"content":"..."}, ... ]}'

# 提交 50 条 Discord
curl -X POST http://localhost:18089/ingest \
  -H 'Content-Type: application/json' \
  -d '{
    "source": "discord",
    "items": [
      {"content":"...", "channel_name":"alpha-calls", "username":"satoshi",
       "posted_at":"2026-05-07T10:00:00+08:00"},
      ...
    ]
  }'
```

```python
# Python requests 示例
import requests
requests.post(
    "http://localhost:18089/ingest",
    json={
        "source": "discord",
        "items": [
            {"content": msg.content,
             "channel_name": msg.channel,
             "username": msg.author,
             "posted_at": msg.timestamp.isoformat()}
            for msg in batch
        ],
    },
    timeout=30,
).raise_for_status()
```

> 当前接口**未做鉴权**,默认假设服务只在内网/本机暴露。如需公网部署请在 nginx
> 或 Flask 层加 token 校验。

## 数据库表结构

ORM 模型定义在 `db/models.py`,服务启动时通过 `Base.metadata.create_all()`
**自动建表**(幂等,不会修改已存在的表)。

### `twitter_posts` / `binance_square_posts`

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL PK | 主键 |
| content | TEXT NOT NULL | 帖子正文 |
| author | VARCHAR(255) | 作者 |
| posted_at | TIMESTAMPTZ | 发帖时间 |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT NOW() | 入库时间 |
| is_summarized | BOOLEAN NOT NULL DEFAULT FALSE | 是否已被一次摘要处理 |
| tweet_id | VARCHAR(64) UNIQUE | 仅 `twitter_posts`:推文原生 ID,抓取侧去重用 |
| post_id | VARCHAR(64) UNIQUE | 仅 `binance_square_posts`:币安广场帖子原生 ID,抓取侧去重用 |

索引:`(is_summarized, created_at)`

### `discord_messages`

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL PK | 主键 |
| channel_name | VARCHAR(255) NOT NULL | 频道名 |
| username | VARCHAR(255) NOT NULL | 发言用户名 |
| content | TEXT NOT NULL | 消息正文 |
| posted_at | TIMESTAMPTZ | 发言时间 |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT NOW() | 入库时间 |
| is_summarized | BOOLEAN NOT NULL DEFAULT FALSE | 是否已被一次摘要处理 |

索引:`(is_summarized, created_at)`、`(channel_name, created_at)`

> Discord 没有"作者"列;在 prompt 渲染时由 ORM 派生属性 `author` 拼成
> `#<channel_name> @<username>`,与其它两类共享 Level1Service 模板。

### `summary_level1`

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL PK | 主键 |
| source | VARCHAR(32) NOT NULL | `twitter` / `binance_square` / `discord` |
| summary | TEXT NOT NULL | 一次摘要内容 |
| raw_ids | BIGINT[] NOT NULL | 本批涉及的原始数据 id 列表(逻辑引用,不建外键) |
| raw_count | INTEGER NOT NULL | 原始条数 |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT NOW() | 摘要生成时间 |
| is_summarized_l2 | BOOLEAN NOT NULL DEFAULT FALSE | 是否已被二次摘要处理 |

索引:`(source, created_at)`、`(is_summarized_l2, created_at)`

### `summary_level2`

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL PK | 主键 |
| source | VARCHAR(32) NOT NULL | `twitter` / `binance_square` / `discord` |
| summary | TEXT NOT NULL | 二次摘要内容 |
| level1_ids | BIGINT[] NOT NULL | 本次涉及的 level1 id 列表 |
| level1_count | INTEGER NOT NULL | 一次摘要条数 |
| period_start | TIMESTAMPTZ NOT NULL | 时间窗起点 |
| period_end | TIMESTAMPTZ NOT NULL | 时间窗终点 |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT NOW() | 生成时间 |

索引:`(source, created_at)`、`(source, period_start, period_end)`

> 修改字段或索引:直接改 `db/models.py`,重启服务后 `create_all()` 会增量创建缺失
> 的表/索引。**已存在的表不会被自动 ALTER**——需要修改既有列时,自行 `DROP TABLE`
> 后重启,或额外执行手工 SQL。

## 配置

所有运行时配置写在 `config/settings.py` 的 `Settings` dataclass 里,**不读 `.env`**。
改配置 = 改这个文件的字段默认值,然后重启服务。

| 字段 | 默认 | 说明 |
|------|------|------|
| `db_host` / `db_port` / `db_name` / `db_user` / `db_password` | `127.0.0.1` / `5432` / `all_new` / `all_new` / `123qwe` | PostgreSQL 连接信息 |
| `ollama_base_url` | `http://localhost:11434` | Ollama 服务地址 |
| `ollama_model_level1` | `qwen3:8b` | level1 模型(高频,推荐轻量) |
| `ollama_timeout_level1` | `600` | level1 单次超时(秒) |
| `ollama_model_level2` | `qwen3:8b` | level2 模型(可换大模型) |
| `ollama_timeout_level2` | `600` | level2 单次超时(秒) |
| `ollama_retry_times` | `1` | 失败重试次数(本地服务默认不重试) |
| `ollama_retry_delay_seconds` | `0` | 重试间隔 |
| `poll_interval_seconds` | `30` | worker 空闲轮询间隔 |
| `batch_size` | `20` | 一次摘要批大小 |
| `level2_threshold` | `5` | 二次摘要触发阈值 |
| `log_path` | `./logs/service.log` | 日志路径 |
| `log_retention_days` | `30` | 日志保留天数 |
| `api_host` / `api_port` | `0.0.0.0` / `18089` | HTTP 接入服务监听 |
| `timezone` | `UTC` | 业务时区(整点窗口计算用) |

## 安装与运行

### 1) 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt`:
```
APScheduler==3.11.0
Flask==3.0.3
loguru==0.7.3
psycopg2-binary==2.9.10
requests==2.32.3
SQLAlchemy>=2.0,<3
pytest==8.3.2
```

### 2) 启动两个进程

```bash
# 终端 A:HTTP 接入服务
python3 api_main.py

# 终端 B:摘要 worker
python3 main.py
```

任一进程启动时会:
1. 加载配置 + 初始化日志
2. 创建 SQLAlchemy Engine
3. `db.create_all()` 兜底建表

日志输出到控制台 + `./logs/service.log`(按天滚动,保留 30 天)。

### 3) 部署建议

- **进程托管**:`systemd` / `supervisor` 给两个进程各起一个 unit,意外退出自动拉起
- **HTTP 服务**:生产环境用 `gunicorn -w 4 -b 0.0.0.0:18089 'api_main:app'`
- **幂等保证**:`is_summarized` / `is_summarized_l2` 标记位 + 失败不更新策略,
  worker 可随意重启
- **DB 主备切换**:`pool_pre_ping=True` 已经能覆盖大多数情形

## 架构亮点

### SQLAlchemy 2.x ORM

完全用 `select()` / `update()` / `session.add()` 等 2.0 风格 API,没有任何手写 SQL。
模型采用 `Mapped[]` 类型注解,IDE 类型推导友好:

```python
class SummaryLevel1(Base):
    __tablename__ = "summary_level1"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), nullable=False)
    ...
```

### Session-per-operation + 显式提交

`Database.get_session()` 是 contextmanager,**异常自动 rollback,但成功不自动 commit**。
Service 层在写入完成后调用 `session.commit()`——这样可以让"LLM 失败 → 不写库 →
不更新标记"的语义自然落地:

```python
with self.db.get_session() as session:
    try:
        level1_id = self.level1_repo.insert(session=session, ...)
        updated = self.raw_repo.mark_summarized(session, raw_ids)
        session.commit()
    except Exception:
        session.rollback()
        raise
```

### 单线程串行 worker

`scheduler/jobs.py` 的 `Jobs` 用一个线程依次跑所有 `level1_service` + `level2_service`。
本机 Ollama 同一时刻只能高效驻留一个模型,串行触发就保证不会触发模型 swap。
全部 service 都"数据不足"时 sleep `poll_interval_seconds`,有任意一个真处理过就立刻
进入下一轮,把积压尽快消掉。

### 规则预过滤

`services/prefilter.py` 用纯 `re` + 词典在 LLM 之前丢弃明显噪音:

- 强信号(命中即保留,即使很短):`$X` / 币名词典 / 美股 ticker
- 强丢弃:长度 < 20;长度 < 35 且命中纯情绪短语(梭哈/亏麻/求带等)
- 弱保留:长度 ≥ 50 且含数字;长度 ≥ 25 且含数字 + `%`

每个决策带 reason 字段,失败排查可直接 grep 日志。

### 连接池自愈

`create_engine(..., pool_pre_ping=True, pool_recycle=1800)`:
- `pool_pre_ping`:借出连接前先 ping,失效连接自动剔除并重新建立
- `pool_recycle=1800`:30 分钟主动回收,避免 PG/中间件单边断开后留死连接

## Prompt

Prompt 模板放在 `prompts/`,使用 `{items}` 作为占位符:
- `level1_twitter.txt` / `level1_binance.txt` / `level1_discord.txt`
- `level2_twitter.txt` / `level2_binance.txt` / `level2_discord.txt`

调整模板**不需要重新发布代码**,重启 worker 即生效。

## 测试

### 运行全部测试

```bash
python3 -m pytest -q
```

### 测试分层

| 文件 | 类型 | 是否需要 DB |
|------|------|-------------|
| `test_models.py` | ORM 模型结构性断言(列、索引、DDL 渲染) | 否 |
| `test_prefilter.py` | 规则过滤 keep/drop 决策 | 否 |
| `test_level1_service.py` / `test_level2_service.py` | Service 流程编排 Mock 测试 | 否 |
| `test_repositories.py` | Repository 集成测试(真实 PG) | 是,PG 不可用时自动 skip |
| `test_ollama_client.py` | LLM 客户端测试 | 需要本地 Ollama |

集成测试每个用例独立 Session,在 `finally` 中按 id 清理本测试插入的行,
**不会污染业务数据**。

### 端到端冒烟

启动两个进程后,直接 POST `/ingest` 灌入 ≥ `batch_size` 条数据:

```bash
curl -X POST http://localhost:18089/ingest \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c '
import json
items = [{"content": f"$BTC test {i}", "author": "smoke"} for i in range(60)]
print(json.dumps({"source": "twitter", "items": items}))
')"
```

worker 端 30 秒内日志应出现:
```
[twitter] 一次摘要完成: level1_id=… raw_count=…
```

二次摘要触发后:
```
[twitter] 二次摘要完成: level2_id=… level1_count=… period=…
```
