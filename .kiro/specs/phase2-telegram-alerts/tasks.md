# Phase 2 · Task 2.2 Telegram 告警 · Implementation Tasks

> 基于 requirements.md v1.0 + design.md v1.0 拆出的实施 checklist。

**执行约定**：
- 每完成一个 Task 跑测试，pass 数只增不减（与 Phase 1 同款规则）
- 测试基线起点：109 passed + 3（Phase 2 黑名单）= 112 passed
- 全部 Task 完成后：112 + 5（telegram）+ 11（alert_trigger）= 128 passed

---

## Task 0：依赖检查

- [x] **0.1 验证标准库 urllib 可用**
  - `.venv/bin/python -c "import urllib.request; print('ok')"`
  - 不需要 pip install 任何东西
  - 不动 requirements.txt

## Task 1：TelegramClient

- [x] **1.1 创建 `notifications/` 目录与 `__init__.py`**
  - 空 `__init__.py` 文件
- [x] **1.2 实现 `notifications/telegram_client.py`**
  - 按 design.md §3.1 完整实现 `TelegramClient`（frozen dataclass）
  - `send_text` 方法：urllib.request POST → JSON 解析 → ok 字段判断
  - 4 类异常分支（HTTPError / URLError / 一般 Exception / API ok=False）
  - 文本超 4000 字符自动截断到 3997 + "..."
  - 不抛异常给调用方
- [x] **1.3 单元测试 `tests/test_telegram_client.py`**（5 个用例）
  - test_send_text_200_ok → True
  - test_send_text_http_error_returns_false → False（mock urlopen 抛 HTTPError 401）
  - test_send_text_network_error_returns_false → False（mock urlopen 抛 URLError）
  - test_send_text_unexpected_error_returns_false → False（mock urlopen 抛 RuntimeError）
  - test_send_text_truncates_long_message → 检查发出的 payload 字符数 ≤ 4000
- [x] **1.4 运行测试**
  - `.venv/bin/python -m pytest tests/test_telegram_client.py -v`
  - 预期 5 passed
  - 全量：`pytest tests/ --ignore=tests/test_ollama_client.py -q` 应 117 passed

_Requirements: Req 1, 6.1_

## Task 2：配置分组

- [x] **2.1 创建 `config/_alerts.py`**
  - 按 design.md §3.3 实现 `AlertSettings`
  - 10 个字段：bot_token / chat_id / timeout_seconds / growth_threshold /
    min_count_short / min_cross_source / cooldown_minutes /
    **escalation_growth_multiplier / heartbeat_hours** / message_template
  - 默认 token / chat_id 为空字符串（生效=禁用告警）
- [x] **2.2 修改 `config/settings.py` 加入多继承**
  - import AlertSettings
  - Settings 类继承顺序追加：
    `Settings(DatabaseSettings, RuntimeSettings, LegacySettings, NewPipelineSettings, AlertSettings)`
  - `__all__` 加 `AlertSettings`
- [x] **2.3 验证配置加载**
  - `.venv/bin/python -c "from config.settings import get_settings; s=get_settings(); print(s.alert_growth_threshold, s.telegram_bot_token)"`
  - 预期：`20.0 ` （后面是空字符串）
- [x] **2.4 运行测试**
  - 预期仍 117 passed（无新测试，但配置变更不能破坏现有）

_Requirements: Req 4_

## Task 3：AlertTriggerService

- [x] **3.1 实现 `services/l2_alert_trigger.py`**
  - 按 design.md §3.2 完整实现
  - `AlertRecord` frozen dataclass：last_alerted_at / last_growth_rate / last_cross_source
  - `AlertTriggerService` 字段：db / hotness_repo / telegram_client /
    growth_threshold / min_count_short / min_cross_source /
    cooldown_minutes / **escalation_growth_multiplier / heartbeat_hours** /
    message_template
  - 状态字段：_last_processed_window_end / _alert_records
  - 5 个方法：run_once / _is_eligible / **_decide_alert** / _render_message
  - 不 import llm.ollama_client
- [x] **3.2 单元测试 `tests/test_l2_alert_trigger.py`**（11 个用例，对应 design.md 测试矩阵）
  - test_first_alert_when_all_conditions_met（[首次] 标签）
  - test_alert_skipped_when_growth_below_threshold
  - test_alert_skipped_when_count_short_below_threshold
  - test_alert_skipped_when_cross_source_below_threshold
  - test_no_alert_within_cooldown_without_escalation（60min 内 + 无质变）
  - test_growth_doubled_triggers_escalation（[升级] 标签）
  - test_cross_source_increase_triggers_escalation（[跨源升级] 标签）
  - test_heartbeat_after_6h_without_escalation（[持续] 标签）
  - test_alert_after_cooldown_with_no_change（[重新触发] 标签）
  - test_skips_same_window_on_repeat_run
  - test_send_failure_does_not_update_alert_record
  - SQLite + Mock TelegramClient
  - 复用 Phase 1 同款的 `_SqliteHotnessSnapshotsRepo` pattern
  - 关键 mock：monkeypatch datetime.now() 来控制冷却 / 心跳判断
- [x] **3.3 运行测试**
  - 预期 117 + 11 = 128 passed

_Requirements: Req 2, 3, 6.2, 7_

## Task 4：main.py 注入

- [x] **4.1 修改 `main.py` 追加 Step 5d**
  - 按 design.md §3.4 实现
  - 关键防御：`if settings.telegram_bot_token and settings.telegram_chat_id`
    才构造 AlertTriggerService
  - 配置缺失时打 INFO 日志并跳过
- [x] **4.2 把 alert_service 加到 new_services 列表**
  - 在 normalizer / entity_extractor / hotness_service 之后追加
  - 调度顺序确保 Hotness 先于 AlertTrigger
- [x] **4.3 验证 main.py 仍能 import**
  - `.venv/bin/python -c "import main; print('ok')"`
- [x] **4.4 运行测试**
  - 预期仍 128 passed

_Requirements: Req 5_

## Task 5：本地端到端验收（人工）

> 这一步需要真实的 Telegram Bot Token + chat_id，之前已经准备过。

- [ ] **5.1 把真实 token + chat_id 填入 `config/_alerts.py`**
- [ ] **5.2 临时把 threshold 调成 1.0**（让告警必定触发）
  - `alert_growth_threshold: float = 1.0`
- [ ] **5.3 用 restart.sh 重启**
  - `./scripts/restart.sh`
  - 启动日志含 "AlertTriggerService 启动：growth_threshold=1.0 cooldown=60min ..."
- [ ] **5.4 等下一个整点（最多 15 分钟）**
  - 期望：Telegram 收到告警消息
  - 消息包含实体名 / 增长倍数 / 排名
- [ ] **5.5 把 threshold 改回 20.0，重启**
  - 收到一次告警就够，回到正常配置避免刷屏

_Requirements: Success Metrics 阶段验收_

## Task 6：文档更新

- [ ] **6.1 更新 `docs/operations_guide.md` 加一节"Telegram 告警"**
  - 在第 6 节"改参数"附近加：如何调 threshold / cooldown
  - 在第 7 节"跑测试"附近加：如何验证告警生效
  - 启动日志样例追加 AlertTriggerService 那一行
- [ ] **6.2 在 `docs/faq_design_decisions.md` 追加 Q6**
  - "为什么告警冷却 60 分钟而不持久化？"
  - "告警没收到怎么排查？"
- [ ] **6.3 不改 README.md**（README 是 Phase 0 老链路文档，Phase 2 不动）

## 执行顺序与依赖图

```
Task 0 (依赖检查)
   │
   ├─► Task 1 (TelegramClient + 5 单测)
   │       │
   │       └───────────┐
   │                   │
   ├─► Task 2 (AlertSettings 配置)
   │       │           │
   │       └─► Task 3 (AlertTriggerService + 11 单测)
   │                   │
   │                   └─► Task 4 (main.py 注入)
   │                            │
   │                            └─► Task 5 (人工端到端验收)
   │                                     │
   │                                     └─► Task 6 (文档)
```

**可并行的 Task**（上下文隔离）：
- Task 1 与 Task 2 可并行（TelegramClient 与 AlertSettings 互不依赖）

**必须串行**：
- Task 3 依赖 Task 1 + Task 2（AlertTriggerService 用 TelegramClient + AlertSettings）
- Task 4 依赖 Task 3
- Task 5 依赖 Task 4 + 真实 Telegram 配置

## 完工后状态

```
新增文件：
  notifications/__init__.py
  notifications/telegram_client.py
  config/_alerts.py
  services/l2_alert_trigger.py
  tests/test_telegram_client.py
  tests/test_l2_alert_trigger.py

修改文件：
  config/settings.py             (+1 行 import + 1 行 多继承)
  main.py                        (+约 25 行 Step 5d)
  docs/operations_guide.md       (+1 段)
  docs/faq_design_decisions.md   (+Q6)

测试基线：
  112 → 128 passed（+16，0 回归）
```

预估工时：

| 任务 | 工时 |
|---|---|
| Task 0 | 5 分钟 |
| Task 1 | 60 分钟 |
| Task 2 | 20 分钟 |
| Task 3 | 90 分钟 |
| Task 4 | 30 分钟 |
| Task 5 | 30 分钟（含等下一个整点）|
| Task 6 | 30 分钟 |
| **合计** | **约 4 小时净 coding** |

---

*文档版本：v1.1*
*基于：requirements.md v1.1 + design.md v1.1*
*v1.0 → v1.1：智能冷却 4 路径 + 测试用例 8 → 11 + 日志格式同步*
