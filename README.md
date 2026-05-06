# Social Summary Service

一个常驻后台运行的 Python 服务,轮询 PostgreSQL 中的两张原始数据表(Twitter / 币安广场),触发本地 Ollama (`qwen3:30b`) 生成摘要并落库:

- 每 30 秒轮询一次,任意数据源累计未处理数据达到 50 条时触发一次摘要(level1)
- 每小时整点,把过去 1 小时内未做二次摘要的 level1 记录汇总生成二次摘要(level2)
- Twitter 与币安广场全程独立处理,不合并

## 技术栈

| 层 | 技术 |
|----|------|
| ORM | **SQLAlchemy 2.x**(`Mapped[]` 风格) |
| DB 驱动 | psycopg2-binary |
| 调度 | APScheduler(BackgroundScheduler) |
| LLM | Ollama HTTP API |
| 日志 | loguru(按天滚动) |
| 配置 | python-dotenv + dataclass |

## 功能概览

- **一次摘要(level1)**:按 `created_at` 升序、`id` 次序取最早的 50 条未处理内容,拼接 prompt 调用 LLM,写入 `summary_level1`,并把原始数据 `is_summarized` 标为 `TRUE`
- **二次摘要(level2)**:每小时整点按时间窗口 `[上一小时开始, 本小时整点)` 拉取未处理的 level1,拼接 prompt 调用 LLM,写入 `summary_level2`,并把 level1 的 `is_summarized_l2` 标为 `TRUE`
- **异常处理**:LLM 超时/失败重试;LLM 空内容视为失败;DB 连接通过 `pool_pre_ping` 自动剔除失效连接;失败时不更新标记,保证下次可重试
- **事务一致性**:Service 层显式 `session.commit()` / `session.rollback()`,失败时不会留下半截数据

## 目录结构

```
.
├── main.py                       # 程序入口,启动 scheduler
├── requirements.txt              # 依赖清单
├── .env.example
├── config/
│   └── settings.py               # 读取 .env,统一暴露配置项
├── db/
│   ├── connection.py             # SQLAlchemy Engine + Session 管理
│   ├── models.py                 # ORM 模型(Base/TwitterPost/BinanceSquarePost/SummaryLevel1/SummaryLevel2)
│   └── repositories/
│       ├── raw_posts_base.py     # 原始表仓储基类(count/fetch/mark)
│       ├── twitter_repo.py
│       ├── binance_repo.py
│       ├── level1_repo.py        # summary_level1 读写
│       └── level2_repo.py        # summary_level2 写入
├── llm/
│   └── ollama_client.py          # Ollama HTTP 调用 + 重试
├── prompts/                      # Prompt 模板,使用 {items} 占位符
├── scheduler/
│   └── jobs.py                   # APScheduler 任务注册
├── services/
│   ├── level1_service.py         # 一次摘要业务逻辑
│   └── level2_service.py         # 二次摘要业务逻辑
└── tests/
    ├── test_level1_service.py    # Service 层 Mock 单测
    ├── test_level2_service.py
    ├── test_models.py            # ORM 模型结构性断言(无需 DB)
    ├── test_repositories.py      # Repository 集成测试(需 PG)
    └── test_ollama_client.py
```

## 环境要求

- Python 3.10+
- PostgreSQL 12+
- Ollama(本地已启动,默认 `http://localhost:11434`,模型 `qwen3:30b` 已 pull)

## 数据库表结构

ORM 模型定义在 `db/models.py`,启动时通过 `Base.metadata.create_all()` **自动建表**(幂等,不会修改已存在的表):

### `twitter_posts` / `binance_square_posts`

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL PK | 主键 |
| content | TEXT NOT NULL | 帖子正文 |
| author | VARCHAR(255) | 作者 |
| posted_at | TIMESTAMPTZ | 发帖时间 |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT NOW() | 入库时间 |
| is_summarized | BOOLEAN NOT NULL DEFAULT FALSE | 是否已被一次摘要处理 |

索引:`(is_summarized, created_at)`

### `summary_level1`

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL PK | 主键 |
| source | VARCHAR(32) NOT NULL | `twitter` 或 `binance_square` |
| summary | TEXT NOT NULL | 一次摘要内容 |
| raw_ids | BIGINT[] NOT NULL | 本次涉及的原始数据 id 列表(逻辑引用,不建外键) |
| raw_count | INTEGER NOT NULL | 原始条数(固定 50) |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT NOW() | 摘要生成时间 |
| is_summarized_l2 | BOOLEAN NOT NULL DEFAULT FALSE | 是否已被二次摘要处理 |

索引:`(source, created_at)`、`(is_summarized_l2, created_at)`

### `summary_level2`

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL PK | 主键 |
| source | VARCHAR(32) NOT NULL | `twitter` 或 `binance_square` |
| summary | TEXT NOT NULL | 二次摘要内容 |
| level1_ids | BIGINT[] NOT NULL | 本次涉及的 level1 id 列表 |
| level1_count | INTEGER NOT NULL | 一次摘要条数 |
| period_start | TIMESTAMPTZ NOT NULL | 时间窗起点 |
| period_end | TIMESTAMPTZ NOT NULL | 时间窗终点 |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT NOW() | 生成时间 |

索引:`(source, created_at)`、`(source, period_start, period_end)`

> 修改字段或索引:直接改 `db/models.py`,重启服务后 `create_all()` 会增量创建缺失的表/索引。**已存在的表不会被自动 ALTER**——需要修改既有列时,自行 `DROP TABLE` 后重启,或额外执行手工 SQL。

## 安装与运行

### 1) 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` 关键依赖:
```
SQLAlchemy>=2.0,<3
psycopg2-binary
APScheduler
loguru
python-dotenv
requests
```

### 2) 配置环境变量

```bash
cp .env.example .env
```

| 配置项 | 默认 | 说明 |
|--------|------|------|
| DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD | 127.0.0.1/5432/postgres/postgres/postgres | PostgreSQL 连接信息 |
| OLLAMA_BASE_URL | http://localhost:11434 | Ollama 服务地址 |
| OLLAMA_MODEL | qwen3:30b | 模型名 |
| OLLAMA_TIMEOUT_SECONDS | 120 | 单次请求超时 |
| OLLAMA_RETRY_TIMES | 3 | 失败重试次数 |
| OLLAMA_RETRY_DELAY_SECONDS | 10 | 重试间隔 |
| POLL_INTERVAL_SECONDS | 30 | 轮询间隔 |
| BATCH_SIZE | 50 | 一次摘要批大小 |
| LOG_PATH | ./logs/service.log | 日志路径 |
| LOG_RETENTION_DAYS | 30 | 日志保留天数 |
| TIMEZONE | UTC | 业务时区(整点窗口计算用) |

### 3) 启动服务

```bash
python3 main.py
```

启动时会:
1. 加载配置 + 初始化日志
2. 创建 SQLAlchemy Engine(`pool_pre_ping=True` 自动健康检查)
3. 调用 `db.create_all()` 兜底建表
4. 注册 APScheduler 任务并常驻

日志输出:
- 控制台
- `./logs/service.log`(按天滚动,保留 30 天)

## 架构亮点

### SQLAlchemy 2.x ORM

完全用 `select()` / `update()` / `session.add()` 等 2.0 风格 API,没有任何手写 SQL。模型采用 `Mapped[]` 类型注解,IDE 类型推导友好:

```python
class SummaryLevel1(Base):
    __tablename__ = "summary_level1"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), nullable=False)
    ...
```

### Session-per-operation + 显式提交

`Database.get_session()` 是 contextmanager,**异常自动 rollback,但成功不自动 commit**。Service 层在写入完成后调用 `session.commit()`——这样可以让"LLM 失败 → 不写库 → 不更新标记"的语义自然落地:

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

### 连接池自愈

`create_engine(..., pool_pre_ping=True, pool_recycle=1800)`:
- `pool_pre_ping`:借出连接前先 ping,失效连接自动剔除并重新建立
- `pool_recycle=1800`:30 分钟主动回收连接,避免被 PG/中间件单边断开后留死连接

替代了原先手写的 `OperationalError + sleep(5) + reconnect` 逻辑。

### Repository 协议化

Service 通过 `typing.Protocol` 依赖 Repository,而不是具体类。这让单测里可以直接传 `Mock`:

```python
class RawRepo(Protocol):
    def count_unsummarized(self, session: Session) -> int: ...
    def fetch_oldest_unsummarized(self, session: Session, limit: int) -> list: ...
    def mark_summarized(self, session: Session, ids: Sequence[int]) -> int: ...
```

## Prompt

Prompt 模板放在 `prompts/`,使用 `{items}` 作为占位符:
- `level1_twitter.txt` / `level1_binance.txt`
- `level2_twitter.txt` / `level2_binance.txt`

调整模板**不需要重新发布代码**,只要重启服务即可生效。

## 测试

### 运行全部测试

```bash
python3 -m pytest -q
```

### 测试分层

| 文件 | 类型 | 是否需要 DB |
|------|------|-------------|
| `test_models.py` | ORM 模型结构性断言(列、索引、DDL 渲染) | 否 |
| `test_level1_service.py` / `test_level2_service.py` | Service 流程编排 Mock 测试 | 否 |
| `test_repositories.py` | Repository 集成测试(真实 PG) | 是,PG 不可用时自动 skip |
| `test_ollama_client.py` | LLM 客户端测试 | 需要本地 Ollama |

集成测试每个用例独立 Session,在 `finally` 中按 id 清理本测试插入的行,**不会污染业务数据**。

### 灌数据 + 端到端冒烟

如需手工触发一次完整流程,可以直接用 ORM 灌入若干条数据,等服务自动跑摘要:

```python
from datetime import datetime, timezone
from config.settings import get_settings
from db.connection import Database
from db.models import TwitterPost

db = Database(get_settings())
db.create_all()
with db.get_session() as session:
    for i in range(60):
        session.add(TwitterPost(content=f"hello {i}", author="smoke"))
    session.commit()
```

启动 `python3 main.py`,30 秒内日志应出现:
```
[twitter] 一次摘要完成:level1_id=… raw_count=50
```

下一个整点会触发 level2:
```
[twitter] 二次摘要完成:level2_id=… level1_count=… period=…
```

## 部署建议

- **进程托管**:`systemd` / `supervisor` / `pm2` 任选其一,确保意外退出后自动拉起
- **幂等保证**:`is_summarized` / `is_summarized_l2` 标记位 + 失败不更新策略,服务可随意重启
- **日志归档**:loguru 已开启按天滚动,30 天后自动清理;如需更长保留,改 `LOG_RETENTION_DAYS`
- **DB 主备切换**:`pool_pre_ping=True` 已经能覆盖大多数情形,如有更复杂的需求可在 Database 上加 `reconnect()` 调用
