# Requirements Document

## Introduction

本 spec 是「加密叙事雷达（Crypto Narrative Radar）」项目的 **Phase 1 · 止血期** 需求文档。

### 项目定位

加密叙事雷达不是"新闻摘要器"，而是一台 **加密注意力流动探测器**。它的核心使命是：从海量社交噪音中，最快发现正在从冷到热的新共识——在市场看懂之前，先看到。

完整系统由 L0~L6 共 7 层漏斗组成，详见 `文档/终极设计文档.md`。本 spec **只覆盖 Phase 1（止血期，约 1 周）**，不涉及 Phase 2/3/4。Phase 1 的全部产出是一份"每 15 分钟刷新的 Top-K 增长最快实体排行榜"，**全程不调用任何 LLM**。

### Phase 1 的边界

Phase 1 在现有代码基座（`db/models.py` 5 张表、`services/prefilter.py`、`services/level1_service.py`、`services/level2_service.py`、`scheduler/jobs.py` 单线程串行 worker）之上**只做扩展，不做替换**。现有的 level1 / level2 老链路在 Phase 1 期间继续跑，作为"并行验证基线"。Phase 1 交付后，老链路是否下线由 Phase 3 决定，不在本 spec 范围。

### 冷启动期望管理

Phase 1 首次部署后**不会**立刻产出有效排行榜，需要经历以下冷启动过程，运维与验收方必须理解：

1. **词典首次维护（T=0）**：首次启动前必须先维护 `dictionaries/*.yaml` 词典 v1（至少把现有 `prefilter.py` 硬编码的 60+ 英文词 + 7 中文词迁移过去）；若词典文件全部为空，服务虽可启动（见 Req 3.6），但 Entity_Extractor 将无法产出任何词典命中实体，排行榜只会包含正则命中的 `$TICKER` / 合约地址等。
2. **基线数据积累（T=0 ~ T+N 小时）**：`entity_mentions` 表累积数据量不足 100 条时，Hotness_Service 按 Req 7.7 主动降级，跳过本轮计算并记录 INFO 日志。这段时间**没有排行榜产出是预期行为**，不是 bug。根据三源实际流量，通常需要 2~12 小时才能累积到可产出有效排行榜的数据量。
3. **首份有效排行榜（T+N 小时后）**：当基线数据充足且至少经过一个 15 分钟整点调度后，`hotness_snapshots` 才会写入第一份 20 条的 Top-K 数据。Gate 1 的 72 小时验收窗口应从**首份有效排行榜产出时刻**起算，而不是从进程启动时刻起算。

### 核心哲学（三条）

1. **热点 = 增长率，不是绝对值**：BTC 永远高频，但它不是 alpha。真正的信号是某个词 7 天均值每小时 20 次提及突然变成 900 次——45 倍才是信号。Phase 1 的排行榜必须基于增长率排序，不能基于绝对提及量。
2. **LLM 只在最后一公里介入**：统计的活让代码干（更快、更准、零成本）。Phase 1 作为整个系统的地基，**完全不允许**调用任何 LLM，以证明"没有 LLM 也能产出价值"。
3. **复杂度是第一风险**：个人开发者最大的敌人不是技术难度，是系统复杂度。Phase 1 必须严格砍到最小闭环，任何"看起来很想做"的扩展（共现网络、Embedding、聚类、Telegram、Streamlit）一律归入 Non-Goals，等后续 Phase 再做。

## Glossary

- **Normalizer_Service**: L0 归一化服务，消费 `twitter_posts` / `binance_square_posts` / `discord_messages` 三张原始表，产出统一结构的 `NormalizedMessage` 并写入 `normalized_messages` 表。对应模块 `services/l0_normalizer.py`。
- **Deduplicator**: L0 去重组件，使用 SimHash 对 `NormalizedMessage` 做近似去重。对应模块 `services/l0_dedup.py`。
- **Entity_Extractor**: L1 实体抽取服务，在 `services/prefilter.py` 产出结果的基础上挂实体标签，并写入 `entity_mentions`。对应模块 `services/l1_entity_extractor.py`，与改造后的 `services/prefilter.py` 协作。
- **Sliding_Counter**: L2 滑动窗口计数器，纯内存实现，维护每个实体在 `15min` / `1h` / `24h` / `7d` 等窗口内的提及次数。对应模块 `services/l2_sliding_counter.py`。
- **Hotness_Service**: L2 热度服务，每 15 分钟调用一次，基于 `Sliding_Counter` 与 `entity_mentions` 计算增长率和综合热度分，写入 `hotness_snapshots`。对应模块 `services/l2_hotness.py`。
- **Worker**: `scheduler/jobs.py` 中的单线程串行 worker。所有 service 在同一个线程循环里轮流执行，避免并发。
- **NormalizedMessage**: L0 归一化后的统一消息结构。字段包含 `raw_id` / `raw_source` / `text` / `author` / `author_weight` / `ts` / `engagement` / `simhash`。
- **Entity**: 被抽取的实体标签。类型（`entity_type`）为 `ticker`（如 `$BTC`）、`chain`（如 `Base`）、`narrative`（如 `AI_Agent`）、`project`、`kol` 之一。
- **增长率（growth_rate）**: `short_count / max(baseline_per_hour, SMOOTHING)`。其中 `short_count` 是短窗（默认 1 小时）内的实体提及次数，`baseline_per_hour` 是基线期（默认 7 天，排除最近 `short_hours`）的每小时平均提及次数，`SMOOTHING = 2.0` 是防止冷启动除零的平滑因子。
- **跨源数（cross_source）**: 某实体在短窗内被命中过的独立数据源个数（`raw_source` 的去重计数），取值范围 1~3。
- **综合热度分（final_score）**: Phase 1 定义为 `growth_rate * (1 + 0.3 * (cross_source - 1))`（即"增长率 × 跨源加权"两项，不含 engagement / kol / novelty，这些留给 Phase 2）。
- **Top-K 排行榜**: 每 15 分钟按 `final_score` 降序取前 20 名（K=20），写入 `hotness_snapshots`。
- **hotness_snapshots**: L2 产出的排行榜表，每条记录代表某个窗口结束时刻某实体的排名快照。
- **Dictionary_Files**: 放在 `dictionaries/` 目录下的 YAML 词典文件（`tickers.yaml` / `chains.yaml` / `narratives.yaml` / `kols.yaml`）。`prefilter.py` 原本硬编码的 `COIN_KEYWORDS_EN` / `COIN_KEYWORDS_ZH` 迁移到这里，`prefilter` 与 `Entity_Extractor` 共用同一份数据。
- **老链路**: 指现有的 `Level1Service` / `Level2Service` + `scheduler/jobs.py` 中已注入的调用链。Phase 1 内保持不动，继续跑。
- **Gate 1**: Phase 1 的验收门槛（见 Success Metrics 章节）。通过 Gate 1 后才有资格进入 Phase 2。

## Requirements

### Requirement 1: 三源消息归一化（Task 1.1）

**User Story:** 作为系统维护者，我希望三个原始表被统一归一化成同一种结构，以便下游 L1/L2 模块无差别消费，不用关心消息来自哪个平台。

#### Acceptance Criteria

1. THE Normalizer_Service SHALL 从 `twitter_posts` / `binance_square_posts` / `discord_messages` 三张表读取尚未归一化的记录（通过 `(raw_source, raw_id)` 在 `normalized_messages` 中不存在判定）。
2. THE Normalizer_Service SHALL 为每条读入的原始记录产出一条 `NormalizedMessage`，字段 `raw_id` / `raw_source` / `text` / `ts` 必须非空，其中 `raw_source` 取值为 `"twitter"` / `"binance_square"` / `"discord"` 之一。
3. WHEN 归一化 `DiscordMessage` 时，THE Normalizer_Service SHALL 将 `author` 字段设置为 `"#{channel_name} @{username}"` 格式（与现有 `DiscordMessage.author` 派生属性保持一致）。
4. THE Normalizer_Service SHALL 将 `ts` 统一转换为带时区的 UTC 时间写入 `normalized_messages.ts`（PostgreSQL `TIMESTAMPTZ`）。
5. IF 原始记录的 `content` 经 `strip()` 后长度为零，THEN THE Normalizer_Service SHALL 跳过该记录，不写入 `normalized_messages`，并记录一条 DEBUG 日志说明跳过原因。
6. THE Normalizer_Service SHALL 保证 `(raw_source, raw_id)` 组合在 `normalized_messages` 表中具有唯一性（通过 UNIQUE 约束 + ON CONFLICT DO NOTHING 实现幂等）。
7. WHEN 同一条原始记录被 Normalizer_Service 反复消费 N 次（N ≥ 2），THE `normalized_messages` 表 SHALL 最终保持恰好一条对应记录。
8. THE Normalizer_Service SHALL **不修改** `twitter_posts.is_summarized` / `binance_square_posts.is_summarized` / `discord_messages.is_summarized` 字段（该字段专属老链路 Level1Service 使用，Phase 1 期间必须保留原语义）。

### Requirement 2: SimHash 近似去重（Task 1.2）

**User Story:** 作为系统维护者，我希望复读机效应（同一内容在短时间内被大量转发/复制）被识别出来，以便下游统计不被重复内容污染。

#### Acceptance Criteria

1. THE Deduplicator SHALL 为每条 `NormalizedMessage` 计算 64 位 SimHash 指纹并写入 `normalized_messages.simhash`。
2. WHEN 新消息的 SimHash 与过去 24 小时内任一已入库消息的 SimHash 汉明距离 ≤ 3 时，THE Deduplicator SHALL 将该新消息标记为重复：设置 `is_duplicate = TRUE` 且 `dup_of` 指向最早的一条原版 `normalized_messages.id`。
3. IF 新消息未命中任何 24 小时窗口内的近似，THEN THE Deduplicator SHALL 将其标记为原版：`is_duplicate = FALSE` 且 `dup_of = NULL`。
4. THE Deduplicator SHALL 仅用"原版"消息（`is_duplicate = FALSE`）参与后续 L1 实体抽取与 L2 统计；重复消息照常入库以便追溯，但不进入统计管道。
5. THE Deduplicator SHALL 在服务启动时从 `normalized_messages` 重建最近 24 小时的 SimHash 索引（支持进程重启后恢复去重能力）。
6. THE Deduplicator SHALL 将汉明距离阈值、窗口小时数作为可配置参数暴露在 `config/settings.py` 中（默认值：阈值 3、窗口 24 小时）。

### Requirement 3: 词典外置到 YAML（Task 1.3）

**User Story:** 作为系统维护者，我希望词典从 Python 源码迁移到 `dictionaries/` 目录下的 YAML 文件，以便后续持续维护时不用改代码也不用重启服务。

#### Acceptance Criteria

1. THE Dictionary_Files SHALL 位于 `dictionaries/` 目录下，至少包含 `tickers.yaml` / `chains.yaml` / `narratives.yaml` / `kols.yaml` 四个文件。
2. THE `tickers.yaml` SHALL 覆盖现有 `services/prefilter.py` 中 `COIN_KEYWORDS_EN` 和 `COIN_KEYWORDS_ZH` 的全部词项，并为每个词项标注 `type`（如 `layer1` / `defi` / `meme` / `stablecoin`）与可选 `aliases`（同义中英文别名列表）。
3. THE `services/prefilter.py` SHALL 在模块加载时从 `Dictionary_Files` 读取词典，不再在源码中硬编码 `COIN_KEYWORDS_EN` / `COIN_KEYWORDS_ZH` 列表。
4. THE Entity_Extractor SHALL 与 `services/prefilter.py` 共享同一份词典数据（通过统一的加载模块，避免两处重复维护）。
5. IF Dictionary_Files 中任一文件格式非法（YAML 语法错误、必填字段缺失），THEN THE 词典加载模块 SHALL 在进程启动阶段抛出明确错误并阻止服务启动（目的是把格式性错误挡在启动检查阶段，不要推迟到运行时才暴露；Phase 1 不做词典热加载，启动检查通过后运行时不再重新读取 Dictionary_Files）。
6. WHERE Dictionary_Files 中某个文件 YAML 语法合法但内容为空（词条数为零），THE 词典加载模块 SHALL 允许服务正常启动，仅在 WARN 日志中提示"该词典为空"，不阻止启动。
7. THE 词典加载模块 SHALL 在加载完成后输出一条 INFO 日志，包含每个词典文件加载的词条数（便于人工 sanity check）。
8. WHERE 两个词典文件定义了同名 `entity`，THE 词典加载模块 SHALL 抛出明确错误指出冲突的 key 与所在文件。

### Requirement 4: 实体抽取扩展（Task 1.4）

**User Story:** 作为系统维护者，我希望每条原版 `NormalizedMessage` 被挂上 0~N 个实体标签，以便 L2 可以基于"单个实体"维度做统计。

#### Acceptance Criteria

1. THE `services/prefilter.py` 的 `FilterDecision` 数据结构 SHALL 扩展为 `FilterDecision(keep, reason, entities)`，其中 `entities` 为 `list[Entity]`，**必须带默认值 `field(default_factory=list)`**，以保证既有 `FilterDecision(True, "A:$symbol")` / `FilterDecision(False, "D:length<20")` 这类位置参数两元构造方式在老代码中继续可用，不产生 `TypeError`。
2. THE Entity_Extractor SHALL 对每条输入文本产出 `list[Entity]`，来源包括：
   - 正则命中：`$TICKER` / `0x` 开头的 EVM 合约地址 / Solana 合约地址 / `@handle`；
   - 词典命中：来自 `Dictionary_Files` 的 ticker / chain / narrative / kol 条目及其 aliases。
3. THE Entity 对象 SHALL 包含至少 `entity`（名称，如 `"$BTC"` 或 `"Base"`）、`entity_type`（`ticker` / `chain` / `narrative` / `project` / `kol` 之一）、`confidence`（取值集合为 `{1.0, 0.95}`：词典命中 = 1.0，正则命中 = 0.95；Phase 1 不允许其他取值）三个字段。
4. THE Entity_Extractor SHALL 在单条消息的 entity 列表内对 `entity` 字段去重（同一实体被正则与词典双命中时只保留一条，取较高 `confidence`）。
5. THE Entity_Extractor SHALL 对每条原版 `NormalizedMessage` 的每个 Entity 在 `entity_mentions` 表中写入一条记录，字段包含 `msg_id` / `entity` / `entity_type` / `raw_source` / `ts` / `confidence`。
6. WHERE 消息的 `author` 命中 `kols.yaml` 中的 kol 条目，THE Entity_Extractor SHALL 将对应 `entity_mentions` 记录的 `is_kol_mention` 标记为 `TRUE`（仅做标记，Phase 1 的 `final_score` 不使用该字段）。
7. IF 一条消息抽出 0 个 Entity，THEN THE Entity_Extractor SHALL 不向 `entity_mentions` 写入任何记录，但仍标记该消息已被 L1 处理过（通过 `normalized_messages.l1_processed_at` 时间戳或等效机制，具体存储方式在 design 阶段决定）。
8. THE Entity_Extractor SHALL 保证同一条 `NormalizedMessage` 被反复处理时，`entity_mentions` 表中对应记录不重复（通过幂等写入机制，具体实现在 design 阶段决定）。
9. THE Entity_Extractor SHALL **不修改** `services/prefilter.py` 现有的 `classify` 函数签名兼容性：老的 `services/level1_service.py` 调用 `prefilter.split(posts)` 的行为必须不受影响（老链路继续按 `keep` / `reason` 字段工作，忽略新增的 `entities` 字段）。

### Requirement 5: 数据库新表（Task 1.5）

**User Story:** 作为系统维护者，我希望 Phase 1 新增的三张表通过 ORM + 迁移脚本落到 PostgreSQL，以便服务代码能通过 SQLAlchemy Session 读写。

#### Acceptance Criteria

1. THE `db/models.py` SHALL 新增三个 ORM 模型类：`NormalizedMessage`、`EntityMention`、`HotnessSnapshot`，分别映射到 `normalized_messages` / `entity_mentions` / `hotness_snapshots` 三张物理表。
2. THE `normalized_messages` 表 SHALL 包含以下字段：`id` (BIGSERIAL PK) / `raw_source` (VARCHAR(32), NOT NULL) / `raw_id` (BIGINT, NOT NULL) / `text` (TEXT, NOT NULL) / `author` (VARCHAR(255)) / `author_weight` (FLOAT, DEFAULT 1.0) / `ts` (TIMESTAMPTZ, NOT NULL) / `engagement` (INTEGER, DEFAULT 0) / `simhash` (BIGINT) / `sentiment_score` (FLOAT, DEFAULT 0) / `is_duplicate` (BOOLEAN, DEFAULT FALSE) / `dup_of` (BIGINT) / `l1_processed_at` (TIMESTAMPTZ, NULL，用于落地 Req 4.7 "等效机制"：NULL 表示 L1 未处理，NOT NULL 表示 L1 已处理时间戳) / `created_at` (TIMESTAMPTZ, DEFAULT NOW())。
3. THE `normalized_messages` 表 SHALL 建立以下索引与约束：`UNIQUE(raw_source, raw_id)` / `INDEX(ts DESC)` / `INDEX(raw_source, ts DESC)` / `INDEX(simhash)` / `INDEX(is_duplicate, l1_processed_at)`（最后一条加速 Entity_Extractor 的 `WHERE is_duplicate = FALSE AND l1_processed_at IS NULL` 扫描）。
4. THE `entity_mentions` 表 SHALL 包含以下字段：`id` (BIGSERIAL PK) / `msg_id` (BIGINT, 逻辑引用 `normalized_messages.id`) / `entity` (VARCHAR(128), NOT NULL) / `entity_type` (VARCHAR(32)) / `raw_source` (VARCHAR(32), NOT NULL) / `ts` (TIMESTAMPTZ, NOT NULL) / `engagement` (INTEGER, DEFAULT 0) / `author_weight` (FLOAT, DEFAULT 1.0) / `confidence` (FLOAT, DEFAULT 1.0) / `is_kol_mention` (BOOLEAN, DEFAULT FALSE)。
5. THE `entity_mentions` 表 SHALL 建立以下索引：`INDEX(entity, ts DESC)` / `INDEX(ts DESC)` / `INDEX(raw_source, ts DESC)`。
6. THE `entity_mentions` 表 SHALL 建立以下唯一约束：`UNIQUE(msg_id, entity)`（落地 Req 4.8 的幂等写入：`INSERT ... ON CONFLICT (msg_id, entity) DO NOTHING`）。
7. THE `hotness_snapshots` 表 SHALL 包含以下字段：`id` (BIGSERIAL PK) / `window_end` (TIMESTAMPTZ, NOT NULL) / `window_type` (VARCHAR(16), NOT NULL) / `entity` (VARCHAR(128), NOT NULL) / `entity_type` (VARCHAR(32)) / `count_short` (INTEGER) / `count_baseline` (FLOAT) / `growth_rate` (FLOAT) / `cross_source` (INTEGER) / `engagement_sum` (INTEGER) / `is_new_entity` (BOOLEAN, DEFAULT FALSE) / `final_score` (FLOAT) / `rank` (INTEGER) / `UNIQUE(window_end, window_type, entity)`。
8. THE `hotness_snapshots` 表 SHALL 建立以下索引：`INDEX(window_end DESC, window_type, rank ASC)` / `INDEX(entity, window_end DESC)`。
9. THE 迁移脚本 SHALL 使用 **Alembic**（或在不引入 Alembic 时，所有 DDL 语句显式使用 `CREATE TABLE IF NOT EXISTS` 与 `CREATE INDEX IF NOT EXISTS`，不允许裸 `CREATE TABLE` / `CREATE INDEX`）在一次执行中创建全部三张表及其索引/约束；迁移必须同时满足两个条件才算成功：(a) 三张表及全部索引/约束实际创建完成；(b) 对已完成建表的库重复执行该迁移不抛错（幂等）。若 (a) 未完成，迁移应以非零退出码失败，不允许仅因 (b) 成立就认为迁移通过。
10. THE 三张新表 SHALL **不对现有 5 张表（twitter_posts / binance_square_posts / discord_messages / summary_level1 / summary_level2）建立外键依赖**，保证老链路可以独立工作；`entity_mentions.msg_id → normalized_messages.id` 的引用只做逻辑关联，由应用层保证一致性。
11. THE 三个 ORM 模型的实现 SHALL 与 `db/models.py` 中现有 `_RawPostMixin` / `SummaryLevel1` / `SummaryLevel2` 的风格对齐：SQL 类型 `TIMESTAMPTZ` 必须用 `DateTime(timezone=True)` 表达，默认当前时间用 `server_default=func.now()`，默认布尔/数值用 `server_default` 字符串形式（例如 `"false"` / `"0"`），索引命名遵循 `idx_<table>_<cols>` 规范（例如 `idx_normalized_messages_ts` / `idx_entity_mentions_entity_ts` / `idx_hotness_snapshots_window_rank`），唯一约束命名遵循 `uq_<table>_<cols>` 规范（例如 `uq_normalized_messages_source_raw` / `uq_entity_mentions_msg_entity` / `uq_hotness_snapshots_window_entity`）。

### Requirement 6: 滑动窗口计数器（Task 1.6）

**User Story:** 作为 Hotness_Service 的消费者，我希望查询"某实体在最近 1 小时提及多少次"是毫秒级操作，而不是每次都扫 `entity_mentions` 表。

#### Acceptance Criteria

1. THE Sliding_Counter SHALL 在内存中维护至少四个命名窗口：`15min`（900 秒）/ `1h`（3600 秒）/ `24h`（86400 秒）/ `7d`（604800 秒）。
2. THE Sliding_Counter SHALL 提供 `add(entity: str, ts: float)` 方法，把一条时间戳追加到所有窗口的指定实体队列尾部。
3. WHEN 调用 `count(entity: str, window: str)` 查询某实体在指定窗口内的提及次数，THE Sliding_Counter SHALL 返回当前时刻该窗口内的实时计数，复杂度 O(过期条数 + 1)。
4. THE Sliding_Counter SHALL 在每次 `count` 调用时懒惰清理队首已过期的时间戳（`ts < now - window_size`）。
5. THE Sliding_Counter SHALL 在服务启动时从 `entity_mentions` 表回填最近 7 天的数据（最长窗口），保证重启后计数器不出现"基线缺失"假激增。
6. THE Sliding_Counter SHALL 作为进程内单例被 Hotness_Service 与 Entity_Extractor 共享：Entity_Extractor 写入 `entity_mentions` 后同步调用 `Sliding_Counter.add`，避免每次查库。
7. THE Sliding_Counter 回填逻辑 SHALL 尽力在 2 分钟内完成（在 Gate 1 预估的数据量下），且必须在 **10 分钟硬上限**之内结束。回填行为按以下四种情况处理，进程在四种情况下均 SHALL 正常启动、worker 主循环 SHALL 正常进入调度：
   - 情况 A（快速成功）：回填在 2 分钟内成功完成。THE 回填逻辑 SHALL 仅输出一条 INFO 日志，包含实际耗时与回填记录条数，**不** 产生 WARN / ERROR 日志，**不** 改变 Hotness_Service 本轮的执行路径（本轮按正常规则判断是否到达 15 分钟整点）。
   - 情况 B（慢速成功）：回填最终成功完成，实际耗时介于 (2 分钟, 10 分钟]。THE 回填逻辑 SHALL 输出一条 WARN 日志记录实际耗时，Hotness_Service 本轮 SHALL 使用该已回填数据（不跳过）。
   - 情况 C（硬超时）：回填持续时间超过 10 分钟仍未完成。THE 回填逻辑 SHALL **强制中止**当前回填（取消后台任务或丢弃中间结果），输出一条 ERROR 日志，Hotness_Service 本轮 SHALL 跳过并等待下一轮 15 分钟调度重试。下一轮 Hotness_Service 触发时允许尝试重新回填（不要求守护进程永久不再回填）。
   - 情况 D（未超时失败）：回填在 10 分钟内抛出未捕获异常或因数据库不可用等原因未能成功完成。行为与情况 C 对齐：THE 回填逻辑 SHALL 输出一条 ERROR 日志，Hotness_Service 本轮 SHALL 跳过并等待下一轮 15 分钟调度重试。

### Requirement 7: 增长率计算与 Top-K 排行榜（Task 1.7）

**User Story:** 作为最终用户，我希望每 15 分钟得到一份"当前最值得关注的 20 个实体"排行榜，按增长倍数排序，以便在市场普遍看懂之前抓到正在升温的叙事。

#### Acceptance Criteria

1. WHEN 每 15 分钟的调度触发到达时，THE Hotness_Service SHALL 从 `Sliding_Counter` / `entity_mentions` 读取所有在最近 24 小时内出现过的 Entity，对其逐个计算 `growth_rate`。
2. THE Hotness_Service SHALL 按以下公式计算 `growth_rate`：`growth_rate = short_count / max(baseline_per_hour, 2.0)`，其中：
   - `short_count` = 窗口 `[window_end - 1h, window_end)` 内的提及次数；
   - `baseline_per_hour` = 窗口 `[window_end - 7d, window_end - 1h)` 内总提及次数 / `(7 * 24 - 1)` 小时；
   - `2.0` 是平滑因子（防止冷启动除零与"突然提一次"的假信号）。
3. THE Hotness_Service SHALL 按以下公式计算 `final_score`：`final_score = growth_rate * (1 + 0.3 * (cross_source - 1))`，其中 `cross_source` 为该实体在短窗内出现过的独立 `raw_source` 个数（取值 1~3）。
4. THE Hotness_Service SHALL 标记"新实体"：若某实体在基线窗口 `[window_end - 7d, window_end - 1h)` 内出现次数为 0，且在短窗内出现 ≥ 5 次，则 `is_new_entity = TRUE`；否则 `is_new_entity = FALSE`。
5. THE Hotness_Service SHALL 将所有 Entity 按 `final_score` 降序排序，取前 20 名，为每条记录赋 `rank` 值（1~20），`window_type = "1h"`，`window_end` 为本轮调度触发时刻（向下对齐到 15 分钟整点）。
6. THE Hotness_Service SHALL 将这 20 条记录通过单个事务写入 `hotness_snapshots`；同一 `(window_end, window_type, entity)` 已存在时使用 UPSERT 覆盖（幂等）。
7. THE Hotness_Service SHALL 在数据量不足时降级：若最近 7 天 `entity_mentions` 总记录数 < 100，则跳过本轮计算并在 INFO 日志中输出明确原因文案 `"hotness skipped: baseline data insufficient (count=<N> < 100)"`，包含实际记录数 `<N>`，便于运维与 Req 6.7 情况 C/D 的"回填失败"日志明确区分（回填失败的日志级别为 ERROR 且文案以 `"sliding-counter backfill failed"` 开头）。
8. THE Hotness_Service SHALL 在单轮计算耗时 > 60 秒时产生 WARN 日志（便于后续性能观测）。
9. THE Hotness_Service SHALL 在本轮写库失败时回滚事务，不污染排行榜；下一轮调度（15 分钟后）自然重试。
10. WHILE `final_score` 相等，THE Hotness_Service SHALL 按 `short_count` 降序作为二级排序键；仍相等时按 `entity` 字母序作为三级排序键（保证 `rank` 稳定可复现）。

### Requirement 8: Worker 注入与调度（Task 1.8）

**User Story:** 作为系统维护者，我希望新增的 L0 / L1 / L2 流水线在 `scheduler/jobs.py` 的同一个 worker 线程里跑，不破坏"同一时刻 Ollama 只有一个模型在飞"这个关键约束（虽然 Phase 1 不调 LLM，但必须为 Phase 2 保留这个约束）。

#### Acceptance Criteria

1. THE `scheduler/jobs.py` 的 `Jobs` 类 SHALL 扩展一个新参数（如 `new_services`），接受一个按顺序依次 `run_once()` 的 service 列表；现有 `level1_services` / `level2_services` 参数与行为必须保持不变。
2. THE `main.py` SHALL 在 worker 启动时额外注入以下 service（按顺序）：`Normalizer_Service`（含内嵌 Deduplicator）、`Entity_Extractor`、`Hotness_Service`；老的 `Level1Service` / `Level2Service` 继续保留注入。
3. THE `_worker_loop` SHALL 在每一轮循环里按"level1 → level2 → new_services"或等价顺序依次触发每个 service 的 `run_once()`，任意一个返回 `True`（代表"本轮确实处理了数据"）则下一轮不 sleep。
4. THE Normalizer_Service.run_once() SHALL 在无新原始消息时返回 `False`，在成功归一化 ≥ 1 条消息时返回 `True`。
5. THE Entity_Extractor.run_once() SHALL 在无新原版消息（`is_duplicate = FALSE` 且未 L1 处理过）时返回 `False`，在成功抽取 ≥ 1 条消息的实体时返回 `True`。
6. THE Hotness_Service.run_once() SHALL 基于"距上次排行榜写入是否已超过 15 分钟"判断本轮是否触发：未到时间返回 `False`；到时间则完整算一次并返回 `True`。
7. IF 任一 new_services 的 `run_once()` 抛出未捕获异常，THEN THE `_worker_loop` SHALL 捕获并记录 ERROR 日志，继续下一个 service 的执行，不允许单个 service 异常把 worker 线程带死。
8. THE worker SHALL 保证进程收到 `shutdown` 信号后，当前正在执行的 service 的 `run_once()` 要么跑完要么在 10 秒内被打断（复用现有 `_stop_event` 机制），不允许写到一半的脏数据。
9. THE Phase 1 流水线 SHALL **不调用任何 LLM**（`llm/ollama_client.py` 在 new_services 中完全不被 import）；老链路 `Level1Service` / `Level2Service` 仍然调用 LLM 但行为不变。

## Success Metrics（Gate 1 验收标准）

Phase 1 完成的判定必须同时满足以下全部指标，才有资格进入 Phase 2：

1. **稳定性**：系统连续运行 **72 小时不崩溃**，期间 worker 主循环未退出，PostgreSQL 连接未泄漏，进程内存增长不超过初始的 2 倍。
2. **排行榜产出节奏**：`hotness_snapshots` 表在 Gate 1 窗口期内，**每 15 分钟稳定产出 1 份**（允许 ±2 分钟抖动），72 小时内累计条数 ≥ `72 * 4 * 20 * 0.95 = 5472`（按 5% 故障容忍计算）。
3. **排行榜命中率**：人工每天挑 3 个整点时刻，对比当时的 Twitter 真实热点话题，**Top-20 命中率 ≥ 60%**（即排行榜中至少有 12 个实体出现在人工判断的"当前 Twitter 热门"里）。连续 3 天取平均，不低于 60%。
4. **去重有效性**：在 Gate 1 观测窗口内，`normalized_messages` 中 `is_duplicate = TRUE` 的比例在 **10% ~ 60%** 之间（下界验证 SimHash 确实抓到了重复；上界验证阈值没设太松错杀真内容）。
5. **词典命中可观测**：`entity_mentions` 表在 Gate 1 期间 `confidence = 1.0`（词典命中）的记录数 > 0 且 `confidence = 0.95`（正则命中）的记录数 > 0，证明两条抽取通路都工作。
6. **老链路不退化**：Phase 1 部署前后，`summary_level1` / `summary_level2` 的每日产出条数不下降超过 10%（证明新流水线没抢老链路的 DB 连接或其他资源）。
7. **LLM 调用量验证**：Phase 1 新流水线对 `llm/ollama_client.py` 的调用次数 = **0**（可通过日志或 metric 观测确认）。

## Non-Goals

Phase 1 **明确不做**的事项（方案 4 的核心警告：个人开发者最大的敌人是复杂度；终极设计文档 §16 的反模式清单）。以下能力属于后续 Phase，不纳入本 spec 验收：

1. **L3 共现网络**：实体两两共现统计、新叙事检测、三角共现——留给 Phase 2。
2. **L4 Embedding 聚类**：bge-m3 向量化、HDBSCAN 聚类、代表消息选择——留给 Phase 2。
3. **L5 LLM 定向简报**：Phase 1 完全不调 LLM；结构化 JSON 简报、narrative/catalyst/fund_logic 等字段——留给 Phase 2。
4. **L6 Telegram 实时告警**：`growth > 20x` 立即推送——留给 Phase 2。
5. **Streamlit 可视化面板**：热度柱状图、共现网络图、生命周期时间轴——留给 Phase 3。
6. **多时间窗口并行（6h / 24h）**：Phase 1 只出 `window_type = "1h"` 一种；6h / 24h 窗口留给 Phase 2。
7. **Engagement / KOL / Novelty 加权**：Phase 1 的 `final_score` 只含 `growth × cross_source` 两项；完整公式（含 `log1p(engagement) * kol_flag * novelty`）留给 Phase 2。
8. **激增实时触发**：Phase 1 只按 15 分钟整点批量产出排行榜，不做"每累计 50 条就检查一次增长率"的实时触发——留给 Phase 2。
9. **Embedding 精筛去重**：Phase 1 只做 SimHash（阶段一）；cosine ≥ 0.92 的 Embedding 精筛（阶段二）留给 Phase 3。
10. **小模型兜底实体抽取**：Phase 1 只做正则 + 词典两层；第三层"`qwen3:8b` 批处理抽实体"留给 Phase 3，且需先观测到词典覆盖率 < 70% 才开启。
11. **情绪分析打分**：`normalized_messages.sentiment_score` 字段虽然建表时预留，但 Phase 1 全部写 `0`，不做情绪词典匹配。
12. **日度总评（`qwen3:32b`）**：跨热点每日总评——留给 Phase 3。
13. **热点生命周期标记**：萌芽 → 爆发 → 高潮 → 衰退——留给 Phase 3。
14. **改造或淘汰老链路**：Phase 1 内 `Level1Service` / `Level2Service` / `prefilter.classify` 老调用方 / `summary_level1` / `summary_level2` 保持现状；是否下线留给 Phase 3 根据 L5 产出质量决定。
15. **新基础设施依赖**：**不引入** Milvus / Weaviate / Redis / Kafka / RabbitMQ / ClickHouse / pgvector / FAISS；存储一律走现有 PostgreSQL，队列一律走进程内内存，滑动窗口走进程内内存。
16. **微服务拆分**：Phase 1 保持单 Python 进程单 worker 线程；多进程 / 多实例 / 跨机部署留给"日处理量 > 5000 万"时再考虑。
17. **链上数据 / 多模态 / 回测**：链上数据（Dune API）、图片/视频多模态、策略回测系统——全部超出本项目范围，**永远不做**（除非商业化）。
