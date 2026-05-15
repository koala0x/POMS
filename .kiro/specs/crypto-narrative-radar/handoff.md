# Handoff · crypto-narrative-radar Phase 1

> 交接简报。新对话打开后按本文档顺序读完就能无缝接手 Task 4。
>
> **本文档不是 spec 的一部分**，只是会话之间的工作状态交接；做完 Phase 1 后可以删除。

---

## 0. 一行背景

把当前「每 20 条原始数据 → LLM 摘要」的老链路保留不动，并行铺一条新链路：**三源原始消息 → 归一化/去重 → 实体抽取 → 每 15 分钟 Top-20 热度排行榜**。Phase 1 全程不调 LLM，靠统计和增长率找热点。

---

## 1. 当前进度

### 已完成（50%，基础设施 + prefilter 就绪）

```
Task 0  [✅] 依赖与脚手架               commit 7fe0cc5
Task 1  [✅] 词典外置与加载器            commit 7fe0cc5
Task 2  [✅] prefilter 改造              commit 44c1505
Task 3  [✅] 数据库新表与迁移            commit 7fe0cc5
```

### 待完成（下一步从 Task 4.1 开始）

```
Task 4  [ ]  L0 Normalizer + Deduplicator   ← 下一步
Task 5  [ ]  L2 Sliding Counter
Task 6  [ ]  L1 Entity Extractor
Task 7  [ ]  L2 Hotness Service
Task 8  [ ]  Worker + main.py 注入
Task 9  [ ]  Gate 1 材料
```

### Commit 历史（当前分支 `AI_1.0.1`，未 push）

```
44c1505 feat(phase1): prefilter 实体抽取改造（Task 2）
7fe0cc5 feat(phase1): 基础设施 + 词典加载器 + 数据库新表（Task 0/1/3）
749924a docs(spec): 完成 crypto-narrative-radar Phase 1 的 spec 文档
03f4db0 (origin/AI_1.0.1) 设计文本     ← 上游最后一个 commit
```

---

## 2. 开工前必读（按顺序）

新对话应先读这 4 份文档，**不要读整个仓库**：

1. **`.kiro/specs/crypto-narrative-radar/requirements.md`** v1.2 —— 需求与验收
2. **`.kiro/specs/crypto-narrative-radar/design.md`** v1.1 —— 架构与接口（§3.6 + §3.7 + §3.8 对 Task 4~8 最关键）
3. **`.kiro/specs/crypto-narrative-radar/tasks.md`** v1.2 —— 实施 checklist（Task 4 在"## Task 4" 小节）
4. **本文档**（handoff.md）—— 实施侧真实踩过的坑 + 环境状态

读完这 4 份，不用再扫代码文件。Task 4 本身的实现在 tasks.md §Task 4 写得很细（4.1~4.5 五个子任务）。

---

## 3. 硬约束（贯穿所有剩余 Task，不能违反）

1. **零 LLM**：新流水线绝不 `import llm.ollama_client`。Task 8.5 会用 `unittest.mock.patch` 替换 `OllamaClient.chat` 并断言 `call_count == 0` 做集成验证。
2. **不破坏老链路**：`services/level1_service.py` / `level2_service.py` / 现有 3 源原始表的 `is_summarized` 字段绝不碰。Task 4.5 的 `test_normalizer_does_not_touch_raw_is_summarized` 专门回归这一点。
3. **不引入新基础设施**：不上 Redis / FAISS / Milvus / pgvector / Kafka。状态只放 PG 或进程内内存。
4. **共用同一个 worker 线程**：Task 8 扩展 `scheduler/jobs.py` 的 `Jobs`，新 `new_services` 和老 `level1_services / level2_services` 在**同一线程**串行跑，避免 Ollama 频繁 swap 模型（Phase 1 不调 LLM，但 Phase 2 会，约束必须保留）。
5. **老 `Level1Service` 调用 `prefilter.split()` 必须继续可用**：`FilterDecision` 已扩展 `entities` 字段但带 `default_factory=list`，两参数构造兼容。动任何 prefilter 相关代码前先读 `tests/test_prefilter.py::test_filter_decision_backward_compat`。

---

## 4. 环境状态

### 开发机

- macOS darwin，zsh
- `.venv` 用 Python 3.12.10（`/Users/ye/Work/Crypto/PomsAI/.venv`）
- **坑**：`.venv/bin/pip` 有 stale shebang 指向旧路径 `/Users/ye/Work/Crypto/POMS/.venv/`，**用 `.venv/bin/python -m pip` 代替**，不要直接用 `pip`。

### 数据库

- PostgreSQL 在 `192.168.1.219:5432`，用户 `all_new`，库 `all_new`（见 `config/settings.py`）
- Alembic 当前 revision = `001`（三张新表已建）
- 测试用 SQLite in-memory（通过 `Base.metadata.create_all(engine)` 快速建表）

### 依赖版本

```
simhash==2.1.2       # 注意：会拉 numpy 2.4.x 作为传递依赖（约 30MB），OK
PyYAML==6.0.2
alembic==1.13.2
```

### 测试基线

```
65 passed, 1 skipped   # skipped 是 tests/test_repositories.py 的种子测试，与本项目无关
1 pre-existing failure: tests/test_ollama_client.py::test_chat_interactive_with_local_ollama
                        （连本地 localhost:11434 的交互测试，不在 CI 绿线，忽略）
```

**每个 Task 完成后的验证命令**：

```bash
.venv/bin/python -m pytest tests/ --ignore=tests/test_ollama_client.py -q
```

---

## 5. Task 4 的启动提示

Task 4 有 5 个子任务（详见 tasks.md）：

| 子任务 | 要做什么 | 关键依赖 |
|-------|---------|---------|
| 4.1 | `services/l0_dedup.py` 实现 `Deduplicator`（SimHash + 小时分桶）| `simhash` 库 |
| 4.2 | `Deduplicator.backfill_from_db(db)` 启动回填 | `NormalizedMessagesRepo.fetch_recent_simhashes` |
| 4.3 | `services/l0_normalizer.py` 实现 `NormalizerService`（三源归一化 + 内嵌 Deduplicator）| `NormalizedMessagesRepo` |
| 4.4 | `tests/test_l0_dedup.py` 5 个单元 case | — |
| 4.5 | `tests/test_l0_normalizer.py` 6 个单元 case（SQLite in-memory + 测试老 `is_summarized` 不被动）| `Base.metadata.create_all` |

### design.md 对应章节

- §3.1 Normalizer_Service（Req 1）
- §3.2 Deduplicator（Req 2）
- **特别注意**：§3.1 末尾 "嵌入 Normalizer 的位置" 伪代码，Task 4.3 构造器字段必须含 `dedup: Deduplicator`，否则 `run_once` 里无处调判重（这是 v1.2 tasks review 时 R3 要修的 Critical 项）。

### 已经铺好的依赖

Task 4 用到的 repo 方法都已实现：

- `NormalizedMessagesRepo.insert(session, ...)` - `INSERT ... ON CONFLICT DO NOTHING` + `RETURNING id`
- `NormalizedMessagesRepo.fetch_recent_simhashes(session, hours=24)` - 回填用
- `NormalizedMessagesRepo.fetch_raw_ids_in_source(session, raw_source)` - 扫描原始表时做"不存在"判定

词典已就绪，`get_dictionaries()` 是进程内单例（`dictionaries/__init__.py` 用 `lru_cache`）。

---

## 6. 实施踩过的坑（避免 Task 4~8 再犯）

### 6.1 YAML 词典里的 `type:` ≠ `entity_type`

- **陷阱**：loader.py 最早把 YAML 的 `type: layer1` 直接赋给 `DictionaryEntry.entity_type`，导致 BTC 被标成 `layer1`，违反 Req 4.3 五类约束。
- **现状**：`entity_type` 由所在文件硬决定（tickers.yaml → `ticker`），YAML 的 `type:` 存到 `DictionaryEntry.category`（Phase 1 只存不用）。详见 design.md §3.3 "命名约定" 块。

### 6.2 SimHash 选型

- **选**：`simhash==2.1.2`
- **原因**：汉明距离 ≤ 3 天然契合 Req 2.2，纯 Python 无额外依赖（除 numpy 传递依赖）
- **不选 `datasketch`**：Jaccard 相似度阈值和汉明距离不是一一映射，Phase 3 升级到 Embedding 时 datasketch 也不会复用

### 6.3 Alembic 迁移必须手写

- **不用 `alembic revision --autogenerate`**——避免误 diff 出现有 5 张表的变更
- 参考 `alembic/versions/001_phase1_initial.py`：手写 `op.create_table` × 3 + `op.create_index` × 10

### 6.4 集成测试用 SQLite in-memory

- `Base.metadata.create_all(engine)` 在 SQLite 上能跑所有新表（没用 PG 独有类型如 ARRAY，这是有意设计）
- Alembic 迁移**不跑**在 SQLite 上（迁移脚本里 `op.create_table` 的 `sa.Text` / `sa.BigInteger` 等都是方言中性的，但 `ON CONFLICT DO NOTHING` 是 PG 专有——repo 方法在集成测试时会走不同分支，注意）

### 6.5 PostgreSQL `ON CONFLICT` 在 SQLite 不可用

- `NormalizedMessagesRepo.insert` 用了 `sqlalchemy.dialects.postgresql.insert` 的 `.on_conflict_do_nothing()`
- **坑**：SQLite in-memory 测试时这个语句会抛错
- **解法**：Task 4.5 的测试要么 mock 掉 `insert`，要么改用 `INSERT OR IGNORE` 的方言分支（推荐前者，测试本意是验证业务逻辑，不是验证方言）。新 AI 遇到这个错需要停下来讨论，不要强改 repo 层。

---

## 7. 第一句启动提示（复制粘贴即用）

打开新对话，贴这段：

```
请读 .kiro/specs/crypto-narrative-radar/handoff.md 和 tasks.md 第 Task 4 小节，
从 Task 4.1 开始实施。实施规则：
1. 严格遵守 handoff.md §3 的五条硬约束
2. 每完成一个子任务跑 .venv/bin/python -m pytest tests/ --ignore=tests/test_ollama_client.py -q
   必须 pass 数只增不减
3. Task 4 做完后停下来等我确认，再推 Task 5
```

---

## 8. 文档版本

- Phase 1 spec（requirements v1.2 / design v1.1 / tasks v1.2）
- 本文档 v1.0 · 2026-05-11 · 对话切换前的最后一份交接
