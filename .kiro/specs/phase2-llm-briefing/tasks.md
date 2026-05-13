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
    model=settings.ollama_model_level5,
    timeout_seconds=settings.ollama_timeout_level5,
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

### 五条硬约束的明确妥协

| 约束 | 本任务的明确状态 |
|---|---|
| **零 LLM** | ❌ **明确突破**——本任务的核心就是引入 LLM |
| 不阻塞主流程 | ✅ BriefingService 失败不影响 hotness / cooccur |
| 不引入新依赖 | ✅ 复用现有 OllamaClient |
| 不破坏向后兼容 | ✅ briefings 表是新增，不动现有 |
| 配置缺失即降级 | ✅ `briefing_enabled=False` 时跳过 |

---

**执行约定**：
- 每完成一个 Task 跑测试，pass 数只增不减
- 测试基线起点：**157 passed**（Phase 2.5 完工状态，spec 写的 135 是 Phase 2.1
  的旧基线；本任务实际起点 157，目标 167 = 157 + 10）
- 全部 Task 完成后落点：**168 passed**（+11，含 1 个 markdown 剥离防御用例，0 回归）
- ★ Task 6（Telegram 集成）按用户决策**暂缓**——观察 1~2 周 briefing 质量稳定后再做

---

## Task 0：可行性 + 用户决策

- [x] **0.1 跑 pytest 确认基线 157 passed**
- [x] **0.2 验证 Ollama 可用**
  - 初始连接被拒（127.0.0.1 only），用户加 `OLLAMA_HOST=0.0.0.0:11434` 后通
  - `qwen3:8b` 和 `qwen3:30b` 都已 pull
- [x] **0.3 用户决策：方案 A / B / C → 选 A**
  - 配置位置：沿用 `config/_legacy.py`（不新建 `_llm.py`）
  - Task 6 暂缓（先观察 1~2 周 briefing 质量稳定后再接 Telegram）
- [x] **0.4 Prompt 工程**
  - 详见 Task 2.2 实测结果

## Task 1：数据库 schema

- [x] **1.1 新建 alembic 迁移 `004_phase2_briefings.py`**
  - 创建 entity_briefings 表 + 1 唯一约束 + 1 条索引
  - 跳过版本号 003（留给已暂缓的 phase2-embedding-clustering）
- [x] **1.2 跑迁移 + 验证**
  - `alembic upgrade head` 成功
- [x] **1.3 加 ORM 模型 `db/models.py`**
  - `class EntityBriefing(Base)` 字段对齐迁移 + 加入 __all__
- [x] **1.4 加 repo `db/repositories/briefings_repo.py`**
  - `upsert_one(entity, window_end, fields)` —— ON CONFLICT DO NOTHING
  - `fetch_for_entity(entity, window_end)`
  - `fetch_recent(window_end, limit)`
- [x] **1.5 跑测试**
  - 修了 test_models.py 加 entity_briefings 进 expected 集合
  - **157 passed**（无回归）

## Task 2：Prompt 模板 + 实测

- [x] **2.1 创建 `prompts/level5_briefing.txt`**
  - 含 4 占位符：`{entity}` / `{n_msgs}` / `{cooccur_hint}` / `{messages}`
  - 强制 JSON 输出 + 禁止 markdown 包裹 + 禁止编造 evidence 之外内容
- [x] **2.2 用 5 个真实 entity 实测**
  - 写了临时脚本 `scripts/smoke_briefing.py`
  - 选 24h 提及最多的 5 个 entity（ETH / BTC / BNB / 稳定币 / OP）
  - 每条 evidence 10 条，调 qwen3:8b
- [x] **2.3 根据实测调 prompt**
  - **实测合法率 5/5 = 100%**，无需回炉
  - 平均耗时 30s/次（Top-5 一轮 ~2.5 分钟）
- [x] **2.4 删除临时脚本**

## Task 3：BriefingService 核心

- [x] **3.1 创建 `services/l5_briefing.py`**
  - `BriefingService` dataclass + 全部字段
- [x] **3.2 实现 `_select_evidence(session, entity, window_end)` 内部方法**
  - JOIN entity_mentions / normalized_messages
  - ORDER BY engagement DESC, random()（无 engagement 退化为随机）
  - LIMIT evidence_count（默认 10）
- [x] **3.3 实现 `_render_prompt(entity, evidence, cooccur_hint)` 内部方法**
  - 加载 prompt_path 模板
  - 替换 4 个占位符；evidence 用 `[author @ ts] text[:300]` 拼接
- [x] **3.4 实现 `_parse_json(response)` 内部方法**
  - 剥 markdown 代码块（容错 qwen3 偶发包裹）
  - json.loads + 字段标准化（5 字段全归一化为 None / str / float）
  - 失败 raise ValueError
- [x] **3.5 实现 `run_once()` + `_generate_one()`**
  - 整轮异常隔离：单 entity 失败不影响其它
  - 整轮无论成败都更新 `_last_processed_window_end`，避免反复扫
- [x] **3.6 单元测试 `tests/test_l5_briefing.py`**（11 个用例）
  - test_select_evidence_top_n ✅
  - test_select_evidence_falls_back_to_random_when_no_engagement ✅
  - test_render_prompt_replaces_placeholders ✅
  - test_parse_json_valid ✅
  - test_parse_json_strips_markdown_code_block ✅（防御）
  - test_parse_json_invalid_raises ✅
  - test_skips_when_no_top_entities ✅
  - test_skips_already_briefed_entities ✅
  - test_per_entity_failure_isolated ✅
  - test_skips_when_window_unchanged ✅
  - test_low_growth_filtered_out ✅
- [x] **3.7 跑测试**
  - **168 passed**（157 + 11）

## Task 4：配置扩展

- [x] **4.1 改 `config/_legacy.py` 加 LLM briefing 配置（决策：放老链路分组）**
  - `ollama_model_level5: str = "qwen3:8b"`
  - `ollama_timeout_level5: int = 600`
- [x] **4.2 改 `config/_new.py` 加 briefing 业务配置**
  - `briefing_enabled / briefing_top_n / briefing_min_growth / briefing_evidence_count`
  - `briefing_min_growth` 默认从 spec 的 30.0 调到 5.0（与 alert_growth_threshold 对齐当前数据流量）
- [x] **4.3 验证配置加载**
  - 实测 `True / 5 / 5.0 / 10 / "qwen3:8b" / 600` 全部正确
- [x] **4.4 跑测试**
  - **168 passed**

## Task 5：main.py 注入

- [x] **5.1 改 `main.py`**
  - 新增 Step 5f（在 Realtime 之后、Jobs 启动之前）
  - 构造 `OllamaClient(model=ollama_model_level5)` + `BriefingService`
  - 共享 hotness_repo / mentions_repo / normalized_repo / cooccur_repo
  - 加入 `new_services` 列表，调度顺序：normalizer → extractor → hotness ×3
    → cooccur → alert → briefing（最后）
- [x] **5.2 配置缺失即降级**
  - `briefing_enabled=False` → 整服务不构造，log INFO 跳过
  - Ollama 不可达 → BriefingService 仍构造，但 run_once 单 entity log warning
    后跳过，不影响 hotness/alert
- [x] **5.3 验证 main.py 仍能 import**
  - `python -c "import main"` 干净通过
- [x] **5.4 跑测试**
  - **168 passed**

## Task 6：Telegram 推送集成

> ✅ **状态：已完成（2026-05-14 用户决策直接推进）**
>
> 关键时序设计：worker 调度顺序是 `hotness → cooccur → alert → briefing`，
> alert 发出去时**当前 window** 的 briefing 还没生成。本任务用
> `fetch_latest_for_entity(entity, since=window_end - 1h)` 兜底——
> 查最近 1h 内任何一条 briefing，让"持续热点"在第二轮 alert 起就带 briefing。
> 首次上榜的 entity 无 briefing 自动降级（spec Req 8.3：告警永远不等 briefing）。
>
> 端到端 smoke 已验证：临时插假 hotness + 假 briefing → alert_service.run_once
> 触发 → Telegram 真收到带 `📰` 行的消息。脚本用完已删除。

- [x] **6.1 改 `services/l2_alert_trigger.py`**
  - 加可选字段 `briefing_repo: Optional[object] = None`（默认 None 向后兼容）
  - 抽出 `_render_briefing_suffix(rec)` 方法做 briefing 查询 + 格式化
  - `_render_message` 改成 `base + "\n" + suffix`（suffix 空串时不追加）
  - `_BRIEFING_LOOKBACK_HOURS = 1`（模块常量，便于未来调整）
  - briefing_repo 加新方法 `fetch_latest_for_entity(entity, since)`
  - 任何 briefing 查询异常都被 try/except 吞掉 + log warning，不影响告警发送
- [x] **6.2 加测试**（+3 用例，spec 要求 +2，多了 1 个健壮性用例）
  - test_alert_message_includes_briefing ✅（命中 → 含 📰 行）
  - test_alert_message_falls_back_when_no_briefing ✅（未命中 → 走原模板）
  - test_alert_message_briefing_query_failure_does_not_break_alert ✅
    （DB 异常 → 告警照常发，不挂）
- [x] **6.3 跑测试**
  - **171 passed**（168 + 3）
- [x] **6.4 main.py 注入 briefing_repo**（spec 没列但必做）
  - `if settings.briefing_enabled:` 时构造 `BriefingsRepo()` 注入 alert_service
  - 启动日志追加 `briefing=ON/OFF` 标识

## Task 7：本地端到端验收

- [x] **7.1 重启服务**
  - 临时 smoke 脚本 `scripts/smoke_briefing_e2e.py` 验证：
    `BriefingService.run_once()` 跑通，22.6s 单 entity 耗时
  - 启动日志确认 `BriefingService 启动：top_n=5 min_growth=5.0 ...`
  - smoke 脚本用完已删除
- [x] **7.2 等下一个 quarter，SQL 验证 briefings 已生成**
  - 实测 PRATT 简报已写入：narrative='政治人物 PRATT' / sentiment='bullish' /
    confidence=0.90 / evidence=1 条
  - 注：当前数据流量下 hotness 榜实体 growth 普遍 < 5，需要等流量起来或
    临时调小 `briefing_min_growth` 才能定期触发
- [x] **7.3 看下一条 Telegram 告警是否带了 briefing 字段**
  - 端到端 smoke 验证：插假 hotness（PRATT_SMOKE growth=99）+ 假 briefing
    → alert_service.run_once → Telegram 真收到含 `📰 政治人物 PRATT | 洛杉矶
    市长竞选大幅上升` 的消息
  - 临时脚本用完已删除

## Task 8：文档

- [x] **8.1 改 `docs/operations_guide.md` §6.5 LLM 简报调参**
  - 新加 §6.5 整段：协同图 / 是什么不是什么 / 调参速查 / SQL 查表 /
    评估方法 / 日志关键字 / 常见问题
  - §6 速查表追加 4 行 briefing 调参
  - §2 启动日志样例追加 `BriefingService 启动` 一行
- [x] **8.2 加 `docs/faq_design_decisions.md` Q11**
  - Q11.1 重新定义硬约束：信号产生链路零 LLM
  - Q11.2 为什么不让 LLM 替代 hotness（稳定性 / ROI / 噪音抑制 三角度）
  - Q11.3 LLM 简报的产品价值（最后一公里解释）
  - Q11.4 处理幻觉的 evidence_msg_ids 审计路径
  - Q11.5 evidence 排序策略
  - Q11.6 何时开/何时关
  - Q11.7 一句话结论

## 执行顺序与依赖图

```
Task 0 (基线 157 + Ollama 可达性 + 决策)
   └─► Task 1 (schema 迁移 + ORM + repo)
           └─► Task 2 (prompt 工程 + 5 entity 实测 100% 合法)
                   └─► Task 3 (BriefingService + 11 测试 → 168)
                           └─► Task 4 (配置)
                                   └─► Task 5 (main.py 注入)
                                           └─► Task 7 (端到端验收)
                                                   └─► Task 8 (文档)

Task 6 (Telegram 集成) ⏸️ 暂缓 1~2 周观察后再启用
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
  main.py                            +Step 5f：BriefingService 构造
  docs/operations_guide.md           +§6.5 + 速查表 4 行 + 启动日志样例 1 行
  docs/faq_design_decisions.md       +Q11
  tests/test_models.py               +entity_briefings 进 expected 集合
  .kiro/specs/phase2-llm-briefing/tasks.md  +勾选所有项

不动（按用户决策暂缓 Task 6）：
  services/l2_alert_trigger.py       未改 _render_message
  tests/test_l2_alert_trigger.py     未加 +2 cases

测试基线：157 → 168 passed（+11，含 1 个 markdown 剥离防御用例，0 回归）
新增能力：热点实体的"为什么热"自动归纳 + Phase 1/2.x"零 LLM"硬约束精确化为
        "信号产生链路零 LLM"
```

## 风险与未决议题（已在 design.md / Q11 解决）

| 风险 | 优先级 | 实际处理 |
|---|---|---|
| **零 LLM 硬约束被突破** | 高 | Q11.1 重新定义为"信号产生链路零 LLM"；briefing 是叶子节点不被任何信号链路读 |
| LLM 输出 JSON 格式不稳定 | 高 | prompt 工程 + 实测 5/5=100% 合法；剥 markdown 兜底 + 解析失败不写表 |
| LLM 推理慢（CPU 模式）影响 hotness | 中 | 调度顺序排最后；Top-N=5 控制单轮 ~2.5 分钟；worker 是异步 30s 轮询，能接受 |
| qwen3:8b 中文叙事归纳能力够吗 | 中 | 实测 5 entity，narrative/catalyst 抓得到主题；continue 用 8b 即可 |
| LLM 幻觉：编造 evidence 之外的内容 | 中 | prompt 强调"只能基于消息内容"+ evidence_msg_ids 字段记录用了哪些消息（Q11.4 审计路径） |
| Briefing 信息过期 | 低 | 每 15 分钟刷新；UNIQUE 约束保证同窗口同实体一次 |
| Telegram 消息长度超限（4096 字符）| — | Task 6 暂缓不涉及 |
| 失败的 briefing 占据"已处理"位置 | 低 | 不写表 = ON CONFLICT 不触发 = 下一轮 window 可重试 |

---

*文档版本：v1.1（实施完成版）*
*预估工时实际：编码 4 小时（spec 估 3~5 天，因为 prompt 工程一次性 100% 合法 + 设计沿用现有架构）*
*对早期热点发现的契合度：⭐⭐ 不创造新信号——是"看完榜单后的辅助"*
