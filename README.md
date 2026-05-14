# PomsAI

加密叙事雷达 —— 一个常驻 Python 服务，从 Twitter / 币安广场 / Discord 三源原始
数据里提取实体、计算热度、识别共现叙事，并用 LLM 给热点实体生成"为什么热"的
JSON 简报。最终通过 Telegram 推送告警。

本仓库**只负责数据加工与告警**：
- 不抓取外部数据 —— 抓取由独立服务负责，结果落入 `twitter_posts` /
  `binance_square_posts` / `discord_messages` 三张原始表
- 不建表 —— 业务表由 alembic 迁移管理（`alembic/versions/`）
- 不提供 HTTP 接口 —— 下游通过 SQL 直接查 `hotness_snapshots` /
  `entity_cooccurrence` / `entity_briefings`，或订阅 Telegram

## 工作流

```
twitter_posts / binance_square_posts / discord_messages
  → NormalizerService     → normalized_messages（L0：归一化 + SimHash 判重）
  → EntityExtractor       → entity_mentions（L1：抽实体 + 滑窗计数）
  → HotnessService × 3    → hotness_snapshots（L2：1h/6h/24h 三窗口榜）
  → CooccurrenceService   → entity_cooccurrence（L3：实体两两共现 + PMI）
  → AlertTriggerService   → Telegram（整点告警；带 LLM 简报）
  → BriefingService       → entity_briefings（L5：LLM 给热点实体出 JSON 简报）

EntityExtractor 内部 hook：
  → RealtimeAlertService  → Telegram（实时通道；端到端 1~2 分钟延迟）
```

历史变更（2026-05）：老链路 `Level1Service` / `Level2Service`（调 Ollama 做
中文文本摘要）已淘汰，被 `BriefingService`（按热点实体出结构化 JSON 简报）
完全替代。详见 `docs/faq_design_decisions.md` Q1。

## 设计哲学

1. **信号产生链路零 LLM**：hotness 公式 / SimHash 去重 / 共现 PMI 全是
   确定性算法，可重放、可单测、可回归。LLM 只在"信号已经产生"之后加解释，
   不反向影响信号链路（详见 `docs/faq_design_decisions.md` Q11）
2. **配置缺失即降级**：每个可选服务（Telegram / 实时 / 共现 / LLM 简报）
   都通过开关独立启停，关掉某个不影响其它
3. **不引入新基础设施**：不上 Redis / FAISS / Milvus / Kafka；状态只放
   PostgreSQL 或进程内内存
4. **单 worker 线程串行调度**：所有 service 共用一个线程逐个调用 `run_once()`，
   异常隔离 + 失败快速回滚

## 技术栈

| 层 | 技术 |
|----|------|
| ORM | SQLAlchemy 2.x（`Mapped[]` 风格）|
| DB 驱动 | psycopg2-binary |
| 调度 | 自实现单线程 worker（`scheduler/jobs.py`）|
| LLM | Ollama HTTP API（`/api/chat`），仅 `BriefingService` 使用 |
| 通知 | Telegram Bot API（标准库 urllib，零新依赖）|
| 日志 | loguru（按天滚动）|
| 配置 | dataclass（直接写在 `config/settings.py` 各分组文件，**不读 `.env`**）|
| 迁移 | alembic |

## 目录结构

```
.
├── main.py                          # 入口：装配各 service + 启动 worker
├── alembic/                         # DB 迁移（手写，不用 autogenerate）
│   └── versions/
│       ├── 001_phase1_initial.py    # normalized_messages / entity_mentions /
│       │                            # hotness_snapshots
│       ├── 002_phase2_cooccurrence.py
│       └── 004_phase2_briefings.py
├── config/
│   ├── settings.py                  # 总入口，多继承 5 个分组
│   ├── _database.py                 # PG 连接
│   ├── _runtime.py                  # 日志 / 时区 / worker 调度
│   ├── _llm.py                      # Ollama 配置（仅 BriefingService 用）
│   ├── _new.py                      # 业务流水线参数
│   └── _alerts.py                   # Telegram 告警 + 实时通道
├── db/
│   ├── connection.py                # SQLAlchemy Engine + Session
│   ├── models.py                    # ORM
│   └── repositories/                # 各表的 CRUD 封装
│       ├── normalized_messages_repo.py
│       ├── entity_mentions_repo.py
│       ├── hotness_snapshots_repo.py
│       ├── cooccurrence_repo.py
│       └── briefings_repo.py
├── dictionaries/                    # YAML 词典（tickers/chains/narratives/kols）
│   └── loader.py
├── llm/
│   └── ollama_client.py             # Ollama HTTP 调用（仅 BriefingService 调）
├── notifications/
│   └── telegram_client.py           # Telegram Bot 客户端
├── prompts/
│   └── level5_briefing.txt          # BriefingService 的 LLM Prompt
├── scheduler/
│   └── jobs.py                      # 单线程串行 worker
├── services/
│   ├── prefilter.py                 # 规则预过滤 + 实体抽取
│   ├── l0_dedup.py                  # SimHash 判重
│   ├── l0_normalizer.py             # 三源归一化
│   ├── l1_entity_extractor.py       # 实体抽取 + 滑窗计数
│   ├── l2_sliding_counter.py        # 进程内多窗口计数器
│   ├── l2_hotness.py                # 1h/6h/24h 多窗口热度榜
│   ├── l2_alert_trigger.py          # 整点 Telegram 告警
│   ├── l2_realtime_trigger.py       # 实时 Telegram 告警
│   ├── l3_cooccurrence.py           # 实体共现网络 + PMI
│   └── l5_briefing.py               # LLM 定向简报
├── scripts/
│   ├── check_status.py              # 一键自检：跑一遍 SQL 看系统在不在干活
│   ├── tune_helper.py               # 调参诊断：看 growth 分布 + 推荐阈值
│   └── pipeline_inspect.py          # 漏斗诊断：每层转化率 + 异常定位
├── docs/
│   ├── operations_guide.md          # 日常运维 + 调试
│   ├── faq_design_decisions.md      # 设计决策 Q&A
│   └── 终极设计文档.md              # 项目早期 v3.0 整合设计文档（历史归档）
└── tests/                           # pytest 套件
    ├── test_l0_dedup.py
    ├── test_l0_normalizer.py
    ├── test_l1_entity_extractor.py
    ├── test_l2_alert_trigger.py
    ├── test_l2_hotness.py
    ├── test_l2_realtime_trigger.py
    ├── test_l2_sliding_counter.py
    ├── test_l3_cooccurrence.py
    ├── test_l5_briefing.py
    ├── test_models.py
    ├── test_prefilter.py
    ├── test_scheduler_jobs.py
    └── test_telegram_client.py
```

## 环境要求

- Python 3.11+
- PostgreSQL 14+，业务表已由 alembic 迁移创建（详见 `alembic/versions/`）
- Ollama（仅 `BriefingService` 用，未配置时该服务自动跳过）

## 安装与运行

```bash
python3 -m venv .venv
source .venv/bin/activate
.venv/bin/python -m pip install -r requirements.txt

# 跑迁移建表（首次部署）
.venv/bin/alembic upgrade head

# 启动服务
.venv/bin/python main.py
```

启动后看 `logs/service.log`（控制台同步输出）；正常的启动日志参考
`docs/operations_guide.md` §2。

建议用 `systemd` / `supervisor` 做进程托管，意外退出自动拉起。

## 配置

5 个分组（按职责拆开）：

| 文件 | 配置组 | 调什么 |
|---|---|---|
| `config/_database.py` | DatabaseSettings | PG 连接 |
| `config/_runtime.py` | RuntimeSettings | 日志 / 时区 / worker 轮询间隔 |
| `config/_llm.py` | LLMSettings | Ollama 服务（BriefingService 用） |
| `config/_new.py` | NewPipelineSettings | 业务流水线参数（normalizer / dedup / extractor / hotness × 3 / cooccur / briefing）|
| `config/_alerts.py` | AlertSettings | Telegram 告警 + 实时通道 |

调参不用动代码，改对应分组里的字段默认值后**重启服务**生效。
最常用的调参点见 `docs/operations_guide.md` §6。

## 测试

```bash
.venv/bin/python -m pytest tests/ --ignore=tests/test_ollama_client.py -q
```

预期：**157 passed**（覆盖各 service 单元 + 集成测试）。

`test_ollama_client.py` 需要本地 Ollama 可达才能跑，CI 默认跳过；其它测试
全部用 SQLite in-memory + Mock，不依赖真实 PG / 网络。

## 文档导航

| 文档 | 看什么时候 |
|---|---|
| `docs/operations_guide.md` | 日常运维、调参、出问题排查（最常翻） |
| `docs/configuration.md` | **配置参数速查表**（73 个参数按 service 分组）|
| `docs/tuning_guide.md` | **调参方法论**（不知道阈值该设多少时看，配合 `tune_helper.py` 和 `pipeline_inspect.py`）|
| `docs/faq_design_decisions.md` | 设计决策"为什么"（Q1~Q12，含老链路淘汰说明）|
| `文档/终极设计文档.md` | 项目早期 v3.0 整合设计文档（历史归档）|
| `docs/faq_design_decisions.md` | 设计决策"为什么"（Q1~Q11，含老链路淘汰说明） |
| `文档/终极设计文档.md` | 项目早期 v3.0 整合设计文档（历史归档） |
| `.kiro/specs/*/` | 各 Phase 子任务的 requirements / design / tasks 实施记录 |
