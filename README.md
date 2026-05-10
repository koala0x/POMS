# PomsAI

一个常驻后台运行的 Python 服务,对外部抓取服务已经落库的原始社交数据
(Twitter / 币安广场 / Discord)按数据源独立做"两级摘要",最终把高密度的
简报落库,供下游系统消费。

本仓库**只负责 AI 数据清洗与提炼**:
- 不提供 HTTP 接入接口,不抓取外部数据 —— 这些职责已拆到其他服务
- 不负责建表或迁移 —— 业务表由上游服务创建并维护

## 工作流

```
twitter_posts / binance_square_posts / discord_messages
  └─ 一次摘要(level1) → summary_level1
       └─ 二次摘要(level2) → summary_level2
```

- **一次摘要 (level1)**:每个 source 独立处理,未处理原始数据累计 ≥ `batch_size`
  时按 `created_at` 升序取最早一批,经 prefilter 丢弃噪音后拼 prompt → 调 Ollama →
  写 `summary_level1`,并把整批(含被过滤的)`is_summarized` 标 TRUE。
- **二次摘要 (level2)**:每个 source 独立处理,未做二次摘要的 level1 累计
  ≥ `level2_threshold` 时,按 `created_at` 升序取一批,拼 prompt → 调 Ollama →
  写 `summary_level2`,并把消费过的 level1 `is_summarized_l2` 标 TRUE。
- **串行 worker**:level1 + level2 共用一个线程串行触发,避免本机 Ollama 模型 swap。
- **事务一致性**:Service 层显式 commit / rollback,LLM 失败时不更新标记,下次仍可重试。
- **连接自愈**:`pool_pre_ping=True` + `pool_recycle=1800`,无需手写重连。

## 技术栈

| 层 | 技术 |
|----|------|
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
├── requirements.txt
├── config/
│   └── settings.py               # 配置中心,直接改字段默认值即可
├── db/
│   ├── connection.py             # SQLAlchemy Engine + Session(只读写,不建表)
│   ├── models.py                 # ORM 模型(查询/写入用)
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
│   ├── prefilter.py              # 规则预过滤
│   ├── level1_service.py         # 一次摘要业务逻辑
│   └── level2_service.py         # 二次摘要业务逻辑
├── scripts/
│   └── smoke_prompt.py           # 脱库调 prompt + LLM 冒烟
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
- PostgreSQL 12+,业务表已由上游创建(本服务不会 `create_all()`)
- Ollama 已启动(默认 `http://localhost:11434`),已 pull 配置中指定的模型

## 数据库依赖表

本服务**只读写、不建表**,启动前业务表需已存在。模型定义在 `db/models.py`,可用作
建表参考(表结构与字段、索引、UNIQUE 约束请以上游 API/迁移服务的 DDL 为准)。

### 读取

- `twitter_posts` / `binance_square_posts` / `discord_messages`:按 `is_summarized = FALSE`
  顺序消费,处理完成后把这一批的 `is_summarized` 翻 TRUE。

### 写入

- `summary_level1(source, summary, raw_ids, raw_count, created_at, is_summarized_l2)`
- `summary_level2(source, summary, level1_ids, level1_count, period_start, period_end, created_at)`

## 配置

所有运行时配置写在 `config/settings.py` 的 `Settings` dataclass 里,**不读 `.env`**。
改配置 = 改这个文件的字段默认值,然后重启服务。

| 字段 | 默认 | 说明 |
|------|------|------|
| `db_host` / `db_port` / `db_name` / `db_user` / `db_password` | 见文件 | PostgreSQL 连接信息 |
| `ollama_base_url` | `http://192.168.1.219:11434` | Ollama 服务地址 |
| `ollama_model_level1` | `qwen3:8b` | level1 模型(高频,推荐轻量) |
| `ollama_timeout_level1` | `600` | level1 单次超时(秒) |
| `ollama_model_level2` | `qwen3:8b` | level2 模型(可换大模型) |
| `ollama_timeout_level2` | `600` | level2 单次超时(秒) |
| `poll_interval_seconds` | `30` | worker 空闲轮询间隔 |
| `batch_size` | `20` | 一次摘要批大小 |
| `level2_threshold` | `5` | 二次摘要触发阈值 |
| `log_path` | `./logs/service.log` | 日志路径 |
| `log_retention_days` | `30` | 日志保留天数 |
| `timezone` | `UTC` | 业务时区 |

## 安装与运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 main.py
```

启动后会:
1. 加载配置 + 初始化日志
2. 创建 SQLAlchemy Engine(懒连接)
3. 启动 summary worker,按 `poll_interval_seconds` 空转轮询

日志输出到控制台 + `./logs/service.log`(按天滚动,保留 30 天)。

建议用 `systemd` / `supervisor` 做进程托管,意外退出自动拉起。

## 架构亮点

### SQLAlchemy 2.x ORM

完全用 `select()` / `update()` / `session.add()` 等 2.0 风格 API,没有任何手写 SQL。

### Session-per-operation + 显式提交

`Database.get_session()` 是 contextmanager,**异常自动 rollback,但成功不自动 commit**。
Service 层在写入完成后调用 `session.commit()` —— 这样"LLM 失败 → 不写库 → 不更新标记"
的语义自然落地。

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

## Prompt

Prompt 模板放在 `prompts/`,使用 `{items}` 作为占位符。调整模板**不需要重新发布代码**,
重启 worker 即生效。

## 测试

```bash
python3 -m pytest -q
```

| 文件 | 类型 | 是否需要 DB |
|------|------|-------------|
| `test_models.py` | ORM 模型结构性断言 | 否 |
| `test_prefilter.py` | 规则过滤 keep/drop 决策 | 否 |
| `test_level1_service.py` / `test_level2_service.py` | Service 流程编排 Mock 测试 | 否 |
| `test_repositories.py` | Repository 集成测试(真实 PG + 已有业务表) | 是,不可用时自动 skip |
| `test_ollama_client.py` | LLM 客户端测试 | 需要本地 Ollama |
