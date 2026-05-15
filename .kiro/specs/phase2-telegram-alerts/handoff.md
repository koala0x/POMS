# Handoff · Phase 2 Task 2.2 Telegram 告警

> 交接简报。新对话打开后按本文档顺序读完就能无缝接手 Task 0 → Task 4。
>
> **本文档不是 spec 的一部分**，只是会话之间的工作状态交接。
> Task 5（人工 Telegram 联调）和 Task 6（文档）做完后可删除。

---

## 0. 一行背景

Phase 1 已结束（实体热度排行榜已稳定产出，老链路已关掉只跑新链路）。
Phase 2 第一个子任务是 **Telegram 实时告警**：当 hotness_snapshots 里出现
growth_rate ≥ 20 的实体时，自动推送 Telegram 消息。要做"智能冷却"——
60 分钟内不刷屏，但 growth 翻倍 / 跨源升级 / 6 小时心跳 时立刻重发。

---

## 1. 当前进度

### 已完成

```
Phase 1 全量 Task 0-9                   ← 109 passed
Phase 1 后续优化（黑名单 / 词典精简 / 配置拆分）  ← +3 = 112 passed
Phase 2 spec（requirements / design / tasks）   ← 写完待 commit
```

### 待完成（本次会话目标）

```
Task 0  [ ]  依赖检查（5 分钟）
Task 1  [ ]  TelegramClient + 5 单测（60 分钟）
Task 2  [ ]  AlertSettings 配置（20 分钟）
Task 3  [ ]  AlertTriggerService + 11 单测（90 分钟）
Task 4  [ ]  main.py 注入（30 分钟）

# 本会话不做（要用户线下配合）：
Task 5       人工 Telegram 端到端联调（用户配置真 token + chat_id）
Task 6       文档更新（operations_guide / FAQ Q6）
```

### 测试基线演进

```
Task 0~4 起点：  112 passed, 1 skipped
Task 1 完成：    112 + 5  = 117 passed
Task 3 完成：    117 + 11 = 128 passed
Task 4 完成：    128 passed（main.py 改动不加测试）
```

### Git 状态

当前分支 `AI_1.0.1`，未 push 的本地 commit：

```
13a3577 (HEAD) Phase 2计划
9fc2541         终极设置文档
f0b9e3c         配置文件增加过滤黑名单模式
6ccdda1         配置文件增加黑名单模式
a2bed2c         优化配置文件方式
f8acecd         新增链和叙事
```

工作区**未提交**改动（开工前请用户决定先 commit 还是带着改）：

```
 M scripts/check_status.py                    本会话改的（24h 持续热点段）
?? .kiro/specs/phase2-telegram-alerts/        Phase 2 spec 三件套（已写完）
```

---

## 2. 开工前必读（按顺序）

新对话先读这 4 份文档，**不要扫整个仓库**：

1. **`.kiro/specs/phase2-telegram-alerts/requirements.md`** v1.1 —— 需求 + 7 条 Req + 验收
2. **`.kiro/specs/phase2-telegram-alerts/design.md`** v1.1 —— 架构 + 完整代码骨架（§3.1 TelegramClient + §3.2 AlertTriggerService）
3. **`.kiro/specs/phase2-telegram-alerts/tasks.md`** v1.1 —— 实施 checklist + 测试矩阵
4. **本文档** —— 实施侧的真实坑位 + 环境状态

读完这 4 份就够。Task 1/3 的实现在 design.md 里有完整代码骨架，直接抄即可。

---

## 3. 硬约束（贯穿所有 Task，**绝不能违反**）

这五条从 Phase 1 延续过来：

1. **零 LLM**：`AlertTriggerService` / `TelegramClient` 绝不 import `llm.ollama_client`。
   `tests/test_phase1_pipeline.py::test_phase1_pipeline_end_to_end` 仍会跑且断言
   `mock_chat.call_count == 0`，做完 Task 4 必须仍然绿。
2. **不阻塞主流程**：Telegram 不可达 / 网络超时 → 优雅降级，hotness 主流程
   继续产出。Jobs 异常隔离已经能兜住，但 TelegramClient 自己也不能抛异常。
3. **不引入新依赖**：用标准库 `urllib.request`，不要 `requests` / `httpx`。
   不动 `requirements.txt`。
4. **不破坏向后兼容**：当前 112 passed 必须保持，禁止任何回归。
5. **配置缺失即禁用**：`telegram_bot_token == "" or telegram_chat_id == ""`
   → main.py 跳过 Service 构造（log INFO 即可，不 raise）。

---

## 4. Phase 2 这次的特殊要点（spec 之外的实施级注意事项）

### 4.1 智能冷却的 4 种触发路径必须互斥优先级清晰

design.md §3.2.1 的 `_decide_alert` 决策树**实现时必须按这个顺序**：

```python
1. 首次（rec is None）          → 告警 [首次]
2. 心跳（elapsed >= 6h）        → 告警 [持续 Nh]
3. growth 升级（× ≥ 1.5）        → 告警 [升级]
4. 跨源升级（cross_source 增加）  → 告警 [跨源升级]
5. 60min 内 + 无质变            → 不告警
6. 60min 外 + 仍达阈值          → 告警 [重新触发]
```

**注意**：心跳判断必须放在 growth 升级**之前**——否则一个持续 6 小时但
growth 没大变的热点会落到"60min 内 + 无质变"分支被错过。

### 4.2 11 个单测要覆盖 6 条决策路径

Task 3.2 的测试列表对应 design.md §5 测试矩阵。每个用例对应一条决策路径
（首次 / 不足 / 60min 内 + 无质变 / growth 升级 / 跨源升级 / 心跳 /
重新触发 / send 失败不更新 / 同窗口不重复扫）。

mock 时间方式：用 `monkeypatch.setattr` 替换 `services.l2_alert_trigger.datetime`，
让 `datetime.now()` 返回固定值——这是 Phase 1 `test_l2_hotness.py::test_stable_ordering`
已经踩通的 pattern，可参考。

### 4.3 SQLite 兼容（Phase 1 同款套路）

测试用 SQLite in-memory + `Base.metadata.create_all(tables=[...])` 只建本任务
碰到的表（`hotness_snapshots` 一张就够）。**别建 summary_level1/2**——
ARRAY(BigInteger) 在 SQLite 不可渲染。

`fetch_top_k` 用 SQLAlchemy ORM 写的，SQLite 直接能跑（不需要子类化 repo）。
但 hotness_snapshots 主键 BigInteger 在 SQLite 不会自增——种数据时**手动指定 id**。

### 4.4 时区策略

`HotnessSnapshot.window_end` 字段是 PG TIMESTAMPTZ。SQLite 测试时仍延续
Phase 1 的踩坑结论：

- 写入用 `datetime.now()`（local naive）+ 手动指定 id
- 读出来是 naive，`ts.timestamp()` 按本地时区解释，但**写读两端语义一致**所以 OK
- 生产 PG 不受影响

`_decide_alert` 用 `datetime.now(timezone.utc)`——纯内存计算，不跟 DB 字段比较，
所以可以用 aware datetime。**测试 mock 时 datetime.now 也要返回 aware**，否则会
跟 `last.last_alerted_at` 类型不匹配（aware - naive 抛 TypeError）。

### 4.5 `notifications/` 是新目录

之前不存在。建目录 + 空 `__init__.py`。命名仿照 `llm/` —— 单独一层平级目录，
不是放进 `services/`。

### 4.6 `config/_alerts.py` 加进多继承

`config/settings.py` 当前 Settings 已经多继承 4 个：
`(DatabaseSettings, RuntimeSettings, LegacySettings, NewPipelineSettings)`。

加 AlertSettings 后变成 5 个继承：
`(DatabaseSettings, RuntimeSettings, LegacySettings, NewPipelineSettings, AlertSettings)`。

**验证字段不冲突**：跨 5 个分组类的字段名必须全局唯一（dataclass 多继承
重名会按 MRO 后定义覆盖前定义，是隐藏 bug）。AlertSettings 字段都带
`alert_` / `telegram_` 前缀，跟前 4 个分组无重名，安全。

---

## 5. 环境状态

### 开发机

- macOS darwin / zsh
- Python 3.12.10 在 `/Users/ye/Work/Crypto/PomsAI/.venv`
- **坑**：`.venv/bin/pip` shebang 指向旧路径，**用 `.venv/bin/python -m pip` 代替**
- 服务器目前**正在跑** main.py（用户已用 `restart.sh` 启动），改代码后用户会自己重启

### 数据库

- PostgreSQL `192.168.1.219:5432`，库 `all_new`，用户 `all_new`
- Phase 1 三张新表 + 老 5 张表都在跑，hotness_snapshots 持续产出
- 测试用 SQLite in-memory（不影响生产）

### 重要工具

- **`./scripts/restart.sh`**：用户已有的安全重启脚本（先 SIGINT 老进程 + 等 15s + 起新进程）。
  做完 Task 4 后用户跑这个生效。**别让用户直接 `python main.py`**——会留下双进程。
- **`scripts/check_status.py`**：6 节自检脚本，看系统状态用

### 当前测试基线

```bash
.venv/bin/python -m pytest tests/ --ignore=tests/test_ollama_client.py -q
# 应输出: 112 passed, 1 skipped
```

`test_ollama_client.py` 跑本地 Ollama 是预先存在的不绿用例，跟新链路无关，
继续 ignore。

### 用户的 Telegram 配置

用户已通过 BotFather 创建了 Bot 并拿到了 token + chat_id。但**Token 不在 spec 里**——
Task 4 完成后由用户自己填到 `config/_alerts.py`。本会话不需要真 token / chat_id，
所有测试都 mock TelegramClient。

**Token 存放策略**：用户已决策 = **方案 A**（直接硬编码到 `config/_alerts.py`，
commit 进 git）。理由是仓库非公开 + 泄露后 BotFather `/revoke` 5 秒重发即可。
不要做环境变量加载 / 单独本地配置文件这种"更安全"的方案——那是 Phase 3 的事，
本任务保持简单。

---

## 6. 实施踩过的坑（Phase 1 + Phase 1 后续）

### 6.1 SQLAlchemy 多继承 dataclass 字段不能重名

config 已经拆 5 个分组，字段名跨分组重名会让 MRO 后定义覆盖前定义。
做 Task 2 时**确认 AlertSettings 的 10 个字段名跨分组唯一**。

### 6.2 `lru_cache` + 词典 / 配置改动需要重启

`get_settings()` / `get_dictionaries()` 都用 `@lru_cache(maxsize=1)`。
改 settings 或 yaml **必须重启进程**才会生效。Task 4 后让用户跑
`./scripts/restart.sh`。

### 6.3 双进程踩过的坑（用户的真实经验）

如果用户问"为什么改了 settings 不生效"，**先看 `pgrep -fl 'python.*main.py'`**——
之前用户开新进程没杀旧进程，UPSERT 让老进程的写入覆盖新进程，看起来像 bug。
告诉用户用 `restart.sh` 别裸 `python main.py`。

### 6.4 SQLite 兼容（Phase 1 已成熟的 pattern）

- `Base.metadata.create_all(engine, tables=[xxx])` 只建本任务表
- BigInteger 主键不自增，测试种数据时手动指定 id
- `DateTime(timezone=True)` 在 SQLite 丢 tzinfo，测试时统一用本地 naive datetime

### 6.5 loguru 日志在 pytest 用 sink 抓

pytest 的 `caplog` 默认只抓 stdlib logging。loguru 的输出要在 fixture 里
`logger.add(sink_fn)` 注入 sink，测完 `logger.remove`。Phase 1
`tests/test_l2_sliding_counter.py::loguru_capture` 有现成 fixture 可复制。

### 6.6 Telegram API 在国内被墙

服务器跑墙内的话 send_text 都会失败。这是 spec 里的 Risk 1，处理方式是
"优雅降级不阻塞主流程"。**不需要在代码里检测国内/国外**——失败就 log error
然后返回 False，下一轮 hotness 触发时再试。

---

## 7. 第一句启动提示（复制粘贴即用）

打开新对话，贴这段：

```
请读 .kiro/specs/phase2-telegram-alerts/handoff.md 和 spec 三份文档
（requirements.md / design.md / tasks.md），从 Task 0 开始实施。实施规则：

1. 严格遵守 handoff.md §3 的五条硬约束（零 LLM / 不阻塞主流程 /
   不引入新依赖 / 不破坏向后兼容 / 配置缺失即禁用）

2. 每完成一个 Task 跑：
   .venv/bin/python -m pytest tests/ --ignore=tests/test_ollama_client.py -q
   pass 数只增不减（112 → 117 → 128 → 128）

3. Task 4 做完后停下来等我确认（Task 5 是人工配真 token 联调，
   Task 6 是文档，先不做）

4. 如果中间发现 spec 有遗漏 / 矛盾，**先停下来问我**，不要自己推断改 spec
```

---

## 8. 文档版本

- Phase 2 spec（requirements v1.1 / design v1.1 / tasks v1.1）
  > v1.0 → v1.1 主要变更：智能冷却（4 种触发路径）+ alert_type 标签
- 本文档 v1.0 · 2026-05-13 · 对话切换前的最后一份交接

---

## 9. 你（新对话的 AI）开工后第一件事

1. 跑 `.venv/bin/python -m pytest tests/ --ignore=tests/test_ollama_client.py -q`
   确认 **112 passed, 1 skipped**（基线一致才能继续）
2. 跑 `git status` 看未提交改动是不是上面 §1 列的那些
3. 读 spec 的 design.md §3.1 和 §3.2 那两段代码骨架
4. 然后开始 Task 0
