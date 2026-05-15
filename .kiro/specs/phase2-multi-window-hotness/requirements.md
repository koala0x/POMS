# Phase 2 · Task 2.1 多窗口热度排行榜 · Requirements

> Phase 2 的第二个子任务（紧跟 Telegram 告警上线）。在已经稳定的 `1h` 热度榜
> 基础上把 HotnessService 扩展为 `1h / 6h / 24h` 三档窗口，给 AlertTriggerService
> 等下游消费者提供"短中长"三档信号；本任务**只铺接口**，不动告警通道本身。

## 背景

Phase 1 起，`HotnessService` 把 `short_hours=1` 与 `window_type='1h'` 都硬编码在
`_compute_records` 里，每 15 分钟产出一份 1h 热度榜。Phase 2.2 上线 Telegram 告警
后，`AlertTriggerService` 也只读 1h 榜——这意味着：

- 1h 维度噪音大，靠它一个窗口判断"是否值得告警"误报率高；
- 真正的"持续升温 / 中期叙事"信号（6 小时尺度）没办法捕捉；
- 宏观新闻级别的事件（24 小时尺度）更是完全感知不到。

`hotness_snapshots` 表的 UNIQUE 约束已经是 `(window_end, window_type, entity)`，
天然支持多窗口共存；`SlidingCounter` 已经支持 4 档窗口（差一个 `'6h'`）。
本任务只做"参数化 + 多实例化 + 配置扩展"三件事，不动 schema、不动告警代码。

## 用户角色

- **唯一用户**：项目所有者（你）
- **设备**：本地 Mac mini（24/7 跑 worker）+ Telegram App
- **使用场景**：希望未来打开"6h 中期告警 / 24h 宏观告警"通道时，不用回头改
  HotnessService；本任务先把基础设施铺好，告警通道留给 Phase 2.2.1

## 边界与非目标

### 包含

1. `SlidingCounter.WINDOWS_SECONDS` 加 `'6h': 21600`，保持现有 4 档不动
2. `HotnessService` 加 `window_type: str` 字段（默认 `'1h'`，向后兼容）
3. `HotnessService.__post_init__` 三道构造期校验
4. `_compute_records` 里 `count()` 用 `self.window_type`、24h 实例的 candidates
   走 `active_entities("7d")`
5. `NewPipelineSettings` 扩展 12 个新字段（`hotness_6h_*` × 6 + `hotness_24h_*` × 6）
6. `main.py` Step 5c 由"单实例"改为"list[HotnessService]"，6h/24h 用 try/except 兜底
7. 三个 HotnessService 实例都走同一套 `align_to_quarter`，每 15 分钟刷新
8. 9 个新增测试用例（详见 Req 7）+ 33 个相关测试零回归

### 不包含（留 Phase 2.2.1 / Phase 3）

1. ❌ `AlertTriggerService` 参数化 `window_type` —— 留给 Phase 2.2.1
2. ❌ 三窗口共振信号检测（同一 entity 在 1h/6h/24h 同时上榜）—— Phase 2.2.2
3. ❌ `hotness_snapshots` 表分区或定期清理 —— Phase 3
4. ❌ schema 变更 / 新增 alembic 迁移 —— 完全不需要
5. ❌ 新增依赖库 —— 全部用现有标准库 + 已有 repo 接口
6. ❌ 新链路开关或 feature flag —— 通过 `hotness_*_enabled` 字段控制

## Requirements

### Req 1：SlidingCounter 加 6h 窗口键

1.1 应修改 `services/l2_sliding_counter.py` 的模块级常量 `WINDOWS_SECONDS`，
    在 `'1h'` 和 `'24h'` 之间插入 `'6h': 21600`
1.2 不得改动 `add` / `count` / `active_entities` / `backfill_from_db` 任何方法
    实现——这些方法都依赖 `WINDOWS_SECONDS` 的迭代，加键即可自动覆盖
1.3 `count(entity, '6h')` / `active_entities('6h')` 应正常返回结果，不得 raise
1.4 内存代价：每个 entity 多一份 `'6h'` deque，估算多消耗 < 5MB
    （Phase 1 实测活跃 entity ~1000，6h deque 平均 < 24h deque 的 25%）
1.5 现有 9 个 SlidingCounter 单测必须 100% pass（不改测试代码）

### Req 2：HotnessService 参数化 window_type

2.1 应给 `HotnessService` dataclass 添加新字段
    `window_type: str = "1h"`（默认值保证向后兼容）
2.2 应实现 `__post_init__(self) -> None`，包含三道校验，**任一**失败即
    `raise ValueError`，错误消息含违规字段名 + 实际值：

    | # | 校验 | 触发条件 |
    |---|---|---|
    | 1 | `window_type` 合法 | `self.window_type not in WINDOWS_SECONDS` |
    | 2 | `short_hours` 与 `window_type` 自洽 | `WINDOWS_SECONDS[window_type] // 3600 != short_hours` |
    | 3 | baseline 数学约束 | `baseline_days * 24 - short_hours <= 0` |

2.3 应改 `_compute_records` 的两行：
    - `short_count = self.sliding_counter.count(entity, self.window_type)`
      （原来硬编码 `"1h"`）
    - `candidate_window = "7d" if self.window_type == "24h" else "24h"`
      然后 `candidates = self.sliding_counter.active_entities(candidate_window)`
2.4 `upsert_batch` 调用必须传 `window_type=self.window_type`（已有逻辑，
    确保从硬编码 `"1h"` 改为读字段）
2.5 `align_to_quarter` 不变——三个实例共用同一对齐函数
2.6 `_last_window_end` 与 `_counter_ready` 是 dataclass 字段，**每实例独立**

### Req 3：多窗口配置（NewPipelineSettings 扩展）

3.1 应在 `config/_new.py` 的 `NewPipelineSettings` 末尾追加 **12 个字段**，
    分两组 6+6：

    **6h 中期窗口组（前缀 `hotness_6h_`）**：
    | 字段 | 类型 | 默认值 |
    |---|---|---|
    | `hotness_6h_enabled` | `bool` | `True` |
    | `hotness_6h_top_k` | `int` | `20` |
    | `hotness_6h_smoothing` | `float` | `5.0` |
    | `hotness_6h_baseline_days` | `int` | `7` |
    | `hotness_6h_min_baseline_count` | `int` | `200` |
    | `hotness_6h_exclude_entities` | `tuple[str, ...]` | `("BTC","ETH","SOL","BNB","USDT","USDC","DAI")` |

    **24h 长期窗口组（前缀 `hotness_24h_`）**：
    | 字段 | 类型 | 默认值 |
    |---|---|---|
    | `hotness_24h_enabled` | `bool` | `True` |
    | `hotness_24h_top_k` | `int` | `20` |
    | `hotness_24h_smoothing` | `float` | `10.0` |
    | `hotness_24h_baseline_days` | `int` | `8` |
    | `hotness_24h_min_baseline_count` | `int` | `500` |
    | `hotness_24h_exclude_entities` | `tuple[str, ...]` | `("USDT","USDC","DAI")` |

3.2 现有 6 个 `hotness_*` 字段（无前缀）一律不动，作为 1h 窗口的配置；
    新字段仅扩展不替换，避免 main.py 与现有测试受影响
3.3 `hotness_24h_baseline_days` 默认 8 是数学硬约束（`8*24-24=168`），不是 7；
    设小于 8 时 `__post_init__` 会 raise，由 main.py 的 try/except 兜底降级
3.4 `hotness_24h_exclude_entities` 默认**不屏蔽** BTC/ETH——24h 维度的 BTC
    大新闻（破新高 / 跌破支撑）是真信号，仅屏蔽稳定币 USDT/USDC/DAI
3.5 字段加载验证：
    `python -c "from config.settings import get_settings; s=get_settings(); \
    print(s.hotness_6h_enabled, s.hotness_24h_baseline_days, s.hotness_6h_smoothing)"`
    应输出 `True 8 5.0`

### Req 4：main.py 多实例构造

4.1 应改 `main.py` Step 5c：把单个 `hotness_service` 改为 `hotness_services: list[HotnessService]`
4.2 1h 实例**必需构造**（不在 try/except 内），构造失败应阻塞启动——这是
    系统的核心窗口，缺它等同于 Phase 1 倒退
4.3 6h / 24h 实例**可降级**：`if settings.hotness_6h_enabled` / `if settings.hotness_24h_enabled`
    门控，构造代码用 `try / except ValueError` 包裹；构造失败时 `log.error` 但不
    阻塞启动，1h 继续工作
4.4 `sc_ok` 状态注入：
    ```python
    for svc in hotness_services:
        svc._counter_ready = sc_ok
    ```
4.5 `new_services = [normalizer_service, entity_extractor, *hotness_services]`，
    AlertTriggerService（如启用）跟在所有 HotnessService 之后，保证
    1h 榜先写入再扫
4.6 三个实例共享**同一个** `sliding_counter` / `mentions_repo` / `hotness_repo`
    引用，关键不变量

### Req 5：调度对齐策略（每 15 分钟刷新所有窗口）

5.1 三个 HotnessService 实例都使用现有的 `align_to_quarter(now)` 算 `window_end`
5.2 worker 一轮触发即调用 `[1h, 6h, 24h]` 三个实例的 `run_once`，**串行**调度
    （与 Phase 1 单线程模型一致，无并发竞争）
5.3 单一实例抛异常被 `Jobs._worker_loop` 已有的异常隔离机制兜底，不影响其它实例
5.4 滚动窗口语义：window_end 是滑动的，不是"按自然时段分块"——所有窗口都是
    "前 N 小时"的滚动统计，每 15 分钟前进一格
5.5 DB 体积估算：3 窗口 × 96 次/天 × 20 行 × ~100B ≈ 11 MB/天，年 < 4 GB，
    远低于 PG 实例容量；定期清理留 Phase 3

### Req 6：与 AlertTriggerService 向前兼容

6.1 不得修改 `services/l2_alert_trigger.py` 的任何代码（Phase 2.2 接口冻结）
6.2 AlertTriggerService 当前显式 `fetch_latest_window_end(session, "1h")`，
    新窗口（6h / 24h）的写入对它**完全透明**——它读不到也不会读到
6.3 必须新增一个回归测试 `test_alert_trigger_ignores_6h_24h_records`：
    种 1h+6h+24h 三份榜，跑 AlertTriggerService.run_once，断言只对 1h 榜里的
    entity 调 `send_text`
6.4 Phase 2.2 已有的 11 个 alert_trigger 测试必须 100% pass（不改测试代码）

### Req 7：测试覆盖（净新增 7 个用例）

7.1 `tests/test_l2_sliding_counter.py` 新增 1 个：
    - `test_count_6h_window`：`add(ts=now-3h)` 后 `count('6h')==1` 且 `count('1h')==0`

7.2 `tests/test_l2_hotness.py` 新增 5 个（4 单元 + 1 集成 + 1 集成 = 实际 6
    个名字，其中 `test_hotness_window_type_field_default_is_1h` 与现有
    "默认构造"测试合并，净新增 5 个）：
    - `test_hotness_24h_baseline_days_lt_8_raises`（单元）
    - `test_hotness_window_type_unknown_raises`（单元）
    - `test_hotness_window_type_short_hours_mismatch_raises`（单元）
    - `test_hotness_6h_writes_window_type_6h`（集成）
    - `test_hotness_24h_uses_7d_active_entities`（集成）

7.3 `tests/test_l2_alert_trigger.py` 新增 1 个：
    - `test_alert_trigger_ignores_6h_24h_records`（向前兼容回归：种 1h+6h+24h
      三份榜，断言只对 1h 实体调 `send_text`）

7.4 现有 33 个相关用例（test_l2_hotness 13 + test_l2_sliding_counter 9 +
    test_l2_alert_trigger 11）保持 100% pass，不允许改测试代码

7.5 不允许真的连接 Telegram API；不允许真的连接 PostgreSQL（用 SQLite 内存库
    + monkeypatch datetime + Mock SlidingCounter）

### Req 8：日志规范

8.1 启动日志样例（按出现顺序）：

    ```
    SlidingCounter backfill 完成：耗时 X.Xs，回填 N 条
    HotnessService(1h) 启动：top_k=20 smoothing=2.0 baseline_days=7 ...
    HotnessService(6h) 启动：top_k=20 smoothing=5.0 baseline_days=7 ...
    HotnessService(24h) 启动：top_k=20 smoothing=10.0 baseline_days=8 ...
    AlertTriggerService 启动：growth_threshold=20.0 cooldown=60min ...
    summary worker 启动：level1=0 level2=0 new=5 空闲 sleep Xs
    ```

8.2 6h/24h 实例构造失败的降级日志：
    `log.error("HotnessService(6h) 构造失败已跳过：{}", e)`
8.3 6h/24h 被关闭的禁用日志：
    `log.info("HotnessService(6h) 未启用（hotness_6h_enabled=False）")`
8.4 现有 INFO 日志（"hotness 跳过：counter 未就绪" / "hotness 写入 N 条"）
    格式不变，但应在消息里能识别出是哪个 window_type 的实例（建议前缀 `[1h]` / `[6h]` / `[24h]`）

## Success Metrics

### 阶段验收（部署后 24 小时内）

- [ ] **三窗口都启动**：日志含 `HotnessService(1h)` / `(6h)` / `(24h)` 三行启动消息
- [ ] **测试基线**：`pytest` 100% pass，128 → 135（+7，0 回归）
- [ ] **DB 验证**：`SELECT window_type, COUNT(*) FROM hotness_snapshots GROUP BY window_type`
      至少看到 `1h` 与 `6h` 两种；`24h` 因冷启动可空 8~12 小时
- [ ] **配置生效**：6h smoothing=5.0、24h smoothing=10.0、24h baseline_days=8
      与 settings 默认值一致

### 业务验收（部署后 7 天内）

- [ ] **24h 榜冷启动达标**：48 小时后 `24h` 窗口稳定输出 Top-20 排行
- [ ] **跨窗口对比有意义**：抽 3~5 个热点 entity，3 个窗口的 growth_rate 呈现
      "1h > 6h > 24h" 自然衰减（信号短窗最尖锐、长窗最稳健）
- [ ] **AlertTrigger 行为不变**：Phase 2.2 配置的 1h 告警频率与质量与本任务上线前一致
- [ ] **零运行时回归**：日志无新的 ERROR / WARN 堆积

### 反向验证（确认没引入新风险）

- [ ] **零 LLM 验证**：`pytest tests/test_phase1_pipeline.py -v` 仍然
      `mock_chat.call_count == 0`
- [ ] **关闭 6h/24h 验证**：把 `hotness_6h_enabled / hotness_24h_enabled` 设 False，
      重启后只写 1h，行为与 Phase 2.2 完全等价
- [ ] **数学约束兜底**：把 `hotness_24h_baseline_days` 改成 5，重启日志应有
      `HotnessService(24h) 构造失败已跳过：baseline_days=5 * 24 = 120 必须 > short_hours=24`，
      且 1h/6h 正常工作

## 硬约束（不可妥协）

1. **零 LLM**：HotnessService / SlidingCounter / 新配置代码严格不 import `llm/ollama_client`
2. **零 schema 变更**：不动 `db/models.py` / `alembic/versions/`，UNIQUE 约束已足够
3. **不破坏老链路兼容**：现有 128 个测试必须 100% pass；`window_type='1h'` 的语义、
   字段、排序、final_score 公式与 Phase 1 完全等价
4. **1h 必需，6h/24h 可降级**：1h 实例构造失败应阻塞启动；6h/24h 通过
   `try/except ValueError` 兜底，构造失败只 log.error 不阻塞
5. **配置缺失即降级**：`hotness_6h_enabled=False` 或 `hotness_24h_enabled=False`
   时跳过该实例构造，等同于 Phase 2.2 行为
6. **24h 边界数学约束（新加）**：`baseline_days * 24 - short_hours > 0` 必须在
   `__post_init__` 强制；24h 窗口最小合法 `baseline_days=8`，否则除零

## 依赖与风险

### 依赖

- Phase 1 已交付：`hotness_snapshots` schema、`SlidingCounter` 4 档窗口、
  `align_to_quarter` 对齐函数、`HotnessSnapshotsRepo.upsert_batch(window_type=...)` 已支持
- Phase 2.2 已交付：`AlertTriggerService` 显式 `window_type="1h"`、
  Telegram 通道已联通
- 不依赖任何新 pip 包、任何新外部服务

### 风险

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| 24h 冷启动期 baseline 数据不足，首日榜空 | 高 | 中：24h 榜首日为空 | `min_baseline_count=500` 保护 + 已有 INFO 跳过日志；运行 24~48h 后自然达标 |
| 用户误把 `hotness_24h_baseline_days` 设为 5 触发除零 | 中 | 低：24h 实例构造失败 | `__post_init__` raise + main.py try/except 兜底，1h/6h 不受影响 |
| 三实例并发扫 SlidingCounter 数据错乱 | 极低 | 高 | 单 worker 线程串行调度（与 Phase 1 一致）；将来若拆多线程需加锁 |
| `hotness_snapshots` 表体积长期累积（11 MB/天） | 低 | 低 | Phase 3 加 cron 清理 30 天前数据；本任务不处理 |
| AlertTriggerService 未来误读 6h/24h 记录 | 低 | 中：误告警 | Phase 2.2 已锁 `window_type="1h"`；新增测试 `test_alert_trigger_ignores_6h_24h_records` 回归保护 |
| 6h/24h 写入频率高带来 PG IO 抖动 | 低 | 低 | 每 15 分钟 3 次 UPSERT × 20 行 = 60 行/15min，远低于 PG 单连接吞吐量 |

---

*文档版本：v1.0*
*基于：design.md v1.0*
*预估工时：3~4 小时净 coding*
*测试基线：128 → 135 passed（+7，0 回归）*
