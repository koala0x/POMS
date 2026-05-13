# Phase 2 · Task 2.7 L5 LLM 定向简报 · Implementation Tasks

> 终极设计文档 §10 L5 的最小实现版。在已经发现的"热点实体 / 新叙事"上**叠加一层
> LLM 解释**——给热榜实体生成结构化 JSON 简报（叙事归属 / 催化事件 / 资金逻辑），
> 让你看榜单时不仅知道"什么热"，还知道"为什么热"。
>
> ⚠️ **本文档是 tasks-only 草案**，实施前必须补 design.md / requirements.md，
> 因为本任务**重新引入 LLM**，与 Phase 1 的"零 LLM 硬约束"形成对比，需用户决策。

---

## 背景

Phase 1 / 2.x 上线后，你能看到：

```
最新 1h 榜：
  rank=1 EIGEN  growth=20.3 cross=3 count_short=42
  rank=2 ETHFI  growth=12.8 cross=2 count_short=18
```

但你需要回答**"为什么 EIGEN 突然热"**才能决定要不要跟进——这是当前系统无法
告诉你的：原始消息文字在 normalized_messages 表里散落着，看 SQL 自己读 100 条
推文太累。

**Task 2.7 目标**：当某 entity 进入 hotness Top-N 时，调 LLM 读它的代表性消息
（ClusteringService 已选好的代表 + Top engagement 几条），输出结构化 JSON：

```json
{
  "entity": "EIGEN",
  "narrative": "Restaking 复苏",
  "catalyst": "EigenLayer 主网升级 v2.0 上线，新增 LST 资产支持",
  "fund_logic": "Restaking 赛道 TVL 反弹至 200 亿美元，EIGEN 排名第 1",
  "sentiment": "bullish",
  "confidence": 0.85,
  "evidence_msg_ids": [1234, 1567, 1789]
}
```

这份 JSON 落到一张 `entity_briefings` 表，可以直接喂给 Telegram 告警的消息模板，
让推送从"`$EIGEN growth 20×`"升级到"`$EIGEN ↑20× | EigenLayer v2.0 上线，
Restaking 复苏`"。

## 设计草案

### 与 Phase 1"零 LLM 硬约束"的关系

Phase 1 故意不调 LLM，因为：
1. Phase 1 重点是**信号产生**——LLM 不创造信号，只解释
2. Phase 1 没经过 Gate 1 验证前不引入推理成本

Phase 2 的前面四个子任务（2.1~2.6）继承了"零 LLM"约束。**Task 2.7 是第一个
明确突破这个约束的任务**——只在"信号已经产生"之后调 LLM 加解释，**不让 LLM
影响信号本身**（hotness 公式、共现统计、聚类都不看 briefing 结果）。

这是"LLM 加层"而不是"LLM 替代"，跟老链路 Level1Service / Level2Service 调
LLM 做摘要本质相同。所以我们**复用现有 OllamaClient**：

```python
from llm.ollama_client import OllamaClient

ollama_l5 = OllamaClient(
    base_url=settings.ollama_base_url,
    model=settings.ollama_model_level1,  # 复用 qwen3:8b
    timeout_seconds=settings.ollama_timeout_level1,
)
```

### 数据模型

```sql
CREATE TABLE entity_briefings (
    id              BIGSERIAL PRIMARY KEY,
    entity          VARCHAR(128) NOT NULL,
    window_end      TIMESTAMPTZ NOT NULL,
    narrative       TEXT,
    catalyst        TEXT,
    fund_logic      TEXT,
    sentiment       VARCHAR(16),  -- "bullish" / "bearish" / "neutral"
    confidence      FLOAT,
    evidence_msg_ids BIGINT[] NOT NULL,
    raw_response    JSONB,         -- LLM 原始返回，便于调试
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (entity, window_end)
);
```

幂等：`(entity, window_end)` 唯一约束。同一窗口同实体多次调用走 ON CONFLICT DO NOTHING（不覆盖）。

### 新增服务

`services/l5_briefing.py` —— `BriefingService`

```python
@dataclass
class BriefingService:
    db
    hotness_repo
    mentions_repo
    briefing_repo
    ollama: OllamaClient
    prompt_path: Path
    top_n: int = 5             # 只对 1h 榜 Top-5 生成 briefing
    min_growth: float = 30.0   # 只对 growth >= 30 的实体（避免 LLM 浪费在噪音上）
    timezone: ZoneInfo

    def run_once(self) -> bool:
        """
        每 15 分钟整点：
          1. 拉最新 1h 榜 Top-N（filter growth >= min_growth）
          2. 跳过已有 briefing 的 entity（ON CONFLICT 兜底）
          3. 对每个 entity：
             - 拉 Top-10 evidence 消息（按 engagement 排序，无 engagement 则随机）
             - 渲染 prompt 模板
             - 调 ollama.chat → 期望 JSON
             - 解析 JSON，写入 entity_briefings
          4. 失败的 entity 不阻塞其它（异常隔离）
        """
        ...
```

### Prompt 模板

`prompts/level5_briefing.txt`：

```
你是加密市场分析助手。下面是关于实体 {entity} 的 {n_msgs} 条社交媒体消息。
请只输出一个 JSON 对象，不要任何解释或 markdown 包裹。字段：

{
  "narrative": "（这个实体当前归属的叙事，例如 "Restaking" / "AI Agent"，不超过 20 字）",
  "catalyst": "（具体催化事件，例如 "EigenLayer 主网升级 v2.0 上线"，不超过 50 字）",
  "fund_logic": "（资金面/基本面逻辑，例如 "TVL 反弹至 200 亿美元"，不超过 50 字）",
  "sentiment": "bullish" | "bearish" | "neutral",
  "confidence": 0.0-1.0
}

如果消息中无法判断某字段，填 null。

消息：
{messages}
```

### 与 Phase 2.5 / 2.6 的协同

- **如果 Task 2.6（Embedding 聚类）已完成**：evidence 消息直接选每个簇的代表，
  避免 LLM 看到大量重复内容
- **如果 Task 2.5（共现）已完成**：briefing 的 narrative 字段可以用共现 Top-PMI
  的邻居作为 hint 加进 prompt，让 LLM 输出更稳定

本任务不强依赖 2.5/2.6，但都做完后效果会显著提升。

### 五条硬约束的明确妥协

| 约束 | 本任务的明确状态 |
|---|---|
| **零 LLM** | ❌ **明确突破**——本任务的核心就是引入 LLM |
| 不阻塞主流程 | ✅ BriefingService 失败不影响 hotness / cooccur |
| 不引入新依赖 | ✅ 复用现有 OllamaClient |
| 不破坏向后兼容 | ✅ briefings 表是新增，不动现有 |
| 配置缺失即降级 | ✅ `briefing_enabled=False` 时跳过 |

**用户决策点**：第 1 条硬约束被明确打破。是否接受？参考方案：

- **A. 接受**（本任务方案）—— 用 LLM 在信号产生后加解释，不影响信号本身
- **B. 跳过**——保持纯统计系统，不把 LLM 引入新链路（Phase 3 再考虑）
- **C. 方案变体**：不调 LLM，改成"把 Top-N entity 的代表消息直接附在 Telegram
  推送里"——你自己读 5 条原文判断为什么热。工程量减少 80%，但失去 LLM 的"归纳
  叙事"能力

---

**执行约定**：
- 每完成一个 Task 跑测试，pass 数只增不减
- 测试基线起点：**135 passed**（Phase 2.1 完工状态）
- 全部 Task 完成后落点：**147 passed**（+12，0 回归）

---

## Task 0：可行性 + 用户决策

- [ ] **0.1 跑 pytest 确认基线 135 passed**
- [ ] **0.2 验证 Ollama 可用**
  - `curl http://192.168.1.219:11434/api/tags`（看 qwen3:8b 在不在）
  - 不可用 → 推迟本任务
- [ ] **0.3 用户决策**：方案 A / B / C
- [ ] **0.4 Prompt 工程**（实施前在 design.md 完成）
  - 试写 prompts/level5_briefing.txt
  - 用 5 个真实 entity（EIGEN / SOL / WIFHAT 等）跑通 LLM 的 JSON 输出
  - 检查 JSON 合法率（应 ≥ 90%，否则改 prompt）

## Task 1：数据库 schema

- [ ] **1.1 新建 alembic 迁移 `004_phase2_briefings.py`**
  - 创建 entity_briefings 表
  - 加 UNIQUE (entity, window_end)
  - 加索引 idx_briefings_window_end
- [ ] **1.2 跑迁移 + 验证**
- [ ] **1.3 加 ORM 模型 `db/models.py`**
- [ ] **1.4 加 repo `db/repositories/briefings_repo.py`**
  - `upsert_one(entity, window_end, fields)` —— ON CONFLICT DO NOTHING
  - `fetch_for_entity(entity, window_end)` 给告警通道用
  - `fetch_recent(window_end, limit)` 给面板用
- [ ] **1.5 跑测试**
  - 预期仍 135 passed

## Task 2：Prompt 模板

- [ ] **2.1 创建 `prompts/level5_briefing.txt`**
  - 见上面"设计草案"
- [ ] **2.2 用 5 个真实 entity 实测**
  - 写一个临时脚本 `scripts/_smoke_briefing.py`
  - 选 5 个当前 hotness 榜单的实体
  - 拉 Top-10 mention，渲染 prompt，跑 ollama.chat
  - 检查 JSON 合法率
- [ ] **2.3 根据实测调 prompt**
  - 直到 5 条全部输出合法 JSON
- [ ] **2.4 删除临时脚本**

## Task 3：BriefingService 核心

- [ ] **3.1 创建 `services/l5_briefing.py`**
  - `BriefingService` dataclass + 全部字段
- [ ] **3.2 实现 `_select_evidence(entity, window_end)` 内部方法**
  - 拉过去 1h 该 entity 的 mentions
  - 按 engagement 排序（无 engagement 时随机）
  - 取 Top-10
  - 如果 Phase 2.6 已上线：优先取每个 cluster 的代表
- [ ] **3.3 实现 `_render_prompt(entity, evidence)` 内部方法**
  - 加载 prompt_path 模板
  - 替换 {entity} / {n_msgs} / {messages} 占位符
- [ ] **3.4 实现 `_parse_json(response)` 内部方法**
  - JSON.loads + 字段校验
  - 解析失败 raise ValueError（不写表）
- [ ] **3.5 实现 `run_once()`**
  - align_to_quarter
  - 拉 hotness Top-N（1h 榜 + growth >= min_growth）
  - 过滤已有 briefing 的 entity（fetch_for_entity 返回非 None）
  - 对每个 entity：try / except 隔离 + log error
- [ ] **3.6 单元测试 `tests/test_l5_briefing.py`**（10 个用例）
  - test_select_evidence_top_n
  - test_select_evidence_falls_back_to_random_when_no_engagement
  - test_render_prompt_replaces_placeholders
  - test_parse_json_valid
  - test_parse_json_invalid_raises
  - test_skips_when_no_top_entities
  - test_skips_already_briefed_entities
  - test_per_entity_failure_isolated（一个 entity 失败不影响其它）
  - test_skips_when_window_unchanged
  - test_low_growth_filtered_out
- [ ] **3.7 跑测试**
  - 预期 135 + 10 = **145 passed**

## Task 4：配置扩展

- [ ] **4.1 改 `config/_legacy.py` 加 LLM briefing 配置（决策：放老链路分组）**
  - **决策理由**：briefing 是 LLM 配置，跟 ollama_model_level1/level2 同组管理；
    `_legacy.py` 当前命名虽然偏老链路，但实际它就是"所有 Ollama 配置"的归档处。
    备选方案：新建 `config/_llm.py` 分组（更干净但要改 settings.py 多继承），
    实施前在 design.md 决定走哪个
  - `ollama_model_level5: str = "qwen3:8b"`
  - `ollama_timeout_level5: int = 600`
- [ ] **4.2 改 `config/_new.py` 加 briefing 业务配置**
  - `briefing_enabled: bool = True`
  - `briefing_top_n: int = 5`
  - `briefing_min_growth: float = 30.0`
  - `briefing_evidence_count: int = 10`
- [ ] **4.3 验证配置加载**
- [ ] **4.4 跑测试**
  - 预期仍 145 passed

## Task 5：main.py 注入

- [ ] **5.1 改 `main.py`**
  - 构造 ollama_l5 = OllamaClient(model=ollama_model_level5)
  - 构造 BriefingService 注入新 services 列表
  - 配置驱动开关 `if settings.briefing_enabled`
- [ ] **5.2 配置缺失即降级**
- [ ] **5.3 验证 main.py 仍能 import**
- [ ] **5.4 跑测试**
  - 预期仍 145 passed

## Task 6：Telegram 推送集成（可选）

> 让告警消息附带 briefing 内容，从"什么热"升级到"为什么热"。

- [ ] **6.1 改 `services/l2_alert_trigger.py`**
  - `_render_message` 在原模板基础上追加 briefing 字段（如果存在）
  - 优雅降级：briefing 不存在时仍按原模板推送
- [ ] **6.2 加测试**（+2 用例）
  - test_alert_message_includes_briefing
  - test_alert_message_falls_back_when_no_briefing
- [ ] **6.3 跑测试**
  - 预期 145 + 2 = **147 passed**

## Task 7：本地端到端验收

- [ ] **7.1 重启服务**
  - 启动日志含 "BriefingService 启动：top_n=5 min_growth=30.0"
- [ ] **7.2 等下一个 quarter，SQL 验证 briefings 已生成**
  - `SELECT entity, narrative, catalyst, sentiment, confidence FROM entity_briefings ORDER BY created_at DESC LIMIT 10;`
  - 人工评估输出质量（叙事是否准确？催化事件是否抓到了关键点？）
- [ ] **7.3 看下一条 Telegram 告警是否带了 briefing 字段**

## Task 8：文档

- [ ] **8.1 改 `docs/operations_guide.md` §6.5 LLM 简报调参**
  - 模型选择（qwen3:8b vs 30b）
  - 超时设置
  - 评估输出质量的方法
- [ ] **8.2 加 `docs/faq_design_decisions.md` Q11**
  - "为什么 Phase 2.7 突破了零 LLM 硬约束？"
  - "LLM 输出的 narrative 不准确怎么办？"
  - "为什么不直接用 LLM 替代 hotness 公式？"

## 执行顺序与依赖图

```
Task 0 (可行性 + 用户决策)
   └─► Task 1 (schema + repo)
           └─► Task 2 (prompt 工程)
                   └─► Task 3 (BriefingService → 145)
                           └─► Task 4 (配置)
                                   └─► Task 5 (main.py 注入)
                                           ├─► Task 6 (Telegram 集成 → 147)
                                           └─► Task 7 (端到端 + 8 文档)
```

## 完工后状态

```
新增文件：
  alembic/versions/004_phase2_briefings.py
  db/repositories/briefings_repo.py
  services/l5_briefing.py
  prompts/level5_briefing.txt
  tests/test_l5_briefing.py

修改文件：
  db/models.py                       +EntityBriefing ORM
  config/_legacy.py                  +ollama_model_level5 / timeout_level5
  config/_new.py                     +4 briefing 字段
  main.py                            +BriefingService 构造
  services/l2_alert_trigger.py       +briefing 渲染
  tests/test_l2_alert_trigger.py     +2 cases
  docs/operations_guide.md           +§6.5
  docs/faq_design_decisions.md       +Q11

测试基线：135 → 147 passed（+12，0 回归）
新增能力：热点实体的"为什么热"自动归纳
```

## 风险与未决议题（实施前在 design.md 解决）

| 风险 | 优先级 | 解决方向 |
|---|---|---|
| **零 LLM 硬约束被突破** | 高 | 用户决策；本任务做的是"信号产生后加解释"，不影响信号本身，比 Phase 1 老链路 LLM 摘要更轻 |
| LLM 输出 JSON 格式不稳定 | 高 | prompt 工程 + 实测合法率 ≥ 90%；不合法的不写表 |
| LLM 推理慢（CPU 模式）影响 hotness | 中 | 串行 worker 设计已经隔离；BriefingService 排在最后 |
| qwen3:8b 中文叙事归纳能力够吗 | 中 | 实测 5 个 entity，质量不够换 30b |
| LLM 幻觉：编造没出现在 evidence 里的内容 | 中 | prompt 强调"只能基于消息内容"+ evidence_msg_ids 字段记录用了哪些消息 |
| Briefing 信息过期 | 低 | 每 15 分钟刷新，UNIQUE 约束保证同窗口同实体一次 |
| Telegram 消息长度超限（4096 字符）| 低 | TelegramClient 已有 4000 字符截断逻辑 |
| 失败的 briefing 占据"已处理"位置 | 低 | 不写表 = ON CONFLICT 不触发 = 下一轮可重试 |

---

*文档版本：v1.0*
*预估工时：实施前补 design + prompt 工程 1~2 天 + 编码 3~5 天 ≈ 1 周*
*对早期热点发现的契合度：⭐⭐ 不创造新信号——是"看完榜单后的辅助"。建议放在最后做*
