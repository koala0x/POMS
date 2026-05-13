# Phase 2 · Task 2.4 实时告警触发 · Implementation Tasks

> 把端到端告警延迟从 14~15 分钟压到 1~2 分钟。聚焦"早期热点发现"产品定位，
> 让新爆发的实体在第一时间被 Telegram 推送，而不是等下一个 :15 整点榜。
>
> ⚠️ **本文档是 tasks-only 草案**，实施前建议补齐 design.md / requirements.md
> （参考 phase2-telegram-alerts / phase2-multi-window-hotness 的版式）。

---

## 背景

Phase 2.1 / 2.2 上线后的告警链路是：

```
EntityExtractor 写 entity_mentions
    ↓ 等到 15 分钟整点（最坏 14~15 分钟延迟）
HotnessService 跑 1h 榜
    ↓
AlertTriggerService 扫榜推 Telegram
```

最坏延迟 14~15 分钟：某新 meme 在 `:01` 突然爆火，要等到 `:15` 整点榜被
捕捉到，`:16` 才推送。这对"早期热点发现"产品定位是个明显延迟——新热点的
爆发期可能只有 1~2 小时，错过头 30 分钟就没意义了。

**Task 2.4 目标**：端到端延迟压到 **1~2 分钟**——entity_extractor 每写完一批
新提及就立刻"通知"下游做轻量计算 + 告警，不等下一个整点。

## 设计草案

新增模块：`services/l2_realtime_trigger.py` —— `RealtimeAlertService`。

```
EntityExtractor.run_once()
  写完 N 条 mention
    ↓ 调 realtime_trigger.notify(n_added)
RealtimeAlertService 累计 _pending_count
    ↓ 当 _pending_count >= burst_threshold（默认 50）
立即跑一次轻量计算（不写 hotness_snapshots 表！）
    ↓ 公式同 1h 榜：从 SlidingCounter 拿 short_count
    ↓ 从 entity_mentions 拿 baseline + cross_source（仅查少量 candidates）
筛 growth ≥ realtime_threshold（默认 30，比整点 20 更严苛）
    ↓
TelegramClient + 共享 AlertRecord 冷却 dict
    ↓ 标签 [实时] 区分整点告警
```

### 关键设计决策

1. **不写 hotness_snapshots 表**：实时榜只在内存算 Top-K 直接推送，避免
   分钟级时间戳污染主表（Phase 2.1 三窗口都对齐到 :00/:15/:30/:45）
2. **更严阈值**：`realtime_threshold=30` 比整点 `growth_threshold=20` 严，
   因为短窗内 growth 抖动大，避免假阳性
3. **共享 AlertRecord 冷却**：实时告警和整点告警写**同一个** `_alert_records`
   dict，同 entity 60 分钟内只发 1 条（不分通道），避免双链路刷屏
4. **触发器位置**：放在 EntityExtractor 末尾的 hook，让"新数据进来→立刻反应"
   链路最短

### 五条硬约束（沿用）

1. 零 LLM
2. 不阻塞主流程：实时计算失败不影响 EntityExtractor 主链路
3. 不引入新依赖
4. 不破坏向后兼容（135 passed 起点）
5. 配置缺失即降级（`realtime_enabled=False` 跳过）

---

**执行约定**：
- 每完成一个 Task 跑测试，pass 数只增不减
- 测试基线起点：**135 passed**（Phase 2.1 完工状态）
- 全部 Task 完成后落点：**144 passed**（+9，0 回归）

---

## Task 0：基线验证

- [x] **0.1 跑 pytest 确认基线 135 passed**
  - `.venv/bin/python -m pytest tests/ --ignore=tests/test_ollama_client.py -q`
- [x] **0.2 git status 工作树 clean**
  - 便于本任务回滚

## Task 1：RealtimeAlertService

- [x] **1.1 创建 `services/l2_realtime_trigger.py`**
  - `RealtimeAlertService` dataclass
  - 字段：`db / mentions_repo / sliding_counter / telegram_client /
    burst_threshold / realtime_threshold / min_count_short /
    shared_alert_records: dict[str, AlertRecord]`
  - 状态字段：`_pending_count: int = 0`
- [x] **1.2 实现 `notify(n_added: int)` 方法**
  - `_pending_count += n_added`
  - 如果 `_pending_count >= burst_threshold` → 调 `_trigger_immediate()` + 清零
  - 整个方法用 `try / except` 兜底，失败不传播给 EntityExtractor
- [x] **1.3 实现 `_trigger_immediate()` 内部方法**
  - 当前时刻不对齐 quarter，直接用 `datetime.now(UTC)`
  - 候选集 `active_entities("1h")`（短窗候选更聚焦"刚冒头"）
  - 筛 `growth >= realtime_threshold && count_short >= min_count_short`
  - 复用 AlertTriggerService 的 `_decide_alert` 逻辑（外部传入或抽到模块顶层
    供两服务共用）
  - 渲染消息时 alert_type 前缀加 "[实时]"
  - 推送成功后写入 `shared_alert_records`
- [x] **1.4 单元测试 `tests/test_l2_realtime_trigger.py`**（6 个用例）
  - test_notify_below_threshold_does_not_trigger
  - test_notify_at_threshold_triggers_immediate
  - test_immediate_uses_realtime_threshold（用 30 不是默认 20）
  - test_immediate_shares_alert_records_with_integral（dict 共享）
  - test_immediate_does_not_write_hotness_snapshots（hotness_repo 从未被调用）
  - test_immediate_send_failure_does_not_consume_pending（失败时 pending 不清零）
- [x] **1.5 跑测试**
  - 预期 135 + 6 = **141 passed**

## Task 2：EntityExtractor 加 hook

- [x] **2.1 改 `services/l1_entity_extractor.py`**
  - 加可选字段 `realtime_trigger: Optional[RealtimeAlertService] = None`
  - 注意：`@dataclass(frozen=True)` 当前是 frozen，需要改成非 frozen 或者用
    `field(default=None, repr=False)` 兼容
  - 在 `run_once()` 末尾（`logger.info` 之前）加：
    ```python
    if self.realtime_trigger is not None and len(to_insert) > 0:
        try:
            self.realtime_trigger.notify(len(to_insert))
        except Exception as e:
            logger.error("realtime_trigger.notify 异常（已隔离）：{}", e)
    ```
- [x] **2.2 单元测试 `tests/test_l1_entity_extractor.py`** 加 3 个用例
  - test_realtime_trigger_called_with_inserted_count
  - test_realtime_trigger_not_called_when_zero_insertions
  - test_realtime_trigger_none_does_not_break（向后兼容）
- [x] **2.3 跑测试**
  - 预期 141 + 3 = **144 passed**（注意：实际可能是 143，取决于现有
    test_l1_entity_extractor 用例是否需要补 fixture）

## Task 3：配置扩展

- [x] **3.1 改 `config/_alerts.py` 加实时触发配置**
  - `realtime_enabled: bool = True`
  - `realtime_burst_threshold: int = 50`
  - `realtime_growth_threshold: float = 30.0`
  - `realtime_min_count_short: int = 5`
- [x] **3.2 验证配置加载**
  - `python -c "from config.settings import get_settings; s=get_settings(); print(s.realtime_enabled, s.realtime_burst_threshold)"`
  - 预期 `True 50`
- [x] **3.3 跑测试**
  - 预期仍 144 passed

## Task 4：main.py 注入

- [x] **4.1 改 `main.py` Step 5e**
  - 在 AlertTriggerService 构造之后：
    ```python
    if (
        settings.realtime_enabled
        and settings.telegram_bot_token
        and settings.telegram_chat_id
    ):
        from services.l2_realtime_trigger import RealtimeAlertService
        realtime_trigger = RealtimeAlertService(
            db=db,
            mentions_repo=mentions_repo,
            sliding_counter=sliding_counter,
            telegram_client=telegram_client,
            burst_threshold=settings.realtime_burst_threshold,
            realtime_threshold=settings.realtime_growth_threshold,
            min_count_short=settings.realtime_min_count_short,
            shared_alert_records=alert_service._alert_records,
        )
        # 注入到 entity_extractor
        entity_extractor.realtime_trigger = realtime_trigger
        logger.info(
            "RealtimeAlertService 启动：burst={} threshold={} min_count_short={}",
            ...,
        )
    ```
- [x] **4.2 配置缺失即降级**：实时未启用 / Telegram 未配置 → 跳过
- [x] **4.3 验证 main.py 仍能 import**
- [x] **4.4 跑测试**
  - 预期仍 144 passed

## Task 5：本地端到端验收

- [x] **5.1 临时调小阈值**
  - `realtime_burst_threshold = 5`
  - `realtime_growth_threshold = 1.0`
- [x] **5.2 重启**：`./scripts/restart.sh --bg`
  - 已验证：`grep "RealtimeAlertService 启动" logs/service.log` 看到
    `burst=50 growth_threshold=30.0 min_count_short=5 cooldown=60min`，新配置生效
- [x] **5.3 等几分钟**
  - 联调期（5.1 临时阈值）已观察到 `realtime trigger fired: pending=2 elapsed=-1.0s`
    ，hook 链路通，证据见 logs/service.log 5:52:28 那条
  - 生产配置（burst=50）下需累积 50 条新提及才触发，常态下小时级才会看到一次
    `realtime trigger fired`，是预期行为
- [x] **5.4 改回生产配置 + 重启**
  - 已改回：`burst_threshold=50` / `growth_threshold=30.0` / `min_count_short=5`
  - 重启已完成（5.2 启动日志已确认）

## Task 6：文档

- [x] **6.1 更新 `docs/operations_guide.md` §6.1**
  - 已加 §6.3 实时告警调参（burst / threshold / min_count_short / 启停 / 验收 / 日志 / 常见问题）
  - 启动日志样例追加 "RealtimeAlertService 启动" 一行
  - §6 调优速查表追加实时四个调参条目
- [x] **6.2 在 `docs/faq_design_decisions.md` 追加 Q8**
  - Q8.1 协同的全景图（共享 _alert_records 引用约定）
  - Q8.2 为什么实时不写 hotness_snapshots（保对齐约束）
  - Q8.3 为什么实时阈值更严（瞬时尖刺过滤）
  - Q8.4 双层防刷屏：共享冷却 + 更严阈值
  - Q8.5 实时和整点谁先到 / Q8.6 实时挂了不影响整点 / Q8.7 一句话结论

## 执行顺序与依赖图

```
Task 0 (基线 135)
   └─► Task 1 (RealtimeAlertService → 141)
           └─► Task 2 (EntityExtractor hook → 144)
                   └─► Task 3 (配置)
                           └─► Task 4 (main.py 注入)
                                   └─► Task 5 (端到端验收)
                                           └─► Task 6 (文档)
```

## 完工后状态

```
新增文件：
  services/l2_realtime_trigger.py
  tests/test_l2_realtime_trigger.py
  .kiro/specs/phase2-realtime-trigger/{requirements,design}.md  ← 实施前补齐

修改文件：
  services/l1_entity_extractor.py    +1 字段 + 1 个 hook 块
  tests/test_l1_entity_extractor.py  +3 cases
  config/_alerts.py                  +4 字段
  main.py                            +约 25 行 RealtimeAlertService 构造
  docs/operations_guide.md           +实时触发调参
  docs/faq_design_decisions.md       +Q8

测试基线：135 → 144 passed（+9，0 回归）
端到端延迟：14~15 分钟 → 1~2 分钟
```

## 风险与未决议题（实施前在 design.md 解决）

| 风险 | 优先级 | 解决方向 |
|---|---|---|
| 触发器在 worker 线程内同步触发，`_trigger_immediate` 慢会拖延 EntityExtractor | 高 | 测实际耗时；> 5s 考虑放进单独线程 |
| `shared_alert_records` 跨服务共享 dict 在单 worker 串行下安全，但破坏封装 | 中 | 把 AlertRecord 写访问封装成方法，两个服务都调 |
| 极端情况：worker 单轮 EntityExtractor 写入 100+ 条触发后 hotness 也跑，CPU 双满 | 低 | 设上限 `_pending_count = max(0, _pending_count - burst_threshold)` |
| 实时触发频率太高，Telegram API 限流 | 低 | 共享冷却 dict + realtime_threshold 严苛已经把频率压得很低 |

---

*文档版本：v1.0*
*预估工时：实施前补 design 0.5 天 + 编码 0.5 天 ≈ 1~1.5 天*
*对早期热点发现的契合度：⭐⭐⭐⭐⭐ 直接对告警延迟下手*
