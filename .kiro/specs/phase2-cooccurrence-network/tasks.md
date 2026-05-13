# Phase 2 · Task 2.5 L3 实体共现网络 · Implementation Tasks

> 终极设计文档 §8 L3 共现网络的最小实现版。在已有的 `entity_mentions` 上做
> 实体两两共现统计，**自动发现新叙事**——当若干实体在同一时间段被频繁一起
> 提及时，标记为新兴叙事。
>
> ⚠️ **本文档是 tasks-only 草案**，实施前建议补 design.md / requirements.md
> （参考 phase2-telegram-alerts / phase2-multi-window-hotness 的版式）。

---

## 背景

Phase 2.1 / 2.2 上线后，单个实体的"突然热"已经能被捕捉。但有一类信号当前
系统**无法发现**：

- 几个实体**同时**变热（例如 `EIGEN` + `ETHFI` + `REZ` 同期讨论量飙升）
- 用户讨论时往往不直接说出"赛道名"（例如不会说"restaking 起飞"，但会同时
  提到上面三个 token）

L3 共现网络解决这个问题：通过统计**实体两两在同一条消息中共同出现的频次**，
在 24 小时窗口内构建图（节点=实体，边=共现次数），找出"突然连成簇"的子图，
即新兴叙事。

**Task 2.5 目标**：把"看单个实体的榜"升级到"看实体网络的拓扑变化"。

## 设计草案

### 数据模型

新增一张表（不动 schema 已有 8 张表）：

```sql
CREATE TABLE entity_cooccurrence (
    id              BIGSERIAL PRIMARY KEY,
    entity_a        VARCHAR(128) NOT NULL,  -- 字典序：a < b 保证 (a,b) = (b,a)
    entity_b        VARCHAR(128) NOT NULL,
    window_end      TIMESTAMPTZ NOT NULL,
    window_type     VARCHAR(16) NOT NULL,   -- '1h' / '6h' / '24h'
    cooccur_count   INTEGER NOT NULL,       -- 短窗内一起出现的消息数
    pmi             FLOAT,                  -- Pointwise Mutual Information，归一化共现强度
    is_new_pair     BOOLEAN DEFAULT FALSE,  -- 7 天基线为 0 且短窗 >= 3 的"突然成对"
    UNIQUE (entity_a, entity_b, window_end, window_type)
);
CREATE INDEX idx_cooccur_window_pmi ON entity_cooccurrence(window_end, window_type, pmi DESC);
```

为什么用 PMI（点互信息）：

```
PMI(a, b) = log( P(a,b) / (P(a) * P(b)) )
        = log( cooccur_count / (count_a * count_b / N) )
```

- PMI 越大表示"a 和 b 一起出现的概率远高于独立出现"
- 普通"BTC 和 USDT 一起被提"PMI 很低（都是巨头，独立频率本来就高）
- "EIGEN 和 ETHFI 一起被提"PMI 高（独立频率本来不高，但一起出现频繁）
- 是 surface 新叙事的关键指标

### 新增依赖

```
✅ 复用现有 numpy（log 计算）—— numpy 已被 SQLAlchemy 间接引入，不算新依赖
✅ 复用现有 sqlalchemy / loguru
不引入新 pip 包，符合"零新依赖"硬约束
```

### 新增服务

`services/l3_cooccurrence.py` —— `CooccurrenceService`

```python
@dataclass
class CooccurrenceService:
    db
    mentions_repo
    cooccur_repo
    sliding_counter
    window_type: str = "24h"  # L3 默认 24h（共现需要长窗才稳定）
    top_pairs: int = 100      # 每窗口写 Top-100 pair
    min_cooccur_count: int = 3
    min_pmi: float = 1.0      # 等同于"共现概率 ≥ 独立预期的 e 倍"
    timezone: ZoneInfo

    def run_once(self) -> bool:
        """与 HotnessService 同款 align_to_quarter，每 15 分钟跑一次"""
        ...
```

### 算法（核心）

每轮扫一遍 `entity_mentions`：

```sql
-- 取 24h 窗口内所有 mention，按 msg_id 聚合
WITH window_mentions AS (
    SELECT msg_id, entity
    FROM entity_mentions
    WHERE ts >= window_end - INTERVAL '24 hours'
      AND ts < window_end
)
-- 按消息聚合，生成实体对
SELECT
    LEAST(a.entity, b.entity) AS entity_a,
    GREATEST(a.entity, b.entity) AS entity_b,
    COUNT(DISTINCT a.msg_id) AS cooccur_count
FROM window_mentions a
JOIN window_mentions b
  ON a.msg_id = b.msg_id AND a.entity < b.entity
GROUP BY 1, 2
HAVING COUNT(DISTINCT a.msg_id) >= 3;
```

**性能考虑**：上面的 SELF JOIN 在 4943 行 entity_mentions 上估算 < 1 秒。
当 entity_mentions 涨到 100k 行时需要加 `idx_entity_mentions_msg_entity`
（已有 UNIQUE 索引覆盖），仍可接受。Phase 3 真有压力时改成在内存里做组合。

### 共现告警（可选 Phase 2.5.1）

本任务先**只产生 entity_cooccurrence 表**，不接 Telegram。等观察 1 周看
PMI 分布稳定后再决定是否加"共现新叙事告警"通道。

### 五条硬约束（沿用）

1. 零 LLM
2. 不阻塞主流程：CooccurrenceService 失败不影响其它 service
3. 不引入新依赖（用现有 SQL + numpy 内置 log）
4. 不破坏向后兼容（135 passed 起点）
5. 配置驱动开关：`cooccur_enabled=False` 时跳过

---

**执行约定**：
- 每完成一个 Task 跑测试，pass 数只增不减
- 测试基线起点：**144 passed**（Phase 2.4 完工状态，spec 写的 135 是 Phase 2.1
  的旧基线；本任务实际起点 144，目标 156 = 144 + 12）
- 全部 Task 完成后落点：**157 passed**（+13，含 1 个防御性短路用例，0 回归）

---

## Task 0：基线验证 + 数据可行性检查

- [x] **0.1 跑 pytest 确认基线 144 passed**
  - `144 passed, 1 skipped`
- [x] **0.2 检查当前 entity_mentions 数据是否够支撑共现统计**
  - 实测：24h 内含 ≥2 实体的消息 = 163 条，total_mentions_24h = 812 条，
    远超 50 阈值，数据量充足

## Task 1：数据库 schema

- [x] **1.1 新建 alembic 迁移 `002_phase2_cooccurrence.py`**
  - 创建 `entity_cooccurrence` 表 + 1 唯一约束 + 3 条索引
- [x] **1.2 跑迁移**
  - `alembic upgrade head` 成功；`\d entity_cooccurrence` 验证通过
- [x] **1.3 加 ORM 模型 `db/models.py`**
  - `class EntityCooccurrence(Base)` 字段对齐迁移 + 加入 __all__
- [x] **1.4 加 repo `db/repositories/cooccurrence_repo.py`**
  - `upsert_batch(window_end, window_type, pairs)`
  - `fetch_top_k_pairs(window_end, window_type, k=100)`
  - `fetch_neighbors(entity, window_end, k=10)`
- [x] **1.5 跑测试**
  - `144 passed`（修了 test_models.py 里的 expected 集合，加 entity_cooccurrence）

## Task 2：CooccurrenceService 核心实现

- [x] **2.1 创建 `services/l3_cooccurrence.py`**
  - `CooccurrenceService` dataclass + `__post_init__` 校验（window_type / min_cooccur_count / top_pairs）
  - 模块级 `_pmi(cooccur, count_a, count_b, N)` 函数
- [x] **2.2 实现 `_compute_pairs(window_end, short_start, window_msgs)` 方法**
  - 候选 = `active_entities('24h')`
  - 流式拉短窗 (msg_id, entity)，按 msg_id 分组
  - itertools.combinations(sorted(unique), 2) 强制 canonical 字典序
  - 单消息最多取前 10 个实体（max_entities_per_msg=10）防长尾爆炸
  - 累计 cooccur_count + entity_count，返回带 PMI 的 list[dict]
- [x] **2.3 实现 `run_once()` 方法**
  - align_to_quarter（复用 services/l2_hotness 的 helper）
  - 跳过：window 未变 / 数据稀疏（msgs < 50）
  - upsert_batch 写 Top-K + 慢速警告
- [x] **2.4 单元测试 `tests/test_l3_cooccurrence.py`**（10 个用例 + 1 防御）
  - test_pairs_combination_correctness ✅
  - test_pairs_canonical_order ✅
  - test_pmi_formula ✅
  - test_pmi_independent_pair_low ✅
  - test_pmi_correlated_pair_high ✅
  - test_skips_when_data_sparse ✅
  - test_skips_when_window_unchanged ✅
  - test_min_cooccur_count_filter ✅
  - test_min_pmi_filter ✅
  - test_upsert_idempotent（SQLite + 子类化 repo）✅
- [x] **2.5 跑测试**
  - `155 passed`（144 + 10 + 1 防御）

## Task 3：is_new_pair 检测（突然成对）

- [x] **3.1 实现 `_is_new_pair(entity_a, entity_b, *, baseline_start, short_start, cooccur_count)`**
  - cooccur_count < 3 短路返回 False
  - mentions_repo.count_pair_cooccur 查 baseline 期 SELF JOIN
- [x] **3.2 单元测试**（+2 用例）
  - test_is_new_pair_baseline_zero_short_three → True ✅
  - test_is_new_pair_baseline_one_short_ten → False ✅
  - （额外）test_is_new_pair_short_circuit_below_min（防御）✅
- [x] **3.3 跑测试**
  - `157 passed`（155 + 2）

## Task 4：配置扩展

- [x] **4.1 改 `config/_new.py` 加 6 字段**
  - `cooccur_enabled / cooccur_window_type / cooccur_top_pairs /
    cooccur_min_cooccur_count / cooccur_min_pmi / cooccur_min_window_msgs`
- [x] **4.2 验证配置加载**
  - 实测输出：`True / 24h / 100 / 3 / 1.0 / 50`，全部默认值正确
- [x] **4.3 跑测试**
  - `157 passed`

## Task 5：main.py 注入

- [x] **5.1 改 `main.py` Step 5d-pre**
  - 在 hotness_services 之后、AlertTriggerService 之前构造 cooccur_service
  - 配置驱动开关 `if settings.cooccur_enabled` + try/except ValueError 兜底
  - 共享 mentions_repo / sliding_counter（关键不变量）
  - 加入 new_services 列表
- [x] **5.2 验证 main.py 仍能 import**
  - `python -c "import main"` 干净通过
- [x] **5.3 跑测试**
  - `157 passed`

## Task 6：本地端到端验收

- [x] **6.1 重启服务**
  - 临时 smoke 脚本验证：CooccurrenceService.run_once() 返回 True，
    elapsed=0.4s，写入 8 对 pair（候选 entity 数 193）
- [x] **6.2 等下一个 quarter，SQL 验证 entity_cooccurrence 已写入**
  - 实测最新窗口 Top-8：META+TSLA(PMI 3.84) / VVV+稳定币 / BNB+币安人生 / ...
- [x] **6.3 看 is_new_pair=True 的对**
  - 当前窗口 0 对（首次写入，1~2 天后能积累 baseline 后才会出现）
  - 等 7 天后真正的"突然成对"信号会自然浮现

## Task 7：文档 + check_status.py 集成

- [x] **7.1 改 `scripts/check_status.py` 加第 7 节"共现 Top-20 + 新对"**
  - PMI 降序 Top-20 + 单独列 is_new_pair=TRUE 清单 + 解读小贴士
- [x] **7.2 加 `docs/operations_guide.md` §6.4 共现网络调参**
  - 调参速查表（6 字段） + SQL 查最新窗口 + 调阈值节奏 + 日志关键字 + 常见问题
  - §6 总速查表追加 4 行 cooccur_* 调参条目
  - §2 启动日志样例追加 CooccurrenceService 一行
- [x] **7.3 加 `docs/faq_design_decisions.md` Q9**
  - 单实体榜的盲区（restaking 复苏案例）
  - PMI 比 cooccur_count 强的具体数值对比
  - 为什么默认 24h 窗口（1h 噪音太大的统计学解释）
  - 为什么共享 sliding_counter
  - is_new_pair 双条件的设计逻辑
  - 为什么本任务不接 Telegram（留 Phase 2.5.1）

## 执行顺序与依赖图

```
Task 0 (基线 144 + 数据可行性)
   └─► Task 1 (schema 迁移 + ORM + repo)
           └─► Task 2 (CooccurrenceService 核心 → 155)
                   └─► Task 3 (is_new_pair → 157)
                           └─► Task 4 (配置)
                                   └─► Task 5 (main.py 注入)
                                           └─► Task 6 (端到端)
                                                   └─► Task 7 (文档 + check_status)
```

## 完工后状态

```
新增文件：
  alembic/versions/002_phase2_cooccurrence.py
  db/repositories/cooccurrence_repo.py
  services/l3_cooccurrence.py
  tests/test_l3_cooccurrence.py
  .kiro/specs/phase2-cooccurrence-network/{requirements,design}.md  ← 实施前补齐

修改文件：
  db/models.py                               +EntityCooccurrence ORM
  db/repositories/entity_mentions_repo.py    +count_pair_cooccur / +count_distinct_msgs_since
  config/_new.py                             +6 字段（cooccur_*）
  main.py                                    +Step 5d-pre：CooccurrenceService 构造
  scripts/check_status.py                    +§7 共现 Top-20 + 新对
  docs/operations_guide.md                   +§6.4 + 速查表 4 行 + 启动日志样例 1 行
  docs/faq_design_decisions.md               +Q9
  tests/test_models.py                       +entity_cooccurrence 进 expected 集合
  .kiro/specs/phase2-cooccurrence-network/tasks.md  +勾选所有项

测试基线：144 → 157 passed（+13，含 1 个防御性短路用例，0 回归）
新增能力：自动发现新叙事（"实体突然成对" / "PMI 异常高"）
```

## 风险与未决议题（实施前在 design.md 解决）

| 风险 | 优先级 | 解决方向 |
|---|---|---|
| SELF JOIN 在 entity_mentions 涨到 100k+ 时慢 | 中 | 加 GIST/HASH 索引；或改成在内存里 itertools.combinations |
| 共现 pair 数量爆炸（n²）| 中 | 每轮只算 active_entities("24h") 内的实体；min_cooccur_count=3 过滤 |
| PMI 公式分母 P(a)*P(b) 在 a/b 频率极低时不稳定 | 低 | 加 smoothing：分子 +1 / 分母 + (count_a+count_b) / N |
| 共现消息中实体数 > 10 时组合爆炸（n=10 → 45 对）| 低 | 加 max_entities_per_msg=5 上限，长尾消息只取前 5 个实体 |
| 中文/英文别名混着出现导致同一实体被算两次 | 中 | prefilter 已经做了归一化（BTC 大写），共现层不重复处理 |
| 共现告警是否上 Telegram | 低 | 本任务不做，留 Phase 2.5.1 |

---

*文档版本：v1.0*
*预估工时：实施前补 design 1 天 + 编码 2~3 天 ≈ 3~4 天*
*对早期热点发现的契合度：⭐⭐⭐⭐ 能发现"实体集群一起冒头"，比单实体榜信号维度高*
