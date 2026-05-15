# Phase 2 · Task 2.7 L5 LLM 定向简报 · Requirements

> Phase 2 路线图里**最后一个**子任务，也是**唯一一个明确突破"零 LLM 硬约束"
> 的任务**。在已经稳定产出的热度榜单 / 共现网络 / 聚类代表之上叠加一层 LLM 解释——
> 让你看榜单时不仅知道"什么热"，还能直接读到"为什么热"。

## 背景

Phase 1 / Phase 2.1~2.6 上线后，你能在 `hotness_snapshots` 表里看到这种结果：

```
window_end=2026-05-13 10:15  window_type=1h
  rank=1  EIGEN   growth=20.3  cross=3  count_short=42
  rank=2  ETHFI   growth=12.8  cross=2  count_short=18
  rank=3  WIFHAT  growth=10.1  cross=1  count_short=11
```

**问题**：你能看到 EIGEN 突然热了，但**不知道为什么热**——这一条信息当前系统
完全无法回答。要回答它，今天唯一的办法是：

1. 自己去 `normalized_messages` 表里 `WHERE entity='EIGEN' AND ts > now() - 1h`
   读 100 条原文
2. 或者切到推特，搜 `$EIGEN` 看看在聊什么

两种办法在白天都还行，但在睡前 / 上班路上 / 离开终端时都做不到。本任务的目标
是把这个"读原文 / 搜推特"的过程**自动化为一条 LLM 调用**：当某 entity 进入
hotness Top-N 时，调 LLM 读它的代表性消息，输出结构化 JSON 简报：

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

这份 JSON 落到一张新表 `entity_briefings`，可以直接喂给 Telegram 告警的消息模板，
让推送从「`$EIGEN growth 20×`」升级到「`$EIGEN ↑20× | EigenLayer v2.0 上线，
Restaking 复苏`」。

## 关于"零 LLM 硬约束"被突破

Phase 1 与 Phase 2.1~2.6 严格遵守"零 LLM"硬约束——这条约束的本质是
**"信号产生链路绝不被 LLM 幻觉污染"**：hotness 公式、SimHash 去重、共现 PMI、
聚类相似度全是确定性算法，可重放、可回归、可单测。

本任务**明确突破**这条约束，但只在"信号已经产生"之后调 LLM 加解释，**不让
LLM 反向影响信号本身**：hotness 公式不读 briefing、共现网络不读 briefing、
告警冷却 dict 不读 briefing。这与老链路 Level1Service / Level2Service 调
Ollama 做摘要的本质相同——LLM 是辅助工具，不是核心管道。

详细论证见 design.md §9。

## 用户角色

- **唯一用户**：项目所有者（你，单人开发者，做早期热点发现）
- **设备**：Telegram App + 终端读 SQL
- **使用场景**：白天看盘 / 夜间睡觉，不可能 24h 守着 PostgreSQL，更不可能
  每次看到一个新 entity 都立刻去推特搜

### 关于 ROI 的诚实声明

用户已经表示不要"花里胡哨的"，倾向"直接去推特搜下就知道了"。所以本任务在
四个 Phase 2 子任务里 **ROI 最低**，**应该最后做**或**完全不做**。

本任务存在的价值是把「手动去推特搜」自动化为「自动推送时附带解释」。如果你
能 30 秒内推特搜出原因，本任务的边际收益不高——这一点会在 design §1 反复强调，
也是 Task 0.3 让用户在方案 A / B / C 之间做决策的根本原因。

## 边界与非目标

### 包含

1. 一张新表 `entity_briefings`（DDL + ORM + repo）
2. 一个新服务 `BriefingService`（每 15 分钟整点对齐运行一次）
3. 一份新 prompt 模板 `prompts/level5_briefing.txt`
4. 复用现有 `OllamaClient`，新增 level5 模型 / 超时配置
5. 与 `AlertTriggerService` 的**可选**集成：让 Telegram 推送附带 briefing 字段
6. 单元测试 + 集成测试覆盖核心路径

### 不包含（Phase 3 / 永不实施）

1. ❌ 用 LLM 替代 hotness 公式 / 替代信号产生链路（与硬约束#2 冲突）
2. ❌ 多语言 briefing（只输出中文）
3. ❌ 历史 briefing 趋势分析（briefing 是窗口快照，不做长期聚合）
4. ❌ briefing 质量自动评估（Phase 3 真有需求时再做"人工标注 + 评分"流程）
5. ❌ Web 面板展示 briefing（Phase 3）
6. ❌ briefing 的版本回溯 / Diff（同 entity 同 window UNIQUE，无版本概念）
7. ❌ 强制走 LLM Function Calling / Tool Use（qwen3:8b 支持不稳定，
   退化为"prompt 里要求输出 JSON"）

## Requirements

### Req 1：数据模型

1.1 应新增表 `entity_briefings`，DDL 如下（详见 design §3.1）：

```sql
CREATE TABLE entity_briefings (
    id              BIGSERIAL PRIMARY KEY,
    entity          VARCHAR(128) NOT NULL,
    window_end      TIMESTAMPTZ  NOT NULL,
    narrative       TEXT,
    catalyst        TEXT,
    fund_logic      TEXT,
    sentiment       VARCHAR(16),
    confidence      DOUBLE PRECISION,
    evidence_msg_ids BIGINT[]    NOT NULL,
    raw_response    JSONB,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_entity_briefings_entity_window UNIQUE (entity, window_end)
);
CREATE INDEX idx_entity_briefings_window_end ON entity_briefings(window_end DESC);
```

1.2 应新增 ORM 模型 `EntityBriefing` 在 `db/models.py`，字段语义对齐
Phase 1 已有模型（`DateTime(timezone=True)` / `BIGSERIAL` / `server_default=func.now()`）

1.3 应新增 repo `db/repositories/briefings_repo.py`，公开 3 个方法：
- `upsert_one(session, entity, window_end, fields) -> int`：ON CONFLICT DO NOTHING
- `fetch_for_entity(session, entity, window_end) -> EntityBriefing | None`
- `fetch_recent(session, window_end, limit=20) -> list[EntityBriefing]`

1.4 `raw_response` 字段必须**始终**保存 LLM 原始 JSON 字符串（即使解析成功后
结构化字段已经填好），用于事后审计 / 调试 / 改 prompt 时回放。

1.5 `evidence_msg_ids` 字段必须记录本次喂给 LLM 的所有 `normalized_messages.id`，
用于事后审计「LLM 是否在 evidence 之外编造内容」（幻觉检测的关键证据）。

1.6 `(entity, window_end)` 唯一约束保证幂等：同窗口同实体多次调用走 ON CONFLICT
DO NOTHING（**不覆盖**——一条窗口快照只生成一次，避免 LLM 输出抖动）。

### Req 2：BriefingService 接口

2.1 应新增 `services/l5_briefing.py`，提供 `BriefingService` dataclass。

2.2 构造参数：
- `db: Database`
- `hotness_repo: HotnessSnapshotsRepo`
- `mentions_repo: EntityMentionsRepo`
- `normalized_repo: NormalizedMessagesRepo`
- `briefing_repo: EntityBriefingsRepo`
- `ollama: OllamaClient`（已用 level5 模型 / 超时构造）
- `prompt_path: Path`
- `top_n: int = 5`
- `min_growth: float = 30.0`
- `evidence_count: int = 10`
- `timezone: ZoneInfo`

2.3 公开方法 `run_once() -> bool`：
- 取最新 1h 榜的 `window_end`，与 `_last_processed_window_end` 比较，
  相同 → 返回 False（与 Phase 2.2 AlertTriggerService 同款幂等模式）
- 拉 Top-N 实体，过滤 `growth_rate < min_growth` 的
- 过滤 `briefing_repo.fetch_for_entity(...)` 已存在的（同窗口已生成过）
- 对剩下的每个 entity：调内部方法生成 briefing 并 UPSERT
- 任意一个 entity 失败不阻塞其它（per-entity try/except）
- 至少成功一个返回 True，全失败 / 全跳过返回 False

2.4 内部方法（不对外暴露）：
- `_select_evidence(session, entity, window_end) -> list[NormalizedMessage]`
- `_render_prompt(entity, evidence) -> str`
- `_parse_json(response: str) -> dict`（解析失败 raise ValueError）

2.5 状态字段（进程内，不持久化）：
- `_last_processed_window_end: Optional[datetime]`：避免同一窗口反复扫描

### Req 3：Prompt 模板与 JSON 输出格式

3.1 应新增 `prompts/level5_briefing.txt`，模板格式与现有 `level1_*.txt` /
`level2_*.txt` 保持风格一致：纯文本，`{entity}` / `{n_msgs}` / `{messages}`
三个占位符。

3.2 prompt 必须**显式约束**：
- "只输出一个 JSON 对象，不要任何 markdown 代码块包裹（不要 \`\`\`json）"
- "不要解释 / 不要思考过程 / 不要前后说明文字"
- "只能基于下面的消息内容做归纳，不要编造没出现的事件"

3.3 期望输出 JSON 结构：

```json
{
  "narrative":  "≤ 20 字 / 当前归属叙事 / 例：Restaking 复苏",
  "catalyst":   "≤ 50 字 / 具体催化事件 / 例：EigenLayer v2.0 上线",
  "fund_logic": "≤ 50 字 / 资金或基本面逻辑",
  "sentiment":  "bullish | bearish | neutral",
  "confidence": 0.0-1.0
}
```

任一字段无法判断时填 `null`（而不是猜）。

3.4 `evidence_msg_ids` 字段不让 LLM 输出，由 BriefingService 自己注入
（拿到 evidence 时就知道；让 LLM 输出反而会有"编造 ID"风险）。

### Req 4：Evidence 选择策略

4.1 候选集：`entity_mentions` 表里 `entity=X AND ts ∈ [window_end - 1h, window_end)`
的所有提及。

4.2 排序策略（按优先级）：

1. **如果 `mentions.engagement` 全表存在非零值（待 Phase 2.x 抓取层升级支持）**：
   先按 `engagement DESC` 排序，取 Top-N
2. **否则（Phase 2 当前真实情况，三源 engagement 全为 0）**：随机抽样 N 条
3. **未来与 Phase 2.6 协同**：如果 Phase 2.6（Embedding 聚类）已上线，
   按"每个 cluster 取 1 条代表"的策略抽样，避免 LLM 看到 10 条几乎相同的
   重复消息浪费上下文

4.3 evidence 上限：默认 `evidence_count=10`，避免 prompt 超 qwen3:8b 的
`num_ctx=16384` 上限（10 条 × 平均 200 字 ≈ 2000 token，加上 prompt 模板和
输出留白绰绰有余）。

4.4 evidence 内容剪裁：每条只用 `(author, posted_at, text[:300])` 三段，
长文本截断到 300 字符（Twitter 一条 280；Discord / 币安广场偶有更长，截断
即可）。

### Req 5：与 Ollama 集成

5.1 应**复用**现有 `llm/ollama_client.py` 的 `OllamaClient`——禁止新建第二个
LLM 客户端类（与硬约束#3 冲突）。

5.2 应新增 level5 配置（详见 Req 6）：`ollama_model_level5` 默认 `"qwen3:8b"`，
`ollama_timeout_level5` 默认 `600`。

5.3 LLM 调用失败的处理：
- 超时 / 连接错 / Ollama 返回空 / JSON 解析失败 → log.warning，**不写表**，
  不进入 `_last_processed_window_end` 标记（**等下一轮重试**）
- 但**整轮**完成后无论成功失败都要更新 `_last_processed_window_end`，避免
  反复扫描同一窗口（已处理实体走 `fetch_for_entity` 命中跳过即可）

5.4 单实体异常隔离：每次 `ollama.chat(prompt)` 必须包在独立 try/except 里，
不让 entity A 的 LLM 失败影响 entity B 的处理。

5.5 调用顺序：BriefingService 必须排在所有写库 service（Normalizer /
EntityExtractor / 全部 HotnessService / AlertTriggerService）**之后**——
LLM 推理慢（CPU 模式 ~10s/次，Top-5 一轮 ~50s），不能阻塞前面的实时管道。

### Req 6：配置（决策位置）

6.1 配置分组决策（详见 design §3.6）：

**推荐方案：新建 `config/_llm.py`** 把 ollama_model_level1 / level2 / level5
+ 各自 timeout 全部迁过去，理由：
- `_legacy.py` 当前的命名暗示"老链路"，把 level5（新链路 LLM）放进去会破坏分类
- 集中管理 LLM 配置便于未来扩展（Phase 3 加 level6 / 切模型）
- 改动可控：只需改 `config/settings.py` 的多继承列表 + level1/2 引用从
  `LegacySettings` 改成 `LLMSettings`

**备选方案：放进现有 `config/_legacy.py`** ——工作量更小（只加 2 个字段，不动
其他文件），但语义错位。如果用户决定先快后好，可以先用备选；后续真有 Phase 3
LLM 配置增多时再做迁移。

6.2 应在某个配置分组（按 6.1 决策）新增：
- `ollama_model_level5: str = "qwen3:8b"`
- `ollama_timeout_level5: int = 600`

6.3 应在 `config/_new.py` 新增 briefing 业务配置：
- `briefing_enabled: bool = True`
- `briefing_top_n: int = 5`
- `briefing_min_growth: float = 30.0`
- `briefing_evidence_count: int = 10`

6.4 `briefing_enabled=False` → main.py 跳过 BriefingService 构造（零运行时开销）。

6.5 `ollama_base_url` 不可达 → BriefingService 仍构造，但 run_once 全失败
log.error，不影响其它 service。

### Req 7：与 Worker 集成

7.1 `main.py` 在新链路初始化阶段构造 `BriefingService`（如 `briefing_enabled=True`）。

7.2 注入 `Jobs.new_services`，与 Normalizer / EntityExtractor / 全部
HotnessService / AlertTriggerService 共用同一 worker 线程。

7.3 调度顺序（必须严格遵守）：

```
NormalizerService
  → EntityExtractor
    → HotnessService(1h) → HotnessService(6h) → HotnessService(24h)
      → AlertTriggerService
        → BriefingService    ← ★ 必须放最后（推理 ~50s，不能阻塞前面）
```

7.4 任意一轮 `BriefingService.run_once` 抛异常 → Jobs 已有的异常隔离机制
兜住（与 Phase 2.2 同），不影响 hotness / alert / 下一轮 worker 循环。

### Req 8：与 AlertTriggerService 集成（可选）

> ⚠️ 本 Req 标记为 **可选** —— Task 6 在 tasks.md 里也是可选项。
> 先把 briefings 表跑起来观察 1~2 周，确认 LLM 输出质量稳定后再做集成；
> 否则可能反过来拉低 Telegram 推送的可读性。

8.1 应在 `services/l2_alert_trigger.py` 的 `_render_message` 方法里追加
"briefing 字段渲染"：
- `_alert_records` 渲染时尝试 `briefing_repo.fetch_for_entity(entity, window_end)`
- 命中 → 在原模板基础上追加一段「📰 {narrative} | {catalyst}」
- 未命中 → 走原模板（**优雅降级**，与硬约束#2 一致）

8.2 `AlertTriggerService` 增加 `briefing_repo: BriefingsRepo | None = None` 字段，
`None` 时跳过 briefing 查询（Phase 2.2 现有调用方传 `None` 即可，向后兼容）。

8.3 不强制 briefing 字段——「告警永远不应等待 briefing」是原则。

### Req 9：测试覆盖

9.1 应新增 `tests/test_l5_briefing.py`，覆盖 10 个用例（详见 design §5）：
1. evidence 选择按 engagement Top-N
2. evidence 在无 engagement 时随机抽样
3. prompt 模板正确替换三个占位符
4. JSON 解析合法响应成功
5. JSON 解析非法响应 raise ValueError
6. 无 Top entity 时跳过
7. 已有 briefing 的 entity 不重复调 LLM
8. 单 entity 失败不影响其它（per-entity 异常隔离）
9. 同窗口已处理时跳过
10. growth < min_growth 的实体被过滤

9.2 应在 `tests/test_l2_alert_trigger.py` 追加 2 个用例（仅 Task 6 启用集成时）：
1. 命中 briefing 时消息含 narrative / catalyst
2. 未命中 briefing 时降级到原模板

9.3 测试基线：
- 起点 **135 passed**（Phase 2.1 完工状态）
- 落点 **147 passed**（+12，0 回归）

9.4 LLM 必须 mock：禁止任何测试真的调 Ollama（CI 不可达 + 推理慢 + 输出不稳定）。

### Req 10：日志规范

10.1 INFO："briefing generated: entity=EIGEN narrative='Restaking 复苏'
catalyst='...' confidence=0.85"

10.2 INFO："briefing skipped: entity=ETH 同窗口已生成"

10.3 WARNING："briefing JSON parse failed: entity=SOL 原始响应前 200 字: ..."

10.4 WARNING："briefing LLM call failed: entity=WIFHAT err=<...>"

10.5 INFO："briefing skipped: latest window already processed"

10.6 启动 INFO："BriefingService 启动：top_n=5 min_growth=30.0 evidence_count=10
model={level5_model}"

## Success Metrics

### 阶段验收（部署后 24 小时内）

- [ ] **配置生效**：`./scripts/restart.sh` 启动后日志含
      "BriefingService 启动：top_n=5 min_growth=30.0 ..."
- [ ] **测试基线**：`pytest` 仍 100% pass，达到 147 passed
- [ ] **零 LLM 约束的等价物保留**：BriefingService 调 LLM 但产出**不**反向
      影响 hotness / cooccur / 任何信号产生链路（用 grep 验证：
      `services/l2_hotness.py` 不 import `briefings_repo`）
- [ ] **JSON 合法率**：观察 24h 内 Top-5 实体的 briefing 生成情况，JSON 解析
      合法率 ≥ 90%（< 90% 就回炉调 prompt）

### 业务验收（部署后 7~14 天内）

- [ ] **LLM 输出质量人工评估**：随机抽 10 条 briefing，由用户人工评估：
      narrative 是否抓到主题（≥ 7/10 算合格）、catalyst 是否准确（≥ 7/10 算合格）
- [ ] **不影响主流程**：briefings 表的写入失败 / Ollama 不可达时，hotness 与
      Telegram 告警继续工作（grep 错误日志确认）
- [ ] **CPU 推理耗时可控**：每轮 BriefingService.run_once 实测 < 90s，
      没有让 worker 主循环积压

### 反向验证（确认没引入新风险）

- [ ] **信号产生链路零 LLM**：`grep -r 'OllamaClient\|ollama' services/l0_*.py
      services/l1_*.py services/l2_hotness.py` 应仍然 0 命中
- [ ] **briefing_enabled=False 路径无副作用**：把开关关掉重启服务，跑 24h，
      hotness / alert 完全正常
- [ ] **LLM 调用失败时优雅降级**：手动断开 Ollama 服务跑 1 轮，BriefingService
      log warning 后继续，不抛异常给 worker

## 硬约束（不可妥协）

> ⚠️ **本任务**第 1 条硬约束**明确突破**——这是 Phase 2 路线图里唯一一个调用
> LLM 的子任务。详细论证见 design.md §9。

### 1. ~~零 LLM~~ → **"信号产生链路零 LLM"**（重定义）

- **Phase 1 / Phase 2.1~2.6 的硬约束原文**："新链路严格不 import
  `llm/ollama_client.py`"——目的是让信号产生链路稳定可靠，不被 LLM 幻觉污染
- **本任务的明确突破**：BriefingService 引入 LLM，但**只在信号产生后加解释**，
  不让 LLM 反向影响：
  - hotness 公式（`l2_hotness.py` 不读 briefing）
  - 共现网络 / Phase 2.5（不读 briefing）
  - 聚类相似度 / Phase 2.6（不读 briefing）
  - 告警冷却 dict / Phase 2.2（不读 briefing；可选读它做消息渲染）
- **重定义后的硬约束**：「**信号产生**链路零 LLM」（详见 design §9 论证）
- **类比**：老链路 Level1Service / Level2Service 也调 LLM，本任务跟它们的设计
  原则一致——LLM 是辅助工具，不是核心管道

### 2. 不阻塞主流程

- BriefingService 失败 / Ollama 不可达 / JSON 解析失败 → 不影响 hotness / alert
  主流程的写入与 worker 主循环
- BriefingService 排在 worker 链路最后（CPU 推理 ~50s 不能拖累实时管道）

### 3. 不引入新依赖

- 复用现有 `OllamaClient`，不新建 LLM 客户端类
- 不新增 `requirements.txt` 依赖

### 4. 不破坏向后兼容

- `entity_briefings` 是新增表，不动现有表
- `AlertTriggerService` 集成是**可选项**，未启用时与 Phase 2.2 行为 100% 等价
- 现有 135 个测试 0 回归

### 5. 配置缺失即降级

- `briefing_enabled=False` → 整个 BriefingService 不构造（零开销）
- `ollama_base_url` 不可达 → 每轮 log.warning，但服务正常继续

## 依赖与风险

### 依赖

- **Ollama 服务可达**：服务器能 `curl http://192.168.1.219:11434/api/tags`
  且看到 `qwen3:8b` 模型已 pull
- **Phase 1 hotness_snapshots 表**（已有）
- **Phase 1 normalized_messages / entity_mentions 表**（已有）
- **Phase 2.1 多窗口热度**（已上线，本任务读 1h 榜，与多窗口共存）
- **Phase 2.2 Telegram 告警**（已上线，本任务 Task 6 可选集成它）
- 可选：**Phase 2.5 共现网络** / **Phase 2.6 聚类**（如已上线，
  evidence 选择策略和 prompt narrative hint 会更稳）

### 风险

| 风险 | 概率 | 缓解 |
|---|---|---|
| **LLM JSON 输出不稳定**（qwen3:8b 容易加 \`\`\`json 包裹）| 高 | prompt 工程显式禁止 markdown；解析失败不写表 + 下轮重试；实测合法率 < 90% 就回炉 |
| **LLM 幻觉**（编造 evidence 之外的内容）| 高 | prompt 强调"只能基于消息内容"；evidence_msg_ids 字段记录用了哪些消息便于审计；用户应人工评估 1~2 周再决定是否长期开启 |
| **CPU 推理慢拖累 worker**（每次 ~10s，Top-5 一轮 ~50s）| 中 | 调度顺序固定排最后；Top-N 默认 5 控制单轮总耗时；HotnessService 是 15min 整点对齐，1 分钟内的 worker 延迟可接受 |
| **零 LLM 硬约束突破被误解**（未来其他 service 也开始随便引 LLM）| 中 | design §9 明确论证、docs/faq Q11 记录原则、code review 时严格审 import |
| **本任务的 ROI 本身不高**（用户可手动推特搜）| 中 | 这就是 Task 0.3 让用户在方案 A/B/C 之间决策的原因；可以**完全不做**，让 spec 停在文档阶段 |
| **briefing 内容过期** | 低 | 每 15 分钟刷新，UNIQUE 约束保证同窗口同实体只生成一次 |
| **Telegram 消息长度超限**（追加 briefing 后超 4096）| 低 | TelegramClient 已有 4000 字符截断逻辑（Phase 2.2 Req 1.3）|
| **失败的 briefing 占据"已处理"位置** | 低 | 不写表 = ON CONFLICT 不触发 = 下一轮 entity 仍可重试 |

---

*文档版本：v1.0*
*基于：Phase 2 路线图 §10 L5 LLM 定向简报*
*预估工时：实施前补 prompt 工程 1~2 天 + 编码 3~5 天 ≈ 1 周*
*本任务在 Phase 2 路线图里 ROI 最低，建议放在最后或完全跳过*
