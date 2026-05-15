# Phase 2 · Task 2.4 实时告警触发 · Requirements

> Phase 2 第四个子任务（紧跟多窗口热度榜、Telegram 告警上线）。把端到端
> 告警延迟从 14~15 分钟压到 1~2 分钟，让"早期热点发现"工具真正具备早期能力。

## 背景

Phase 2.1（多窗口热度榜）+ Phase 2.2（Telegram 告警）上线后的告警链路：

```
EntityExtractor 写 entity_mentions
    ↓ 等到 :00 / :15 / :30 / :45 整点（最坏 14~15 分钟延迟）
HotnessService(1h) 跑榜 → hotness_snapshots
    ↓
AlertTriggerService 扫最新榜 → 推 Telegram
```

**痛点**：新 meme 在 `10:01` 突然爆火，最坏要等到 `10:15` 整点被捕捉、
`10:16` 才推 Telegram，总延迟 **14~15 分钟**。

对**单人独立开发者做加密货币早期热点发现工具**这个产品定位是真痛——一个新热点
的爆发期可能只有 1~2 小时，错过头 30 分钟基本就没机会埋伏。Telegram 告警是
用户真正的入场信号，**告警延迟是这次任务要解决的唯一痛点**。

**Task 2.4 目标**：EntityExtractor 写完一批新提及后立刻"通知"下游做一次轻量
计算 + 告警，不等下一个整点。延迟压到 **1~2 分钟**。

## 用户角色

- **唯一用户**：项目所有者（你）
- **设备**：本地 Mac mini 24/7 跑 worker + Telegram App
- **使用场景**：白天看盘 / 夜间睡觉，期望新热点冒头几分钟内收到推送
- **决策风格**：单人 + 单 worker，选最简单可工作的方案。能在内存里串行
  算清楚的事情就不引入分布式（不上 Redis / 消息队列 / 多线程）

## 边界与非目标

### 包含

1. 新增 `services/l2_realtime_trigger.py` —— `RealtimeAlertService`
2. EntityExtractor 加可选 `realtime_trigger` 字段；`run_once()` 末尾调
   `realtime_trigger.notify(n_added)`
3. 共享 `AlertRecord` 冷却 dict：实时和整点写**同一个** `_alert_records`
4. 4 个新配置（`realtime_enabled` / `realtime_burst_threshold` /
   `realtime_growth_threshold` / `realtime_min_count_short`）
5. main.py Step 5e 注入 RealtimeAlertService，反向注入到
   `entity_extractor.realtime_trigger`
6. 9 个新测试 + 现有 135 个零回归
7. 文档：`operations_guide §6.1` 实时调参、`faq_design_decisions Q8`

### 不包含

1. ❌ **不写 hotness_snapshots 表** —— 实时榜只在内存算 Top-K，避免分钟级
   时间戳（如 `10:23:47`）污染整点对齐的主表（Phase 2.1 三窗口都对齐到
   `:00/:15/:30/:45`）
2. ❌ **不引入新依赖** —— 全部用现有标准库 + 已有 repo 接口
3. ❌ **不动告警分通道结构** —— 仍只走单 Telegram chat_id；多通道留 Phase 2.5+
4. ❌ **不引入异步 / 多线程** —— `_trigger_immediate` 同步执行；< 5s 不切异步
5. ❌ **不持久化冷却记录** —— 进程内 dict，重启失效；每实体最多多 1 条告警
6. ❌ **不参数化 alert 通道** —— RealtimeAlertService 复用 AlertTriggerService
   的 TelegramClient 实例和决策函数，不做接口抽象
7. ❌ **不覆盖 6h / 24h 实时榜** —— 短窗时长本身就是 6 小时，实时无意义；
   只跑 1h 维度

## Requirements

### Req 1：RealtimeAlertService

1.1 应实现 `services/l2_realtime_trigger.py`
1.2 类 `RealtimeAlertService` 接受参数：
    - `db / mentions_repo / sliding_counter / telegram_client`（依赖注入）
    - `burst_threshold: int = 50`（攒够多少新提及触发）
    - `realtime_threshold: float = 30.0`（比整点 20 严）
    - `min_count_short: int = 5`（比整点 3 严）
    - `shared_alert_records: dict[str, AlertRecord]`（**与 AlertTriggerService
      共享的冷却 dict 同一引用**）
    - `cooldown_minutes / escalation_growth_multiplier / heartbeat_hours`
      （与 AlertTriggerService 完全一致）
    - `message_template`（默认与整点共用，渲染时 alert_type 自带 "[实时]" 前缀）
1.3 状态字段：`_pending_count: int = 0`（不持久化）
1.4 方法 `notify(n_added)`：
    - `_pending_count += n_added`
    - 达 `burst_threshold` → 调 `_trigger_immediate()`
    - 整个方法 try/except 兜底，**绝不**抛异常给 EntityExtractor（硬约束 2）
1.5 方法 `_trigger_immediate()`：
    - 当前时刻**不对齐 quarter**，直接 `datetime.now(UTC)` 作 window_end
    - 候选集 `sliding_counter.active_entities("1h")`（聚焦"刚冒头"实体）
    - 公式与 HotnessService(1h) 完全一致：
      `growth_rate = short_count / max(baseline_per_hour, smoothing)`
    - 筛 `growth_rate >= realtime_threshold && count_short >= min_count_short`
    - 调 `decide_alert()`（复用 4 路径决策树，详见 Req 3.2）
    - 渲染时 `alert_type` 前缀加 `"[实时]"`
    - 推送成功才写入 `shared_alert_records`
1.6 `_pending_count` 处理（详见 design §3.1.2）：
    - 全部成功 / 没合格 entity → 清零
    - send 全失败 → 不清零，下一轮 burst 再试（Task 1.4 用例 6）
1.7 **不写 hotness_snapshots 表**：全程不调 hotness_repo

### Req 2：EntityExtractor hook

2.1 应改 `services/l1_entity_extractor.py`：加可选字段
    `realtime_trigger: Optional[RealtimeAlertService] = None`
2.2 **dataclass 改造**（关键决策，详见 design §3.2）：
    - 当前 EntityExtractor 是 `@dataclass(frozen=True)`，不能运行时给字段赋值
    - 本任务把 `frozen=True` 去掉，让 main.py 能反向注入
      `entity_extractor.realtime_trigger = svc`
    - 反向注入的原因：RealtimeAlertService 需要 `alert_service._alert_records`，
      而 alert_service 必须在 EntityExtractor 之后构造，形成时序循环
    - 风险：去掉 frozen 后该 dataclass 仍是 main.py 全局单例，正常代码路径
      不会改其他字段；测试覆盖（Task 2.2 用例 9）保证向后兼容
    - 替代方案对比与取舍详见 design §3.2.2
2.3 `run_once()` 末尾追加（在 `logger.info` 之前）：
    ```python
    if self.realtime_trigger is not None and len(to_insert) > 0:
        try:
            self.realtime_trigger.notify(len(to_insert))
        except Exception as e:
            logger.error("realtime_trigger.notify 异常（已隔离）：{}", e)
    ```
2.4 `to_insert == []` 时不调 notify（无新提及，没必要触发）
2.5 双重异常隔离：notify 内部已 try/except，这里再加一层

### Req 3：共享 AlertRecord 冷却（绝对核心）

3.1 RealtimeAlertService 与 AlertTriggerService 共享**同一个**
    `dict[str, AlertRecord]` 对象引用（不是值拷贝）
3.2 `decide_alert` 决策逻辑与 AlertTriggerService 完全等价（4 路径决策树）：
    1. `last is None` → `True, "[首次]"`
    2. `elapsed >= heartbeat_hours` → `True, "[持续 Nh]"`
    3. `growth ≥ 上次 × multiplier` → `True, "[升级 → growth ×X.X]"`
    4. `cross_source` 增加 → `True, "[跨源升级 +N]"`
    5. cooldown 内 + 无质变 → `False, ""`
    6. cooldown 外 + 无质变 → `True, "[重新触发]"`

    实现：把 `_decide_alert` 抽到 `services/l2_alert_trigger.py` 模块顶层
    （独立函数 `decide_alert`），两个 Service 都调（详见 design §3.3.2）。
3.3 跨服务无重复推送：实时在 `:14:30` 推过 NEWMEME，整点 `:15:00` 扫到同
    entity 时由于 30 秒前刚告警过 + 没明显升级，被冷却拦下。
3.4 跨服务可升级：实时 growth=20，整点 growth=35（>20×1.5）→ 整点告警立即
    覆盖，不被冷却拦。
3.5 单 worker 串行设计下安全：
    - worker 顺序跑 `[Normalizer, EntityExtractor, Hotness×3, AlertTrigger]`
    - `notify()` 在 EntityExtractor 内同步触发 `_trigger_immediate()`
    - 此时 AlertTriggerService 还没开始跑，绝无并发写 `_alert_records`
    - CPython GIL 保证 dict 单 key set/get 原子
    - 未来拆多 worker 时再加 `threading.Lock`；当前阶段不需要

### Req 4：配置（4 个新字段）

4.1 应在 `config/_alerts.py` 的 `AlertSettings` 末尾追加：

    | 字段 | 类型 | 默认 | 说明 |
    |---|---|---|---|
    | `realtime_enabled` | `bool` | `True` | 总开关 |
    | `realtime_burst_threshold` | `int` | `50` | 累积多少新提及触发 |
    | `realtime_growth_threshold` | `float` | `30.0` | 比整点 20 严 50% |
    | `realtime_min_count_short` | `int` | `5` | 比整点 3 严 |

4.2 加载验证：`python -c "from config.settings import get_settings;
    s=get_settings(); print(s.realtime_enabled, s.realtime_burst_threshold,
    s.realtime_growth_threshold, s.realtime_min_count_short)"` 应输出
    `True 50 30.0 5`
4.3 配置缺失即降级：`realtime_enabled=False` 或 Telegram 未配置 → main.py
    跳过构造，等同 Phase 2.2 行为，零运行时开销
4.4 `realtime_growth_threshold=30` 比整点 `20` 严的理由：
    - 短窗（不到 1h，可能几分钟）的 growth_rate 抖动远比整点榜大
    - 实时触发频率高（最坏每轮一次），把噪音放进去会刷屏
    - 30 倍是"明显异常"信号，比 20 倍的"够热"更适合做实时入口

### Req 5：与 Worker 集成

5.1 `main.py` 在 Step 5d（AlertTriggerService）之后新增 Step 5e：构造
    RealtimeAlertService，传入 `shared_alert_records=alert_service._alert_records`
5.2 反向注入：`entity_extractor.realtime_trigger = realtime_service`
5.3 调度顺序不变：worker 仍按 `[Normalizer, EntityExtractor, Hotness×3,
    AlertTrigger]` 串行；RealtimeAlertService **不**进 `new_services`，
    它由 EntityExtractor 内部 `notify()` 同步触发
5.4 任意一轮 `_trigger_immediate` 抛异常 → notify 的 try/except 兜住，
    不影响 EntityExtractor 写库
5.5 启动条件（4 选 1 任一不满足即跳过）：
    1. `settings.realtime_enabled`
    2. `settings.telegram_bot_token` 非空
    3. `settings.telegram_chat_id` 非空
    4. AlertTriggerService 已成功构造
5.6 任一不满足时打印 INFO 日志说明跳过原因

### Req 6：测试覆盖（净新增 9 个用例）

测试基线起点 / 落点（沿用 tasks.md 推演）：

```
Phase 2.1 完工                                   135 passed
+ Task 1.4 RealtimeAlertService 单元 6 cases  →  141
+ Task 2.2 EntityExtractor hook 集成 3 cases  →  144
合计 144 passed（+9，0 回归）
```

**测试演进逻辑**（与 tasks.md Task 0~6 对应）：

- **Task 0 基线**：先确认 135 passed 起点干净
- **Task 1.4（6 cases，→141）**：先把 RealtimeAlertService 内部行为锁死，
  不依赖 EntityExtractor。建立"实时计算"逻辑独立可信
- **Task 2.2（3 cases，→144）**：让 EntityExtractor 与 RealtimeAlertService
  真正联动，验证 hook 的最小侵入性 + 向后兼容
- **Task 3~5 不增加用例**：只改配置 / main.py 注入 / 端到端验收，靠现有
  144 个用例兜底防回归

#### 6.1 `tests/test_l2_realtime_trigger.py`（Task 1.4，6 个用例）

1. `test_notify_below_threshold_does_not_trigger` —— `notify(40)` 后
   `send_text.call_count == 0`，`_pending_count == 40`
2. `test_notify_at_threshold_triggers_immediate` —— `notify(50)` 触发；
   send_text 被调；`_pending_count == 0`
3. `test_immediate_uses_realtime_threshold` —— `realtime_threshold=30`，
   growth=25 不告警 / growth=35 告警
4. `test_immediate_shares_alert_records_with_integral` —— 外部 dict 作
   `shared_alert_records`，触发后外部 dict 含该 entity 的 AlertRecord
   （验证引用语义）
5. `test_immediate_does_not_write_hotness_snapshots` —— hotness_repo 全程
   未被调用
6. `test_immediate_send_failure_does_not_consume_pending` —— `send_text`
   返回 False → `_pending_count` 不清零

#### 6.2 `tests/test_l1_entity_extractor.py`（Task 2.2，新增 3 个用例）

7. `test_realtime_trigger_called_with_inserted_count` ——
   `entity_extractor.realtime_trigger = mock_trigger`；run_once 写入 N 条 →
   `mock_trigger.notify.assert_called_once_with(N)`
8. `test_realtime_trigger_not_called_when_zero_insertions` ——
   `to_insert == []` → `mock_trigger.notify.call_count == 0`
9. `test_realtime_trigger_none_does_not_break` —— 默认 None 时 run_once
   行为与 Phase 2.1 等价（向后兼容回归）

#### 6.3 现有用例零回归

- 现有 135 个用例 100% pass，**不允许改测试代码**
- `test_l1_entity_extractor.py` 已有用例都是默认构造（没传
  `realtime_trigger`），新字段 `default=None` 让它们继续过

#### 6.4 测试约束

- 不允许真调 Telegram API
- 不允许真连 PG（用 Mock SlidingCounter / repos / TelegramClient +
  monkeypatch `datetime.now`）

### Req 7：日志规范

7.1 启动 INFO（main.py Step 5e）：
    `RealtimeAlertService 启动：burst=50 threshold=30.0 min_count_short=5`
7.2 跳过 INFO（任一启动条件不满足）：
    - `RealtimeAlertService 未启用（realtime_enabled=False）`
    - `RealtimeAlertService 跳过：Telegram 未配置`
    - `RealtimeAlertService 跳过：依赖 AlertTriggerService 未启用`
7.3 触发 INFO（每次 `_trigger_immediate`）：
    `realtime trigger fired: pending=50` / `done: candidates=N alerts=M`
7.4 推送 INFO（与整点告警同格式但带 [实时] 标签）：
    `alert sent: entity=NEWMEME growth=42.5 type=[实时][首次]`
7.5 推送失败 ERROR：
    `realtime alert send failed (will retry next burst): entity=NEWMEME`
7.6 异常隔离 ERROR：
    `realtime_trigger.notify 异常（已隔离）：<reason>`

## Success Metrics

### 阶段验收（部署后 24 小时内）

- [ ] **测试基线**：`pytest` 100% pass，135 → 144（+9，0 回归）
- [ ] **配置生效**：启动日志含
      `RealtimeAlertService 启动：burst=50 threshold=30.0 min_count_short=5`
- [ ] **EntityExtractor 行为不变**：worker 主链路日志频率与延迟与上线前一致
- [ ] **零 LLM 验证**：`pytest tests/test_phase1_pipeline.py -v` 仍然
      `mock_chat.call_count == 0`

### 业务验收（部署后 7 天内）

- [ ] **延迟达标**：抽 ≥ 3 次实时告警，从最早一条相关 normalized_message
      的 ts 到 Telegram 推送时间，端到端延迟 ≤ 2 分钟
- [ ] **共享冷却生效**：抽 1 次同 entity 在实时 + 整点都达阈值的场景，
      Telegram 只收到 1 条告警（"[实时]" 在前 / 整点在前都可）
- [ ] **假阳性可控**：7 天内 "[实时]" 告警里，30 分钟内该 entity 后续仍
      持续上榜的占比 ≥ 60%；< 60% 说明 threshold 调小了
- [ ] **不影响主流程**：Telegram 不可达 / RealtimeAlertService 抛异常 →
      EntityExtractor 主链路不受影响，hotness 持续产出

### 反向验证

- [ ] **关闭实时**：`realtime_enabled=False` 重启后行为与 Phase 2.2 等价
- [ ] **不写 hotness_snapshots**：抽 _trigger_immediate 触发时段，
      `SELECT * FROM hotness_snapshots WHERE
      window_end NOT IN (':00', ':15', ':30', ':45')` 应为空
- [ ] **共享 dict 不被污染**：实时 + 整点都触发场景下
      `_alert_records[entity]` 应记录最后一次告警快照（不会被互相覆盖出旧值）

## 硬约束（沿用 + 本任务具体含义）

1. **零 LLM** —— RealtimeAlertService / hook 严格不 `import llm.ollama_client`。
   纯计数 + 纯阈值，零 LLM 是天然成立的
2. **不阻塞主流程** —— `notify()` 与 `_trigger_immediate()` 双重 try/except；
   任何异常 log.error 后吞掉，**绝不**让 EntityExtractor 已 commit 的批次回滚
   或 worker 线程崩溃
3. **不引入新依赖** —— 不上 Redis / Celery / RQ / APScheduler / asyncio。
   `_pending_count` 用整数变量，`shared_alert_records` 用 dict 引用，"实时"靠
   EntityExtractor 同步 hook 实现而不是后台线程
4. **不破坏向后兼容** —— 起点 135 落点 144（+9，0 回归）；
   `EntityExtractor.realtime_trigger=None` 默认值保证现有调用路径不变；
   `AlertTriggerService` 接口冻结只动决策函数抽取
5. **配置缺失即降级** —— 任一启动条件不满足时跳过 RealtimeAlertService 构造，
   `entity_extractor.realtime_trigger` 留 None，hook 走"短路分支"零开销

## 依赖与风险

### 依赖

- Phase 2.1：`SlidingCounter` 已支持 `'1h'` 窗口、`HotnessService` 稳定运行
- Phase 2.2：`AlertTriggerService` + `_alert_records` + `_decide_alert` +
  `TelegramClient`
- 不依赖任何新 pip 包 / 新外部服务 / schema 变更

### 风险

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| `_trigger_immediate` 同步执行慢，拖延 EntityExtractor | 中 | 中 | 实测耗时；< 2s 可接受、2~5s 警告、> 5s 必须切异步线程；详见 design §6 风险 1 |
| Telegram API 限流 | 低 | 低 | 30+5 双门槛 + 共享冷却把频率压极低；30 msg/s 限制远高于实际 |
| 共享 dict 跨服务破坏封装 | 中 | 低 | 单 worker 串行无并发；Phase 2.5 多通道时封装成 store 类（design §8.1） |
| 阈值过严 7 天 0 告警 | 中 | 中 | Success Metrics 强制 7 天 ≥ 3 次；不达标即调小 threshold/burst |
| 阈值过松刷屏 | 低 | 中 | 30+5+共享 60min 冷却三重保护；上线初期人工监督 |
| 进程重启冷却失效，连发 2 条 | 低 | 低 | 沿用 Phase 2.2 取舍：每实体多 1 条远比持久化复杂度划算 |
| 去 frozen 后字段被误改 | 低 | 中 | Code Review + 测试覆盖（用例 9）；本任务只动 `realtime_trigger` 一个字段 |

---

*文档版本：v1.0*
*基于：tasks.md v1.0 + design.md v1.0*
*预估工时：编码 4~6 小时（不含 spec 写作）*
*测试基线：135 → 144 passed（+9，0 回归）*
*端到端延迟：14~15 分钟 → 1~2 分钟*
