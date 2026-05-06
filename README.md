# Social Summary Service

一个常驻后台运行的 Python 服务，轮询 PostgreSQL 的两张原始数据表（Twitter / 币安广场），触发本地 Ollama（qwen3:30b）生成摘要并落库：
- 每 30 秒轮询一次，任意数据源累计未处理数据达到 50 条时触发一次摘要（level1）
- 每小时整点，把过去 1 小时内未做二次摘要的 level1 记录汇总生成二次摘要（level2）
- Twitter 与币安广场全程独立处理，不合并

## 功能概览

- 一次摘要（level1）：按 created_at（缺失则按 id）取最早的 50 条未处理内容，拼接 prompt 调用 LLM，写入 summary_level1，并标记原始数据 is_summarized=TRUE
- 二次摘要（level2）：每小时整点按时间窗口 [上一小时开始, 本小时整点) 拉取未处理的 level1，拼接 prompt 调用 LLM，写入 summary_level2，并标记 level1 的 is_summarized_l2=TRUE
- 异常处理：LLM 超时/失败重试；LLM 空内容视为失败；DB 连接中断自动重连；失败时不更新标记，保证下次可重试
- 字段自适配：对 twitter_posts / binance_square_posts 做字段探测映射，兼容不同字段命名（见下文）

## 目录结构

```
.
├── main.py
├── requirements.txt
├── .env.example
├── config/
│   └── settings.py
├── db/
│   ├── connection.py
│   ├── migrations/
│   └── repositories/
├── llm/
│   └── ollama_client.py
├── prompts/
├── scheduler/
│   └── jobs.py
├── services/
│   ├── level1_service.py
│   └── level2_service.py
└── tests/
```

## 环境要求

- Python 3.10+
- PostgreSQL
- Ollama（本地已启动，默认 `http://localhost:11434`）

## 安装与运行

### 1) 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2) 配置环境变量

复制示例文件并按需修改：

```bash
cp .env.example .env
```

关键配置项：
- DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD：PostgreSQL 连接信息
- OLLAMA_BASE_URL/OLLAMA_MODEL：Ollama 地址与模型名
- OLLAMA_TIMEOUT_SECONDS：单次请求超时（默认 120）
- OLLAMA_RETRY_TIMES / OLLAMA_RETRY_DELAY_SECONDS：失败重试策略
- POLL_INTERVAL_SECONDS：轮询间隔（默认 30）
- BATCH_SIZE：一次摘要批大小（默认 50）
- TIMEZONE：业务时区（用于整点窗口计算，默认 UTC）

### 3) 数据库表结构

启动服务时会通过 SQLAlchemy `Base.metadata.create_all()` 自动创建四张表(幂等):

- `twitter_posts` / `binance_square_posts`(原始数据)
- `summary_level1`(一次摘要,`raw_ids` 为 `BIGINT[]`)
- `summary_level2`(二次摘要,`level1_ids` 为 `BIGINT[]`)

时间字段统一使用 `TIMESTAMP WITH TIME ZONE`。表结构定义见 `db/models.py`。
如需修改字段或索引,直接改模型类,重启服务后会自动新增缺失的表/索引(`create_all` 不会变更已存在的表)。

### 4) 启动服务

```bash
python3 main.py
```

日志默认输出到：
- 控制台
- `./logs/service.log`（按天滚动，保留 30 天）

## 原始表字段自适配

原始表是“已有表”，字段命名可能不同。服务会在首次访问时从 `information_schema.columns` 探测字段并映射：
- id：必须存在（用于批处理和稳定排序）
- content：优先匹配 `content/text/body/message/post/tweet`
- author：优先匹配 `author/username/user/screen_name`（缺失则为 NULL）
- posted_at：优先匹配 `posted_at/posted_time/post_time/published_at`（缺失则为 NULL）
- created_at：优先匹配 `created_at/created_time/inserted_at/inserted_time`（缺失则按 id 排序）
- is_summarized：优先匹配 `is_summarized/summarized/is_summary/is_summarised/summary_done`

服务启动后日志会打印类似：
`[twitter_posts] 字段映射：id=... content=... author=... posted_at=... created_at=... flag=...`

## Prompt

Prompt 模板在 `prompts/`，使用 `{items}` 作为占位符：
- `prompts/level1_twitter.txt`
- `prompts/level1_binance.txt`
- `prompts/level2_twitter.txt`
- `prompts/level2_binance.txt`

## 测试（可选）

本仓库带有最小化单测，但需要安装 pytest：

```bash
python3 -m pytest -q
```
