# Phase 2 · Task 2.5 L3 实体共现网络 · Requirements

> Phase 2 的第三个子任务（紧跟 Telegram 告警 / 多窗口热度榜）。在已经稳定的
> `entity_mentions` 之上做"实体两两共现"统计，让系统自动发现"新叙事萌芽"——
> 不需要等用户先说出 narrative 名字，几个 token 同期被讨论就直接捕获。
> 本任务**只产出 `entity_cooccurrence` 表**，不接 Telegram，告警通道留给 Phase 2.5.1。

## 背景

Phase 2.1 / 2.2 上线后，单实体的"突然热"已经能在 Top-K 榜里看到。但现实里
**真正值钱的早期信号**长这样：

- `EIGEN` 单看不算激增（baseline 本来就有）
- `ETHFI` 单看也不算激增
- `REZ` 单看更不算激增
- 但**这三个 token 同一天突然被一起讨论**——这是 restaking 叙事复苏的明确信号

单实体榜在"叙事级共振"面前是看不出来的。用户也不会直接说"restaking 起飞"
（自然语言里赛道名出现频率远低于 token 名），所以 narrative 词典命中率天然有限。

L3 共现网络解决这个问题：

- 在 24h 窗口内对所有 `entity_mentions` 做两两共现统计
- 用 **PMI（Pointwise Mutual Information）** 衡量"是不是不寻常的一起出现"
- PMI 高 + 短窗共现 ≥ 3 + 7 天基线共现为 0 → 这对实体"突然成对"
- 多个突然成对的 entity 自然形成簇，就是叙事候选

`hotness_snapshots` 还是看"谁在变热"；`entity_cooccurrence` 看"谁正在跟谁一起变热"。
两者互补，一个从节点角度看，一个从边角度看。

## 用户角色

- **唯一用户**：项目所有者（你）—— 单人独立开发者，做加密货币早期热点发现
- **关心的信号**：在 narrative 还没在媒体上被反复提及前，就从社区讨论的
  实体共现拓扑里发现新叙事候选；比单个 entity 上榜更有价值
- **使用场景**：每天瞄一两次 `scripts/check_status.py` 输出，看哪些"突然成对"
  的实体值得深挖；高 PMI 的 pair 列表是直接的研究 backlog

## 边界与非目标

### 包含

1. 一张 `entity_cooccurrence` 表 + alembic 迁移 `002_phase2_cooccurrence.py`
2. ORM 模型 + `CooccurrenceRepo`（与 `HotnessSnapshotsRepo` 同款 UPSERT 接口）
3. `services/l3_cooccurrence.py`：`CooccurrenceService` 每 15 分钟扫一次
4. PMI 公式 + smoothing 设计（避免低频实体除零）
5. `is_new_pair` 检测（baseline 共现 0 + 短窗 ≥ 3）
6. 6 个新配置字段（`cooccur_*` 前缀）
7. `main.py` 注入 + try/except 兜底（构造失败不阻塞启动）
8. 12 个测试用例（10 + 2，对应 Task 2.4 + 3.2）

### 不包含（留 Phase 2.5.1 / Phase 3）

1. ❌ **共现告警 Telegram 通道**——本任务只产出表，避免和 `AlertTriggerService`
   冲突；Phase 2.5.1 单独再做共现告警，复用 `TelegramClient`
2. ❌ 图算法（PageRank / community detection）—— 用 PMI 单边筛选已够发现叙事候选
3. ❌ 跨窗口共现共振检测（同一对在 1h/6h/24h 都高 PMI）—— Phase 2.5.2
4. ❌ 共现网络可视化（D3 / Streamlit）—— Phase 3
5. ❌ `entity_cooccurrence` 表分区或定期清理 —— Phase 3
6. ❌ 新增 pip 依赖 —— 全部用现有 numpy / sqlalchemy / Python 标准库

## Requirements

### Req 1：数据模型 `entity_cooccurrence` 表

1.1 应新建 alembic 迁移 `alembic/versions/002_phase2_cooccurrence.py`，创建
    `entity_cooccurrence` 表，字段如下：

    | 字段 | 类型 | 约束 |
    |---|---|---|
    | `id` | `BIGSERIAL` | 主键 |
    | `entity_a` | `VARCHAR(128)` | NOT NULL，字典序保证 a < b |
    | `entity_b` | `VARCHAR(128)` | NOT NULL，字典序保证 a < b |
    | `window_end` | `TIMESTAMPTZ` | NOT NULL，align 到 :00/:15/:30/:45 |
    | `window_type` | `VARCHAR(16)` | NOT NULL，默认 `'24h'` |
    | `cooccur_count` | `INTEGER` | NOT NULL，短窗内共现消息数 |
    | `pmi` | `FLOAT` | 可空，PMI 计算结果（含 smoothing） |
    | `is_new_pair` | `BOOLEAN` | NOT NULL，默认 FALSE |
    | `created_at` | `TIMESTAMPTZ` | NOT NULL，默认 NOW() |

1.2 唯一约束：`UNIQUE(entity_a, entity_b, window_end, window_type)`，
    名 `uq_cooccur_pair_window`，幂等写入靠 ON CONFLICT 走 UPSERT
1.3 索引 `idx_cooccur_window_pmi`：`(window_end, window_type, pmi DESC)`，
    支持"最新窗口的 Top-K PMI 对"查询，给 `scripts/check_status.py` 用
1.4 索引 `idx_cooccur_entity_a`：`(entity_a, window_end DESC)`，
    支持"某实体的邻居"查询（`fetch_neighbors`）
1.5 应在 `db/models.py` 新增 `EntityCooccurrence(Base)` ORM 类，字段对齐迁移
1.6 应在 `db/repositories/cooccurrence_repo.py` 实现 `CooccurrenceRepo`：
    - `upsert_batch(session, *, window_end, window_type, pairs)` —— 批量 UPSERT
    - `fetch_top_k_pairs(session, *, window_end, window_type, k=100)`
    - `fetch_neighbors(session, *, entity, window_end, k=10)` —— 给某 entity
      找 PMI 最高的 k 个邻居（`entity_a == entity OR entity_b == entity`）
1.7 不得对现有 8 张表做任何 schema 变更（零 schema 改动外溢）

### Req 2：CooccurrenceService 接口

2.1 应实现 `services/l3_cooccurrence.py` 的 `CooccurrenceService` dataclass，
    构造参数：

    | 字段 | 类型 | 默认值 | 来源 |
    |---|---|---|---|
    | `db` | `Database` | — | main.py 注入 |
    | `mentions_repo` | `EntityMentionsRepo` | — | 与 HotnessService 共享 |
    | `cooccur_repo` | `CooccurrenceRepo` | — | 本任务新增 |
    | `sliding_counter` | `SlidingCounter` | — | 与 EntityExtractor 共享同一实例 |
    | `window_type` | `str` | `'24h'` | 共现需要长窗才稳定 |
    | `top_pairs` | `int` | `100` | 每窗口写 Top-100 pair |
    | `min_cooccur_count` | `int` | `3` | 短窗共现 ≥ 3 才考虑 |
    | `min_pmi` | `float` | `1.0` | PMI < 1 视为噪音不写库 |
    | `min_window_msgs` | `int` | `50` | 窗口内不同消息数 < 50 直接跳过 |
    | `timezone` | `ZoneInfo` | UTC | 与 HotnessService 同步 |

2.2 应实现 `__post_init__` 校验：
    - `window_type ∈ {'1h', '6h', '24h'}`，否则 raise `ValueError`
    - `min_cooccur_count >= 1` 且 `top_pairs >= 1`，否则 raise `ValueError`
2.3 应实现 `run_once(self) -> bool`：
    - 用 `align_to_quarter(now)` 计算当前 `window_end`
    - 与 `_last_window_end` 比较，相同则返回 False（不重复处理）
    - 数据稀疏时（窗口内不同消息数 < `min_window_msgs`）跳过 + INFO 日志
    - 调 `_compute_pairs(window_end)` 得到候选对列表
    - 过滤 `cooccur_count >= min_cooccur_count` 且 `pmi >= min_pmi`
    - 对每对调 `_is_new_pair(...)` 计算 `is_new_pair` 标记
    - 取 Top-100（按 PMI 降序）调 `cooccur_repo.upsert_batch`
    - UPSERT 失败 → `session.rollback()` + 不更新 `_last_window_end`，下一轮重试
    - 任意一对成功写入返回 True；空数据 / 全失败返回 False
2.4 应实现 `_compute_pairs(window_end) -> list[dict]`：
    - 候选实体集 = `sliding_counter.active_entities('24h')`，避免 n² 爆炸
    - 从 `mentions_repo` 拉短窗内的 `(msg_id, entity)` 列表
    - 按 `msg_id` 分组，对每条消息内的实体走 `itertools.combinations(sorted, 2)`
      生成 pair（保证 `entity_a < entity_b` 字典序）
    - 累计每对的 `cooccur_count`
    - 同时聚合每个 entity 的独立短窗 count（PMI 分母）和窗口内 N（消息总数）
    - 对每对计算 PMI（公式与 smoothing 见 design.md §3.3）
2.5 应实现 `_is_new_pair(entity_a, entity_b, window_end, short_start) -> bool`：
    - 查 7 天基线期 `[window_end - 7d, short_start)` 这对的共现次数
    - baseline_count == 0 且当前 cooccur_count >= 3 → True
    - 否则 False
2.6 单实体共现失败（DB 异常）不拖整批，log warning 后跳过该对

### Req 3：PMI 公式与 is_new_pair 检测

3.1 PMI 公式（含 smoothing）：

    ```
    PMI(a, b) = log( (cooccur + 1) * N / ((count_a + K) * (count_b + K)) )
    ```

    其中：
    - `cooccur` = 短窗内 a 和 b 共现的消息数
    - `count_a` / `count_b` = 短窗内 a / b 各自的提及消息数
    - `N` = 短窗内不同消息总数（带至少一个实体的消息）
    - `K` = 候选实体数（active_entities 的长度），smoothing 因子
    - 分子 `+1`、分母 `+K`：避免 `count_a` 或 `count_b` 极小时 PMI 不稳定/除零

3.2 PMI 计算用 `numpy.log`（自然对数）。numpy 已被 sqlalchemy 间接引入，
    不算新依赖
3.3 `is_new_pair` 判定逻辑（Req 2.5）：必须**两条同时满足**：
    - baseline 期（前 7 天，**不含**短窗本身，避免双算）这对的 cooccur_count == 0
    - 当前短窗 cooccur_count >= 3
3.4 baseline 期的查询走 `mentions_repo` 现有的 `count_for_entity` 接口扩展
    （或 service 内自带 SQL；详见 design.md §3.4），不引入 schema 变更

### Req 4：性能约束

4.1 当前 `entity_mentions` 4943 行规模下，`run_once` 单轮总耗时 < 1 秒
4.2 当 `entity_mentions` 涨到 10 万行规模时，单轮总耗时 < 10 秒
4.3 当超过这两个目标时，记录 WARN 日志：
    - 4943 行下 > 1s → `cooccur run_once 慢速：N.Ns（4943-row baseline 1s 已超）`
    - 10 万行下 > 10s → 升级 ERROR：迁移路径见 design.md §3.5
4.4 实现策略选择（design.md §3.5 详述）：
    - 起步用 SQL `SELF JOIN` 直接聚合（4943 行毫秒级，写起来短）
    - 备选用内存 `itertools.combinations`（10 万行后切换；预留迁移路径）
    - 起步即用方案在 service 层用 SQL，但 `_compute_pairs` 用单 helper 隔离，
      未来切换不动其它逻辑

### Req 5：配置（NewPipelineSettings 扩展 6 字段）

5.1 应在 `config/_new.py` 的 `NewPipelineSettings` 末尾追加 6 个字段：

    | 字段 | 类型 | 默认值 | 含义 |
    |---|---|---|---|
    | `cooccur_enabled` | `bool` | `True` | False 时跳过 service 构造 |
    | `cooccur_window_type` | `str` | `'24h'` | 共现窗口长度 |
    | `cooccur_top_pairs` | `int` | `100` | 每窗口写 Top-K pair |
    | `cooccur_min_cooccur_count` | `int` | `3` | 短窗共现下限 |
    | `cooccur_min_pmi` | `float` | `1.0` | PMI 下限 |
    | `cooccur_min_window_msgs` | `int` | `50` | 窗口内消息总数下限 |

5.2 字段加载验证：

    ```bash
    .venv/bin/python -c "from config.settings import get_settings; \
      s=get_settings(); print(s.cooccur_enabled, s.cooccur_window_type, s.cooccur_min_pmi)"
    ```

    应输出 `True 24h 1.0`
5.3 默认 `cooccur_window_type='24h'`：1h 共现噪音太大不实用（窗口内消息少
    导致随机共现频繁），24h 才是稳定信号源

### Req 6：与 Worker 集成

6.1 应改 `main.py` 在 hotness_services 之后追加 cooccur_service 构造（Step 5e）
6.2 配置驱动开关：`if settings.cooccur_enabled` 才构造，否则 INFO 跳过日志
6.3 构造代码用 `try / except ValueError` 包裹，构造失败 `log.error` 不阻塞启动
6.4 注入 `Jobs.new_services`，调度顺序：在 hotness_services 之后、
    `AlertTriggerService` 之前（**或之后均可**，本任务不依赖告警通道）
6.5 共享同一个 `mentions_repo` / `sliding_counter` 引用（关键不变量）

### Req 7：测试覆盖（净新增 12 用例）

7.1 `tests/test_l3_cooccurrence.py` 新增 10 个用例（对应 tasks.md Task 2.4）：

    | # | 用例 | 关键断言 |
    |---|---|---|
    | 1 | `test_pairs_combination_correctness` | 3 实体在同一消息 → 3 对 |
    | 2 | `test_pairs_canonical_order` | 字典序 entity_a < entity_b |
    | 3 | `test_pmi_formula` | 给定 cooccur/count_a/count_b/N，PMI 数值与公式吻合 |
    | 4 | `test_pmi_independent_pair_low` | 两个高频但独立的实体 PMI ≈ 0 |
    | 5 | `test_pmi_correlated_pair_high` | 两个低频但常一起出现的实体 PMI 显著 > 0 |
    | 6 | `test_skips_when_data_sparse` | 窗口消息数 < 50 直接跳过返回 False |
    | 7 | `test_skips_when_window_unchanged` | 同一 window_end 第二次 run_once 返回 False |
    | 8 | `test_min_cooccur_count_filter` | 共现 1~2 次的对不写库 |
    | 9 | `test_min_pmi_filter` | PMI < 1.0 的对不写库 |
    | 10 | `test_upsert_idempotent` | 同窗口跑 2 次结果一致（rowcount 不暴涨） |

7.2 `tests/test_l3_cooccurrence.py` 新增 2 个用例（对应 tasks.md Task 3.2）：

    | # | 用例 | 关键断言 |
    |---|---|---|
    | 11 | `test_is_new_pair_baseline_zero_short_three` | baseline=0 且 short=3 → True |
    | 12 | `test_is_new_pair_baseline_one_short_ten` | baseline=1 → False（已不"新"） |

7.3 现有 135 个用例必须 100% pass，不允许改测试代码
7.4 不允许真的连接 PostgreSQL（用 SQLite 内存库 + monkeypatch datetime）；
    不允许真的调外部服务

### Req 8：日志规范

8.1 启动日志样例：

    ```
    CooccurrenceService 启动：window=24h top_pairs=100 min_pmi=1.0 min_cooccur=3
    ```

8.2 正常运行日志（INFO）：

    ```
    cooccur window_end=2026-05-13 10:15 pairs_written=42 new_pairs=5 elapsed=0.3s
    ```

8.3 跳过日志（INFO）：

    ```
    cooccur skipped: window unchanged
    cooccur skipped: data sparse (window_msgs=12 < 50)
    ```

8.4 慢速 WARN：

    ```
    cooccur run_once 慢速：1.5s（>1s 警告，window_end=...）
    ```

8.5 写库失败 ERROR：

    ```
    cooccur upsert failed: <reason>（不更新 _last_window_end，下一轮重试）
    ```

8.6 配置禁用日志：

    ```
    CooccurrenceService 未启用（cooccur_enabled=False）
    ```

## Success Metrics

### 阶段验收（部署后 24 小时内）

- [ ] **服务启动**：日志含 `CooccurrenceService 启动：window=24h top_pairs=100 min_pmi=1.0 min_cooccur=3`
- [ ] **测试基线**：`pytest` 100% pass，135 → 147（+12，0 回归）
- [ ] **DB 验证**：`SELECT COUNT(*) FROM entity_cooccurrence WHERE window_end > NOW() - INTERVAL '1 hour'`
      ≥ 1（至少一份共现榜已写入）
- [ ] **配置生效**：`settings.cooccur_min_pmi == 1.0` 且 `cooccur_top_pairs == 100`

### 业务验收（部署后 7 天内）

- [ ] **PMI 分布有信号**：`SELECT percentile_cont(0.99) WITHIN GROUP (ORDER BY pmi)
      FROM entity_cooccurrence WHERE window_end > NOW() - INTERVAL '7 days'`
      99% 分位 PMI ≥ 3.0（足够区分"巧合一起出现"和"真共振"）
- [ ] **至少抓到 1 次"新对"**：7 天内 `is_new_pair=TRUE` 的 pair ≥ 5 对
      （如果 0 对说明阈值太严或数据稀疏，调 `cooccur_min_cooccur_count=2` 再观察）
- [ ] **叙事覆盖肉眼可识别**：随机抽 10 对高 PMI（+ `is_new_pair=TRUE`）的实体，
      手工判断"这些是不是同一叙事的成员"，命中率 ≥ 60%
- [ ] **AlertTrigger 行为不变**：Phase 2.2 配置的 1h 告警频率与质量与本任务上线前一致

### 反向验证（确认没引入新风险）

- [ ] **零 LLM 验证**：`pytest tests/test_phase1_pipeline.py -v` 仍然
      `mock_chat.call_count == 0`
- [ ] **关闭共现验证**：把 `cooccur_enabled=False`，重启后服务正常跑，
      hotness/alert 行为完全等价
- [ ] **performance 守护**：当前 4943 行下单轮 < 1s（实测 < 0.5s 即合格）

## 硬约束（不可妥协）

1. **零 LLM**：CooccurrenceService 严格不 import `llm/ollama_client`
2. **零新依赖**：复用现有 numpy（用 `np.log` 算 PMI）/ sqlalchemy / Python 标准库
   `itertools`，**不引入** networkx / igraph / scipy 等图算法库
3. **不破坏老链路兼容**：现有 135 个测试必须 100% pass；hotness / alert 行为
   完全等价（共现是新增旁挂，不修改任何现有 service）
4. **不阻塞主流程**：CooccurrenceService 抛任何异常被 `Jobs._worker_loop` 异常隔离
   机制兜住；构造失败由 main.py 的 try/except 兜底降级
5. **本任务不接 Telegram**：明确不与 `AlertTriggerService` 共享告警通道，
   共现告警留 Phase 2.5.1 单独做；避免和单实体激增告警在用户视角下混淆

## 依赖与风险

### 依赖

- Phase 1 已交付：`entity_mentions` schema 与 `EntityMentionsRepo`，
  `SlidingCounter.active_entities('24h')` 接口
- Phase 2.1 已交付：`SlidingCounter` 已扩展 `'6h'` 窗口（本任务不直接用 6h，
  但保留可能性）
- Phase 2.2 已交付：`AlertTriggerService` 接口冻结，本任务不动它
- 不依赖任何新 pip 包、任何新外部服务

### 风险

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| SELF JOIN 在 entity_mentions 涨到 100k+ 时慢 | 中 | 中：单轮 > 10s | design.md §3.5 留迁移路径，切换内存 `itertools.combinations`；4943 行下毫秒级，远未触及阈值 |
| PMI 在 count_a/count_b 极小时数值不稳定 | 中 | 低：PMI 偶尔虚高 | smoothing 设计（分子 +1，分母 +K=候选实体数）；`min_pmi=1.0` 兜底过滤 |
| 候选 pair 数量 n² 爆炸 | 低 | 中：内存膨胀 | 候选集走 `active_entities('24h')`（实测 < 1000）；min_cooccur_count=3 + Top-100 切片 |
| 同一对实体在 (a,b) 与 (b,a) 双写 | 低 | 高：唯一约束破坏 | canonical pair order：`itertools.combinations(sorted(entities), 2)` 强制字典序 |
| 数据稀疏（消息少）导致 PMI 全是噪音 | 中 | 低：榜空 | `min_window_msgs=50` 跳过 + INFO 日志；窗口默认 24h 而非 1h |
| 共现写入与 alert 调度时序冲突 | 极低 | 低 | 单 worker 线程串行调度（与 Phase 1 一致），无并发竞争 |
| 用户误关闭 `cooccur_enabled` 后忘记开 | 低 | 低 | 启动日志会清晰输出 `未启用（cooccur_enabled=False）`，巡检即发现 |

---

*文档版本：v1.0*
*基于：tasks.md v1.0 + 终极设计文档 §8 L3 共现网络*
*预估工时：3~4 小时净 coding（不含 spec 写作）*
*测试基线：135 → 147 passed（+12，0 回归）*
