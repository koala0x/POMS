# Phase 2 · Task 2.1 多窗口热度排行榜 · Implementation Tasks

> 基于 requirements.md v1.0 + design.md v1.0 拆出的实施 checklist。

**执行约定**：
- 每完成一个 Task 跑测试，pass 数只增不减（与 Phase 1 / Phase 2.2 同款规则）
- 测试基线起点：**128 passed**（Phase 2.2 完工状态）
- 全部 Task 完成后落点：**135 passed**（+7，0 回归）
- 改动顺序严格按依赖：SlidingCounter → HotnessService → 兼容性回归 → 配置 → main.py → 端到端

---

## Task 0：基线验证

- [x] **0.1 跑 pytest 确认基线 128 passed**
  - `.venv/bin/python -m pytest tests/ --ignore=tests/test_ollama_client.py -q`
  - 必须看到 `128 passed`，否则先回滚到 Phase 2.2 干净状态
- [x] **0.2 git log 确认 Phase 2.2 已 commit**
  - `git log --oneline -5`
  - 工作树必须 clean（`git status` 无未提交改动），便于本任务回滚

_Requirements: 硬约束 §3（不破坏 128 passed 基线）_

## Task 1：SlidingCounter 加 6h 窗口

- [x] **1.1 改 `services/l2_sliding_counter.py`**
  - `WINDOWS_SECONDS` 字典在 `'1h'` 与 `'24h'` 之间插入一行 `"6h": 21600`
  - 不动 `add` / `count` / `active_entities` / `backfill_from_db` 实现
- [x] **1.2 加单测 `test_count_6h_window`**
  - `tests/test_l2_sliding_counter.py` 文件末尾追加
  - 步骤：`add(entity, ts=now-timedelta(hours=3))` → 断言
    `count('6h') == 1` 且 `count('1h') == 0` 且 `count('24h') == 1`
- [x] **1.3 跑测试**
  - `.venv/bin/python -m pytest tests/test_l2_sliding_counter.py -v`
  - 预期 9 + 1 = 10 passed
  - 全量：`pytest tests/ --ignore=tests/test_ollama_client.py -q` 应 **129 passed**

_Requirements: Req 1, 7.1_

## Task 2：HotnessService 参数化 + 校验

- [x] **2.1 改 `services/l2_hotness.py` 加字段 `window_type`**
  - dataclass 新增 `window_type: str = "1h"`（默认值保证现有调用 100% 兼容）
- [x] **2.2 实现 `__post_init__` 三道校验**
  - 校验 1：`window_type not in WINDOWS_SECONDS` → raise
  - 校验 2：`WINDOWS_SECONDS[window_type] // 3600 != short_hours` → raise
  - 校验 3：`baseline_days * 24 - short_hours <= 0` → raise
  - 错误消息含字段名 + 实际值，便于 main.py 兜底日志定位
- [x] **2.3 改 `_compute_records`**
  - 把 `count(entity, "1h")` 改成 `count(entity, self.window_type)`
  - 把 `active_entities("24h")` 改成
    `active_entities("7d" if self.window_type == "24h" else "24h")`
  - 验证 `upsert_batch` 调用已用 `window_type=self.window_type`（如已是字段引用就不动）
- [x] **2.4 加 5 个单测到 `tests/test_l2_hotness.py`**
  - `test_hotness_24h_baseline_days_lt_8_raises`
  - `test_hotness_window_type_unknown_raises`
  - `test_hotness_window_type_short_hours_mismatch_raises`
  - `test_hotness_6h_writes_window_type_6h`（集成：mock SlidingCounter，
    断言 `upsert_batch.call_args.kwargs['window_type']=='6h'` 且
    `sliding_counter.count` 被以 `(_, '6h')` 调用）
  - `test_hotness_24h_uses_7d_active_entities`（断言 24h 实例的 candidates
    来自 `active_entities("7d")`）
- [x] **2.5 跑测试**
  - `.venv/bin/python -m pytest tests/test_l2_hotness.py -v`
  - 预期 13 + 5 = 18 passed
  - 全量应 **134 passed**

_Requirements: Req 2, 7.2_

## Task 3：AlertTriggerService 兼容性回归

- [x] **3.1 加 `test_alert_trigger_ignores_6h_24h_records`**
  - `tests/test_l2_alert_trigger.py` 文件末尾追加
  - 用 SQLite + Mock TelegramClient（复用现有测试 fixture pattern）
  - 种 3 份榜：1h（BTC growth=25 应触发）、6h（ETH growth=999 应忽略）、
    24h（SOL growth=999 应忽略）
  - 断言 `telegram.send_text.call_count == 1` 且 call_args 文本含 `BTC` 不含 `ETH`/`SOL`
  - 不改 `services/l2_alert_trigger.py` 任何代码
- [x] **3.2 跑测试**
  - `.venv/bin/python -m pytest tests/test_l2_alert_trigger.py -v`
  - 预期 11 + 1 = 12 passed
  - 全量应 **135 passed**

_Requirements: Req 6, 7.3_

## Task 4：配置扩展

- [x] **4.1 改 `config/_new.py` 追加 12 个字段**
  - 6h 中期窗口组（前缀 `hotness_6h_`）：
    `enabled=True / top_k=20 / smoothing=5.0 / baseline_days=7 /
    min_baseline_count=200 / exclude_entities=("BTC","ETH","SOL","BNB","USDT","USDC","DAI")`
  - 24h 长期窗口组（前缀 `hotness_24h_`）：
    `enabled=True / top_k=20 / smoothing=10.0 / baseline_days=8 /
    min_baseline_count=500 / exclude_entities=("USDT","USDC","DAI")`
  - 字段顺序：先 6h 6 个 → 再 24h 6 个，与 design §3.4.1 一致
- [x] **4.2 验证 settings 加载**
  - `.venv/bin/python -c "from config.settings import get_settings; s=get_settings(); \
    print(s.hotness_6h_enabled, s.hotness_24h_baseline_days, s.hotness_6h_smoothing)"`
  - 预期：`True 8 5.0`
- [x] **4.3 跑测试**
  - 预期仍 **135 passed**（无新测试，配置变更不能破坏现有）

_Requirements: Req 3_

## Task 5：main.py 多实例化

- [x] **5.1 改 `main.py` Step 5c：单实例 → list[HotnessService]**
  - 1h 实例不在 try/except 内（必需，构造失败应阻塞启动）
  - 显式传 `window_type="1h"` 到 1h 实例（即便是默认值也写出来便于阅读）
  - `hotness_services: list[HotnessService] = [hotness_1h]` 作为起点
- [x] **5.2 6h / 24h 用 try/except ValueError 兜底**
  - 各自先 `if settings.hotness_6h_enabled` / `hotness_24h_enabled` 门控
  - try 内构造 + `hotness_services.append(svc)` + log.info 启动消息
  - except ValueError 内只 `log.error("HotnessService(6h) 构造失败已跳过：{}", e)`
  - else 分支 log.info 未启用消息
- [x] **5.3 sc_ok 循环注入到所有实例的 _counter_ready**
  - `for svc in hotness_services: svc._counter_ready = sc_ok`
  - 失败时 `log.warning(... f"{len(hotness_services)} 个实例首轮都会跳过")`
- [x] **5.4 new_services 列表更新**
  - `new_services = [normalizer_service, entity_extractor, *hotness_services]`
  - AlertTriggerService（如启用）跟在最后
- [x] **5.5 验证 main.py 仍能 import**
  - `.venv/bin/python -c "import main; print('ok')"`
- [x] **5.6 跑测试**
  - 预期仍 **135 passed**（main.py 改动不引入新测试）

_Requirements: Req 4, 5_

## Task 6：本地端到端验收 + 文档

> 这一步用真实 PG 跑 worker，验证三窗口都能写入。

- [x] **6.1 重启服务并核对启动日志**
  - `./scripts/restart.sh --bg`
  - 应按顺序看到 `HotnessService(1h)` / `(6h)` / `(24h)` 三行启动消息 +
    `AlertTriggerService 启动` 一行
- [x] **6.2 等下一个 quarter（最多 15 分钟），SQL 验证三种 window_type 都已写入**
  - `SELECT window_type, COUNT(*), MAX(window_end) FROM hotness_snapshots GROUP BY window_type;`
  - 预期：1h 与 6h 至少有数据；24h 因冷启动可空 8~12 小时
    （接受 `min_baseline_count=500` 保护）
- [x] **6.3 验证 AlertTriggerService 仍只对 1h 榜触发告警**
  - `tail -f logs/service.log | grep "alert sent"`
  - 应只看到 1h 榜的实体，不出现 6h/24h
- [x] **6.4 改 `docs/operations_guide.md` 第 6 节**
  - 加"多窗口调参表"：列出 1h / 6h / 24h 三组配置字段及推荐范围
  - 加"启动日志样例"：贴上 6.1 的 HotnessService 三行启动消息
- [x] **6.5 改 `docs/faq_design_decisions.md` 追加 Q7**
  - "为什么需要三个时间窗口？"——短中长信号互补、24h 不屏蔽 BTC/ETH 的理由、
    冷启动 8~12 小时可接受的原因（参考 design §3.8）

_Requirements: Success Metrics 阶段验收 + 业务验收_

## 执行顺序与依赖图

```
Task 0 (基线 128)
   └─► Task 1 (SlidingCounter +6h → 129)
           └─► Task 2 (HotnessService 参数化 → 134)
                   ├─► Task 3 (Alert 兼容性回归 → 135) ──┐
                   └─► Task 4 (配置扩展)              ──┴─► Task 5 (main.py 多实例)
                                                              └─► Task 6 (端到端 + 文档)
```

**可并行**：Task 3 与 Task 4（互不依赖）。
**必须串行**：Task 1 → 2 → {3, 4} → 5 → 6。

## 完工后状态

```
新增文件：无（不引入新文件）

修改文件：
  services/l2_sliding_counter.py        +1 行（WINDOWS_SECONDS 加 '6h'）
  services/l2_hotness.py                +window_type 字段 / +__post_init__ /
                                          +2 行 _compute_records
  config/_new.py                        +12 字段（hotness_6h_* × 6 + hotness_24h_* × 6）
  main.py                               Step 5c 单实例 → list[HotnessService]
  tests/test_l2_sliding_counter.py      +1 case
  tests/test_l2_hotness.py              +5 cases
  tests/test_l2_alert_trigger.py        +1 case
  docs/operations_guide.md              +多窗口调参表 + 启动日志样例
  docs/faq_design_decisions.md          +Q7

测试基线：128 → 135 passed（+7，0 回归）

不动文件（核心约束验证）：
  db/models.py / alembic/versions/      零 schema 变更
  services/l2_alert_trigger.py          Phase 2.2 接口冻结
  notifications/telegram_client.py / config/_alerts.py
  requirements.txt                       零新依赖
```

预估工时：Task 0 (5min) + Task 1 (15min) + Task 2 (45min) + Task 3 (10min) +
Task 4 (20min) + Task 5 (30min) + Task 6 (30min) ≈ **2.5~3 小时净 coding**

---

*文档版本：v1.0*
*基于：requirements.md v1.0 + design.md v1.0*
