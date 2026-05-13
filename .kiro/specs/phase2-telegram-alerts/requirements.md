# Phase 2 · Task 2.2 Telegram 实时激增告警 · Requirements

> Phase 2 第一个子任务。从"被动看 hotness 榜单"升级到"主动推送热点"，
> 让用户离开终端也能第一时间收到突发热点。

## 背景

Phase 1 已经能产出每 15 分钟刷新的 Top-20 实体热度排行榜（`hotness_snapshots`）。
但用户需要主动跑 `scripts/check_status.py` 才能看到结果，时效性差。

Phase 2 第一步：把 hotness 数据接上 Telegram Bot，实现：
- **growth_rate 突破阈值** → 自动推送 Telegram 消息
- 用户在手机上随时收到，不用守着终端

参考终极设计文档 §11.1（实时激增警报）。

## 用户角色

- **唯一用户**：项目所有者（你）
- **设备**：Telegram App（手机 + 桌面端）
- **使用场景**：白天看盘 / 夜间睡觉，不可能 24h 守着 PostgreSQL

## 边界与非目标

### 包含

1. 一个 `TelegramClient` 模块，封装 sendMessage HTTP 调用
2. 一个 `AlertTriggerService`：每次 `HotnessService.run_once` 写完榜单后
   触发，扫描刚写入的 hotness_snapshots，挑出"够格告警"的实体，调
   `TelegramClient` 发消息
3. 告警去重：同一实体在冷却期内只告警一次（避免每 15 分钟重复推送）
4. 告警门槛：通过 settings 配置（最低 growth_rate / 最低 count_short / 最低 cross_source）
5. 优雅降级：Telegram 不可达不影响 hotness 主流程
6. 单元测试 + 集成测试覆盖核心路径

### 不包含（Phase 2 后续 / Phase 3）

1. ❌ Telegram Bot 反向命令交互（`/top` `/help` 等命令）—— Phase 3 再考虑
2. ❌ 多用户多 chat 推送（仅支持单 chat_id）—— 一个人用够了
3. ❌ Markdown / HTML 富文本格式 —— 纯文本起步，复杂格式留 Phase 2.5
4. ❌ 告警历史持久化到 DB —— 起步只用进程内内存做去重
5. ❌ 失败重试 / 队列 —— 失败就跳过，下一轮 hotness 触发时再算
6. ❌ 限流（Telegram API 30 msg/s 上限）—— 单实体每小时最多 1 次推送，
   远低于上限，不需要专门限流

## Requirements

### Req 1：Telegram 客户端

1.1 应实现 `llm/...` 平级的 `notifications/telegram_client.py`
1.2 类 `TelegramClient` 接受 `bot_token` + `chat_id` 两个参数构造
1.3 `send_text(text: str, *, parse_mode: str | None = None) -> bool` 方法：
    - 调用 `https://api.telegram.org/bot<token>/sendMessage`
    - HTTP timeout 默认 10 秒（可配置）
    - 成功返回 True，任何异常（网络/HTTP 4xx/5xx）返回 False + log.error
    - 不抛出异常给调用方
1.4 不引入新的依赖库——用标准库 `urllib.request` 即可，避免引入 requests
1.5 测试可通过 `unittest.mock.patch` 替换 HTTP 调用层

### Req 2：告警触发器

2.1 应实现 `services/l2_alert_trigger.py`
2.2 类 `AlertTriggerService` 接受参数：
    - `db: Database`
    - `hotness_repo: HotnessSnapshotsRepo`
    - `telegram_client: TelegramClient`
    - `growth_threshold: float`（默认 20.0）
    - `min_count_short: int`（默认 3，避免 1 次提及就告警）
    - `min_cross_source: int`（默认 1，单源也告警；Phase 1 跨源命中率太低）
    - `cooldown_minutes: int`（默认 60，同实体常规冷却期）
    - `escalation_growth_multiplier: float`（默认 1.5，growth 翻这么多倍即强制再告警）
    - `heartbeat_hours: int`（默认 6，持续热点最长不告警时长）
2.3 方法 `run_once() -> bool`：
    - 查最新 `window_end`（最新一份 hotness 榜）
    - 如果跟上次处理的 window_end 相同 → 返回 False（不重复处理）
    - 否则筛出符合条件的 records：
      - `growth_rate >= growth_threshold`
      - `count_short >= min_count_short`
      - `cross_source >= min_cross_source`
      - 通过"智能冷却"判断（见 2.4）
    - 对每条调 `telegram_client.send_text(...)` 推送
    - 推送成功 → 更新该 entity 的告警状态
    - 任意推送成功返回 True
    - 没新榜 / 没合格 records / 推送全失败 返回 False

2.4 **智能冷却策略**（核心：60 分钟内不刷屏，但"质变"立刻重发）：
    - 同一 entity 上次告警过的话，需要满足下面**任一**才能再发：
      - **常规冷却到期**：距上次告警 ≥ `cooldown_minutes`（默认 60 分钟）
      - **growth 升级**：本次 growth_rate ≥ 上次告警时 growth_rate × `escalation_growth_multiplier`（默认 ×1.5）
      - **cross_source 升级**：本次 cross_source > 上次告警时的 cross_source（多平台共振）
      - **心跳提醒**：距上次告警 ≥ `heartbeat_hours`（默认 6 小时），即便没质变也告警一次保持持续性提醒
    - 实现方式：进程内 `dict[entity, AlertRecord]`，每个 record 含
      `(last_alerted_at, last_growth_rate, last_cross_source)`
    - 重启后冷却失效（每实体最多多 1 次告警），不持久化

2.5 **告警类型标记**：消息里清晰标注本次告警的触发原因，让用户知道是"首次"还是"升级"还是"心跳"：
    - 首次告警：`🔥 [首次]`
    - growth 翻倍：`🔥 [升级 → growth ×{ratio}]`
    - cross_source 增加：`🔥 [跨源升级 +{n}]`
    - 心跳：`🔥 [持续 {hours}h]`

### Req 3：消息格式

3.1 默认纯文本格式，模板可配置（settings）
3.2 模板支持以下变量：
    - `{alert_type}` 触发原因标签（"[首次]" / "[升级 → growth ×2.0]" / "[跨源升级 +1]" / "[持续 6h]"）
    - `{entity}` 实体名
    - `{entity_type}` 类型（ticker/chain/narrative/project）
    - `{growth_rate}` 增长倍数（保留 1 位小数）
    - `{count_short}` 1h 提及次数
    - `{cross_source}` 跨源数
    - `{is_new_entity_mark}` "新冒头" 标记，True 时显示"★ 新实体"
    - `{window_end}` 窗口时刻（YYYY-MM-DD HH:MM）
    - `{rank}` 当前榜单排名
3.3 默认模板示例：

    ```
    🔥 {alert_type}
    实体: {entity} ({entity_type})
    增长: {growth_rate}x（基于过去 7 天基线）
    提及: {count_short} 次 / 1h
    跨源: {cross_source}
    {is_new_entity_mark}
    rank: #{rank} @ {window_end}
    ```

3.4 异常情况降级：模板字段缺失时 log.warning 但仍发送（用 `<n/a>` 占位）

### Req 4：配置

4.1 新增 `config/_alerts.py` —— `AlertSettings` 分组类，包括：
    - `telegram_bot_token: str` —— 默认空字符串，空时禁用告警
    - `telegram_chat_id: str` —— 默认空字符串
    - `telegram_timeout_seconds: int = 10`
    - `alert_growth_threshold: float = 20.0`
    - `alert_min_count_short: int = 3`
    - `alert_min_cross_source: int = 1`
    - `alert_cooldown_minutes: int = 60`
    - `alert_escalation_growth_multiplier: float = 1.5`（growth 翻这么多倍即升级告警）
    - `alert_heartbeat_hours: int = 6`（持续热点的心跳提醒间隔）
    - `alert_message_template: str = <见 Req 3.3 默认模板>`
4.2 通过 `Settings` 多继承自动暴露
4.3 `telegram_bot_token == "" or telegram_chat_id == ""` → 启动时跳过
    AlertTriggerService 初始化（log.info "Telegram 告警未配置，已禁用"）

4.4 **Token 存放策略（用户决策：方案 A）**：
    - 直接硬编码在 `config/_alerts.py`，commit 进 git 仓库历史
    - 理由：仓库非公开 + token 泄露后 BotFather `/revoke` 5 秒重发
    - **不**走环境变量 / 单独本地配置文件方案（Phase 3 视情况再升级）
    - 实施细节：Task 2.1 默认空字符串，Task 5（人工联调）由用户填真值

### Req 5：与 Worker 集成

5.1 `main.py` 在新链路初始化阶段构造 `AlertTriggerService`（如配置就绪）
5.2 注入 `Jobs.new_services`，与 Normalizer / EntityExtractor / Hotness
    共用同一个 worker 线程
5.3 调度顺序：在 `HotnessService` 之后（保证 hotness_snapshots 已写入）
5.4 任意一轮 `AlertTriggerService.run_once` 抛异常 → Jobs 已有的异常隔离
    机制兜住，不影响其他 service

### Req 6：测试覆盖

6.1 `tests/test_telegram_client.py`：mock urllib，覆盖：
    - 成功 200 → 返回 True
    - 4xx 错误 → 返回 False + log.error
    - 网络超时 → 返回 False + log.error
    - 不抛出任何异常
6.2 `tests/test_l2_alert_trigger.py`：用 SQLite + mock TelegramClient，覆盖：
    - 触发条件全满足 → telegram_client.send_text 被调用（首次告警）
    - growth_rate 不足 → 不调用
    - count_short 不足 → 不调用
    - 同一 entity 在常规冷却期内（且无质变）→ 不重复推送
    - **growth 翻倍 → 立刻升级告警**（即便在 60 分钟冷却内）
    - **cross_source 增加 → 立刻升级告警**（即便在 60 分钟冷却内）
    - **6 小时心跳：距上次告警 > heartbeat_hours → 即便没质变也告警**
    - 冷却期外 → 重新允许推送（视为首次再触发）
    - window_end 与上次相同 → 不重复扫描
    - send_text 返回 False → 该 entity **不**进入冷却（下一轮可重试）
    - 配置缺失（token/chat_id 为空）→ Service 不构造，由 main.py 跳过
    - 告警类型标签正确（首次 / 升级 / 跨源升级 / 心跳）
6.3 不允许真的调 Telegram API（CI 不可达）

### Req 7：日志规范

7.1 INFO："alert sent: entity=BTC growth=25.3 type=[首次]"（含告警类型标签）
7.2 INFO："alert skipped: entity=ETH 60min 内无质变"
7.3 ERROR："telegram send failed: <reason>"
7.4 启动 INFO："AlertTriggerService 启动：growth_threshold=20.0 cooldown=60min escalation×1.5 heartbeat=6h"

## Success Metrics

### 阶段验收（部署后 24 小时内）

- [ ] **配置生效**：`./scripts/restart.sh` 启动后日志含
      "AlertTriggerService 启动：threshold=20.0 ..."
- [ ] **测试基线**：`pytest` 仍 100% pass，新增 ≥ 10 个测试用例（telegram + alert_trigger）
- [ ] **零 LLM 约束保留**：`AlertTriggerService` 不 import `llm.ollama_client`
- [ ] **告警冷却**：同一 entity 在 60 分钟内最多收到 1 次告警

### 业务验收（部署后 7 天内）

- [ ] **至少触发 5 次告警**：意味着系统在 7 天内识别到 5 次"突然热"
      （如果 0 次，可能 threshold 太高，调成 10 再看）
- [ ] **告警内容可读**：消息能在 Telegram 上正常显示，能从字段判断该不该跟进
- [ ] **不影响主流程**：Telegram 不可达时（VPN 断了/服务器跑墙内）hotness
      继续产出，没有 ERROR 堆积

### 反向验证（确认没引入新风险）

- [ ] **零 LLM 验证**：`pytest tests/test_phase1_pipeline.py -v` 仍然
      `mock_chat.call_count == 0`
- [ ] **老链路开关仍可用**：`disable_legacy_pipeline=False` 切回去能跑老链路
- [ ] **告警禁用时无副作用**：把 `telegram_bot_token=""` 设回去，服务正常跑，
      hotness_snapshots 照常产出

## 硬约束（不可妥协）

1. **零 LLM**：AlertTriggerService 严格不 import `llm/ollama_client`
2. **不阻塞主流程**：告警失败、Telegram 不可达、网络超时 → 不影响 hotness
   主流程的写入 / worker 主循环
3. **不破坏老链路兼容**：现有 109 + 3 = 112 个测试必须保持全部通过
4. **冷却期默认 60 分钟 + 智能升级**：常规情况同实体 1 小时内最多发 1 次；
   但 growth 翻倍、cross_source 增加、距上次 > 6 小时 三种"质变"任一发生时
   立刻升级告警，避免错过"持续升温"的真正信号
5. **配置缺失即禁用**：token/chat_id 任一为空 → 不构造 AlertTriggerService
   而不是 raise；让用户能"先观察 hotness 再决定要不要开告警"

## 依赖与风险

### 依赖

- Telegram Bot Token + chat_id（用户需事先准备，已确认会准备）
- 服务器需能访问 `api.telegram.org`（如果在国内，需 VPN）
- Phase 1 hotness_snapshots 表结构（已有）

### 风险

| 风险 | 概率 | 缓解 |
|---|---|---|
| Telegram API 在国内被墙 | 高 | VPN；失败优雅降级不影响主流程 |
| 告警刷屏（同一 entity 反复触发）| 中 | 60 分钟冷却期 |
| 告警过少（threshold 太高，1 周 0 次）| 中 | 部署 24h 后观察实际 growth 分布，调参 |
| Bot Token 泄露 | 低 | 通过 settings 配置；建议生产环境用环境变量（Phase 3 处理）|
| 与 Phase 1 hotness 服务时序冲突 | 低 | 调度顺序固定：先 Hotness 后 AlertTrigger |

---

*文档版本：v1.1*
*基于：终极设计文档 §11.1 "实时激增警报"*
*预估工时：半天（4~6 小时净 coding 时间，不含 spec 写作）*
*v1.0 → v1.1：加入智能冷却（growth 翻倍 / 跨源升级 / 6h 心跳）+ alert_type 标签*
