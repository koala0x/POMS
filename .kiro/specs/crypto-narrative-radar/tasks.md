# Implementation Plan · crypto-narrative-radar Phase 1

> **说明**：本 tasks.md 基于 `requirements.md` v1.2 与 `design.md` v1.1（实施期校准）产出，只覆盖 Phase 1（止血期，约 1 周）的实施任务。Phase 2/3/4 不在本清单范围。
>
> **执行约定**：
> - 每个 Task 完成后勾选 `[x]`，主动跑对应单元测试确保绿。
> - 顺序执行：前一个 Task 未完成不得开始下一个（除非明确标注"可并行"）。
> - 每个 Task 末尾的 `_Requirements:` 标注它落地的 requirements.md 条目编号。
>
> **v1.1 变更记录**（第一轮质量检查后修订）：
> - 修正 Task 5 / Task 6 顺序：SlidingCounter 现在是 Task 5，EntityExtractor 变成 Task 6
> - Task 2.3 拆成 Task 2.3a / Task 2.3b，控制单任务工作量
> - 补充了 Req 3.5 / Req 6.7 情况 B / Req 7.9 / Req 8.7 / Req 8.8 的测试 case
>
> **v1.2 变更记录**（第二轮质量检查后修订）：
> - Task 4.3 明确 NormalizerService 构造器字段含 `dedup: Deduplicator`（避免实施时漏传依赖）
> - Task 8.3 显式列出 3 个新 service 的构造参数（关键：EntityExtractor 和 HotnessService 必须持有同一个 `sliding_counter` 实例）
> - Task 8.5 修正零 LLM 验证方式：mock `OllamaClient.chat` 并断言 `call_count == 0`（之前的"import 即暴露"说法不准确）
> - Task 2.3b 加词典 substring 性能备注（Gate 1 观测 > 2s 才考虑 Aho-Corasick）
> - Task 5.2 / 5.3 引入可配置 `warn_seconds`（抽到 Settings），便于单测用小阈值验证情况 B
> - Task 8.2 新增 `sliding_counter_backfill_warn_seconds` 配置字段
> - Task 8.4 `shutdown` 测试阈值从 10s 收紧到 2s（避免单测跑太慢）
> - Task 3.1 索引命名 `idx_normalized_messages_unproc` 改为完整的 `idx_normalized_messages_is_duplicate_l1_processed_at`（design.md §3.5.1 同步）
> - Task 9.1 修复 Markdown 代码块嵌套语法（外层用 4 个反引号）

---

## Task 0：依赖与脚手架

- [ ] **0.1 引入新依赖**
  - 修改 `requirements.txt`，追加三行：
    ```
    simhash==2.1.2
    PyYAML==6.0.2
    alembic==1.13.2
    ```
  - 在 `.venv` 中执行 `pip install -r requirements.txt`，确认三个包成功安装。
  - 跑一次现有测试 `pytest tests/ -v`，确保所有测试仍然通过（baseline 回归）。
  - _Requirements: 所有 Phase 1 Req 的前置_

- [ ] **0.2 初始化 Alembic**
  - 在仓库根目录执行 `alembic init alembic`，生成 `alembic/` 目录与 `alembic.ini`。
  - 修改 `alembic/env.py`：
    - `import` 语句加上 `from config.settings import get_settings` 和 `from db.models import Base`。
    - 把 `target_metadata` 设为 `Base.metadata`。
    - 动态构造 `sqlalchemy.url` 从 `get_settings()` 读取（不从 `alembic.ini` 硬编码）。
  - 修改 `alembic.ini`：把 `script_location = alembic` 保留，其他 DB 配置留空（由 `env.py` 代管）。
  - _Requirements: Req 5.9_

---

## Task 1：词典外置与加载器（Req 3）

- [ ] **1.1 建立词典目录与 YAML 骨架**
  - 新建 `dictionaries/` 目录。
  - 新建 `dictionaries/tickers.yaml`：把 `services/prefilter.py` 中 `COIN_KEYWORDS_EN`（60+ 个）和 `COIN_KEYWORDS_ZH`（7 个）全部迁移进来，每项标注 `type`（layer1 / defi / meme / stablecoin / layer2 / privacy / us_stock 等，按现有代码分组注释标注的分类）。中文词作为英文主词的 `aliases`。样例：
    ```yaml
    BTC:
      type: layer1
      aliases: [比特币, bitcoin, 大饼, 老大]
    HYPE:
      type: defi
      aliases: [hyperliquid, HL]
    ```
  - 新建 `dictionaries/chains.yaml` 骨架（内容可为 `{}` 空对象，后续再填）。
  - 新建 `dictionaries/narratives.yaml` 骨架（`{}` 空对象）。
  - 新建 `dictionaries/kols.yaml` 骨架（`{}` 空对象）。
  - _Requirements: Req 3.1, 3.2, 3.6_

- [ ] **1.2 实现词典加载器 `dictionaries/loader.py`**
  - 按 design.md §3.3 的代码骨架实现：
    - `DictionaryEntry` frozen dataclass（`name` / `entity_type` / `aliases` / `weight`）
    - `Dictionaries` frozen dataclass（`tickers` / `chains` / `narratives` / `kols` / `alias_index`，全部用 `MappingProxyType` 包裹）
    - `load_dictionaries(base_dir: Path) -> Dictionaries`：逐个文件加载、跨文件同名冲突检查、别名冲突检查、INFO 日志输出词条数。
    - `_load_one(path, entity_type)`：YAML 语法错向上 raise、空文件 WARN 返回空 dict、`aliases` 合并 `keywords` 字段（narrative 用 `keywords`，其他用 `aliases`）、**必填字段缺失时 raise 明确错误**（如 `BTC: ~` 这种空值条目）。
  - _Requirements: Req 3.1, 3.3, 3.5, 3.6, 3.7, 3.8_

- [ ] **1.3 暴露单例入口 `dictionaries/__init__.py`**
  - 实现 `get_dictionaries() -> Dictionaries`，内部用 `functools.lru_cache(maxsize=1)` + 默认 `base_dir = Path(__file__).parent`。
  - _Requirements: Req 3.4_

- [ ] **1.4 新增单元测试 `tests/test_dictionary_loader.py`**
  - 测试用例：
    - `test_load_valid_tickers_yaml` - 正常加载含 3 个 ticker + aliases 的 yaml
    - `test_empty_file_allows_startup` - 空 yaml 文件返回空 dict + 产生 WARN 日志
    - `test_invalid_yaml_raises` - 语法错误的 yaml 抛 `yaml.YAMLError`
    - `test_missing_required_field_raises` - `BTC: ~` 或 `BTC: {}` 等缺少必填字段（name 为空值）抛 `RuntimeError`（覆盖 Req 3.5 "必填字段缺失"）
    - `test_cross_file_name_conflict_raises` - `tickers.yaml` 和 `chains.yaml` 同时定义 `BTC` → 抛 `RuntimeError`
    - `test_alias_conflict_raises` - 两个 ticker 定义了同一个 alias → 抛 `RuntimeError`
    - `test_alias_index_lowercase` - `alias_index` 的 key 全部是小写
  - 使用 `tmp_path` fixture 产生临时 yaml 文件，不依赖真实 `dictionaries/` 目录。
  - 运行 `pytest tests/test_dictionary_loader.py -v`，全部通过。
  - _Requirements: Req 3.5, 3.6, 3.8_

---

## Task 2：改造 prefilter.py（Req 4 + Req 3.3, 3.4）

- [ ] **2.1 扩展 FilterDecision 数据结构**
  - 修改 `services/prefilter.py`：
    - 新增 `Entity` frozen dataclass（`name` / `entity_type` / `confidence`）。
    - 改 `FilterDecision`：追加 `entities: list[Entity] = field(default_factory=list)` 字段，**必须带默认值**。
  - 关键兼容性验证：现有代码 `FilterDecision(True, "A:$symbol")` 这种两参数构造必须仍然可用。
  - _Requirements: Req 4.1, 4.9_

- [ ] **2.2 把硬编码词典切换为动态加载**
  - 修改 `services/prefilter.py`：
    - 删除 `COIN_KEYWORDS_EN` 与 `COIN_KEYWORDS_ZH` 两个模块级常量。
    - 在模块加载时调用 `dictionaries.get_dictionaries()`，从 `tickers.yaml` 产出的 `Dictionaries` 对象里动态构造 `_EN_COIN_RE` / `_ZH_COIN_RE` 两个正则。
    - 英文词典取所有 `entity_type == 'ticker'` 且 name 是 ASCII 的条目；中文词典取 aliases 中含中文字符的条目。
  - _Requirements: Req 3.3, 3.4_

- [ ] **2.3a 正则扩展：合约地址抽取**（工作量拆分 1/2）
  - 修改 `services/prefilter.py`：
    - 新增 `_EVM_ADDR_RE = re.compile(r'0x[a-fA-F0-9]{40}')` 匹配 EVM 合约
    - 新增 `_SOLANA_ADDR_RE = re.compile(r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b')` 匹配 Solana 合约
    - 新增辅助函数 `_extract_regex_entities(content: str) -> list[Entity]`，处理三种正则命中并返回 Entity 列表（`$TICKER` → ticker，EVM/Solana 合约 → project），confidence=0.95
  - **不动 `classify` 的 keep/drop 规则**，只新增辅助函数。
  - _Requirements: Req 4.2（正则部分）, 4.3_

- [ ] **2.3b 词典抽取 + 实体去重 + 整合到 classify**（工作量拆分 2/2）
  - 修改 `services/prefilter.py` 的 `classify(content)` 函数：
    - 调 `_extract_regex_entities(c)` 得到正则实体
    - 遍历 `dicts.alias_index`（小写 key），用 `substring in c.lower()` 判断词典命中，构造 confidence=1.0 的 Entity
    - 单条消息内实体去重：同一 `entity.name` 只保留 confidence 较高的一条（词典命中优先覆盖正则命中）
    - 原有 keep/drop 判断逻辑完全不变，`FilterDecision` 返回时带上 `entities` 列表
  - **性能备注**：substring match 是 O(词典大小 × 文本长度)。Phase 1 词典约 200 条，单次 classify 耗时 < 5ms，batch=500 累计 < 1s，足够用。Gate 1 观测到 Entity_Extractor 单 batch 耗时 > 2s 时考虑升级到 Aho-Corasick（`pip install pyahocorasick`）。**Phase 1 不做**，留到 Phase 3 词典扩到上万条时再优化。
  - _Requirements: Req 4.2（词典部分）, 4.4_

- [ ] **2.4 跑老测试 + 新增 classify 实体抽取测试**
  - 跑 `pytest tests/test_prefilter.py -v`，**所有老测试必须通过**（Req 4.9 回归）。
  - 在 `tests/test_prefilter.py` 追加测试用例（不开新文件）：
    - `test_classify_returns_entities_for_dollar_ticker` - `"$BTC 突破"` 返回 1 个 Entity（BTC / ticker / 1.0，因为 BTC 在词典里）
    - `test_classify_returns_entities_for_unknown_ticker` - `"$XYZABC 突破"` 返回 1 个 Entity（XYZABC / ticker / 0.95，不在词典里走正则）
    - `test_classify_dedup_regex_and_dict_hit` - `"$BTC 比特币"` 只返回 1 个 Entity（confidence=1.0，词典覆盖正则）
    - `test_classify_evm_address` - `"0xdAC17F958D2ee523a2206206994597C13D831ec7 好项目"` 返回 1 个 project Entity（confidence=0.95）
    - `test_classify_solana_address` - 合法的 32~44 位 base58 地址被抽出
    - `test_classify_empty_entities_still_returns_decision` - 空内容返回 `FilterDecision(keep=False, reason='...', entities=[])`
    - `test_filter_decision_backward_compat` - 用两参数构造 `FilterDecision(True, 'A')` 不抛错，`.entities == []`
  - _Requirements: Req 4.1, 4.2, 4.3, 4.4, 4.9_

---

## Task 3：数据库新表与迁移（Req 5）

- [ ] **3.1 在 db/models.py 追加三个 ORM 类**
  - 按 design.md §3.5.1 完整实现：
    - `NormalizedMessage`（字段含 `l1_processed_at TIMESTAMPTZ NULL`；索引 `idx_normalized_messages_ts` / `idx_normalized_messages_source_ts` / `idx_normalized_messages_simhash` / `idx_normalized_messages_is_duplicate_l1_processed_at`（加速 Entity_Extractor 扫描的复合索引）；唯一约束 `uq_normalized_messages_source_raw`）
    - `EntityMention`（唯一约束 `uq_entity_mentions_msg_entity`；索引 `idx_entity_mentions_entity_ts` / `idx_entity_mentions_ts` / `idx_entity_mentions_source_ts`）
    - `HotnessSnapshot`（唯一约束 `uq_hotness_snapshots_window_entity`；索引 `idx_hotness_snapshots_window_rank` / `idx_hotness_snapshots_entity_window`）
  - 风格对齐：`DateTime(timezone=True)`、`server_default=func.now()`、布尔 `server_default="false"`、数值 `server_default="0"` 等——与现有 `_RawPostMixin` / `SummaryLevel1` 完全一致。
  - 在 `db/models.py` 的 `__all__` 末尾追加三个新类名。
  - _Requirements: Req 5.1~5.8, 5.10, 5.11_

- [ ] **3.2 手写 Alembic 迁移 `alembic/versions/001_phase1_initial.py`**
  - **不使用 `alembic revision --autogenerate`**（Risk E 兜底），手写 `upgrade()` / `downgrade()`。
  - `upgrade()` 里用 `op.create_table(...)` × 3 + `op.create_index(...)` × 10 个索引。
  - `downgrade()` 里按倒序 `op.drop_table(...)` × 3（索引跟表一起删，不用单独 drop）。
  - 迁移文件头：`revision = "001"`, `down_revision = None`。
  - _Requirements: Req 5.9_

- [ ] **3.3 迁移演练**
  - 本地（或测试库）执行：
    ```bash
    alembic upgrade head     # 应创建三张表
    alembic downgrade -1     # 应删除三张表
    alembic upgrade head     # 再升一次
    alembic upgrade head     # 重复执行幂等，不抛错
    ```
  - 连到 PostgreSQL 检查：
    ```sql
    \dt   -- 应看到 normalized_messages / entity_mentions / hotness_snapshots
    \di   -- 应看到 10 个新索引
    ```
  - 用 `pg_dump --schema-only` 前后对比现有 5 张表（twitter_posts / binance_square_posts / discord_messages / summary_level1 / summary_level2）schema 应完全一致。
  - _Requirements: Req 5.9, 5.10_

- [ ] **3.4 实现三个 repositories**
  - 新建 `db/repositories/normalized_messages_repo.py`：
    - `insert(session, ...) -> Optional[int]` - `INSERT ... ON CONFLICT (raw_source, raw_id) DO NOTHING` + 通过 `RETURNING id` 判断是否真插入
    - `fetch_unprocessed_for_l1(session, limit)` - `WHERE is_duplicate = FALSE AND l1_processed_at IS NULL ORDER BY ts ASC, id ASC LIMIT :limit`
    - `mark_l1_processed(session, ids: Sequence[int])` - `UPDATE ... SET l1_processed_at = NOW() WHERE id = ANY(:ids) AND l1_processed_at IS NULL`
    - `fetch_recent_simhashes(session, hours: int)` - 回填 SimHash 用，返回 `list[tuple[id, simhash, ts]]`
  - 新建 `db/repositories/entity_mentions_repo.py`：
    - `bulk_upsert(session, rows: list[dict])` - `INSERT ... ON CONFLICT (msg_id, entity) DO NOTHING`
    - `count_since(session, since: datetime) -> int` - 用于 Req 7.7 基线充足性检查
    - `count_for_entity(session, entity, start, end) -> int`
    - `count_sources_for_entity(session, entity, start, end) -> int` - `SELECT COUNT(DISTINCT raw_source)`
    - `stream_mentions_since(session, since: datetime, chunk_size: int = 10000)` - 流式回填 Sliding_Counter 用
  - 新建 `db/repositories/hotness_snapshots_repo.py`：
    - `upsert_batch(session, window_end, window_type, records)` - 按 design.md §3.7 的 UPSERT SQL 实现
  - _Requirements: Req 5.1, 5.4, 5.6_

---

## Task 4：L0 Normalizer + Deduplicator（Req 1 + Req 2）

- [ ] **4.1 实现 Deduplicator `services/l0_dedup.py`**
  - 按 design.md §3.2 代码骨架实现：
    - `compute_simhash(text) -> int` 调 `simhash.Simhash(text).value`
    - `is_duplicate(sh, now_ts) -> tuple[bool, Optional[int]]` - 按小时分桶查找
    - `add(sh, msg_id, ts)` - 追加到当前小时桶 + 调用 `_evict_old`
    - `_evict_old(cur_bucket)` - 删除超过 `window_hours` 的旧桶
    - `_hamming(a, b)` - `bin(a ^ b).count("1")`
  - 配置从 `Settings` 的 `dedup_hamming_threshold` / `dedup_window_hours` 读取。
  - _Requirements: Req 2.1, 2.2, 2.3, 2.6_

- [ ] **4.2 实现 Deduplicator.backfill_from_db 方法**
  - 参数：`db: Database`。
  - 逻辑：调用 `NormalizedMessagesRepo.fetch_recent_simhashes(session, 24)`，对每条调 `self.add(sh, id, ts.timestamp())`。
  - 失败不抛异常，返回 `False` 即可（由调用方在启动时决定是否继续）。
  - _Requirements: Req 2.5_

- [ ] **4.3 实现 NormalizerService `services/l0_normalizer.py`**
  - 按 design.md §3.1 代码骨架：
    - 构造器字段（frozen dataclass）：`db: Database` / `normalized_repo: NormalizedMessagesRepo` / **`dedup: Deduplicator`**（★ 必须持有 Deduplicator 实例，否则 `run_once` 里的判重调用无处落脚）/ `batch_size: int = 500` / `timezone: ZoneInfo`
    - `run_once() -> bool`：三源各扫 `batch_size` 条未归一化记录 → 合并 → 逐条清洗 + 计算 SimHash + 判重 + `INSERT ... ON CONFLICT DO NOTHING` → 若真插入则 `self.dedup.add(...)`。
    - 空 content 跳过并产生 INFO 日志（修订自 Req 1.5 的 DEBUG）。
    - `batch_size` 从 `Settings.normalizer_batch_size` 读取，默认 500。
  - **不写原始表的 `is_summarized` 字段**（Req 1.8）。
  - _Requirements: Req 1.1~1.7, 1.8, 2.2, 2.3, 2.4_

- [ ] **4.4 新增单元测试 `tests/test_l0_dedup.py`**
  - 测试用例：
    - `test_identical_text_is_duplicate` - 相同文本两次进入，第二次 `is_duplicate=True`，`dup_of` 指向第一次的 id
    - `test_near_text_within_hamming_threshold` - 改一两个字符（在阈值 3 内）仍判重
    - `test_different_text_not_duplicate` - 完全不同文本 `is_duplicate=False`
    - `test_old_bucket_evicted` - 模拟 25 小时前的消息被清出桶
    - `test_hamming_calculation` - 直接测 `_hamming(0b1010, 0b0101) == 4`
  - _Requirements: Req 2.1, 2.2, 2.3_

- [ ] **4.5 新增单元测试 `tests/test_l0_normalizer.py`**
  - 使用 SQLite in-memory + `Base.metadata.create_all(engine)` 搭建测试 DB。
  - 测试用例：
    - `test_normalizer_ingests_twitter_post` - 注入一条 `TwitterPost`，跑一次 `run_once`，`normalized_messages` 多一条
    - `test_normalizer_discord_author_format` - `DiscordMessage(channel_name='alpha-calls', username='ye')` 归一化后 `author == '#alpha-calls @ye'`
    - `test_normalizer_skips_empty_content` - `content="   "` 跳过，`normalized_messages` 无新增
    - `test_normalizer_idempotent` - 同一条原始记录跑两次 `run_once`，`normalized_messages` 只有一条（`UNIQUE (raw_source, raw_id)` 生效）
    - `test_normalizer_does_not_touch_raw_is_summarized` - 原始表的 `is_summarized` 字段前后保持 `False`
    - `test_normalizer_returns_false_when_no_new_data` - 空库调用 `run_once()` 返回 `False`
  - _Requirements: Req 1.1~1.8, 2.2_

---

## Task 5：L2 Sliding Counter（Req 6）

> ⚠️ **顺序提醒**：SlidingCounter 必须先于 EntityExtractor 实现，因为 EntityExtractor 的 `run_once` 会同步调用 `sliding_counter.add(entity, ts)` 更新计数（见 Req 6.6）。这是 v1.0 → v1.1 的修订点。

- [ ] **5.1 实现 SlidingCounter `services/l2_sliding_counter.py`**
  - 按 design.md §3.6 代码骨架：
    - `WINDOWS_SECONDS = {"15min": 900, "1h": 3600, "24h": 86400, "7d": 604800}`
    - `_store: dict[str, dict[str, deque[float]]]`
    - `add(entity, ts)` - 四窗口同时 append
    - `count(entity, window) -> int` - 惰性清理过期 + 返回 len
    - `active_entities(window='24h') -> list[str]` - 返回该窗口内至少被提及一次的所有 entity
  - **不加锁**（Phase 1 单 worker 线程调用；在 docstring 里明确说明，未来多线程需加 `threading.Lock`）。
  - _Requirements: Req 6.1, 6.2, 6.3, 6.4, 6.6_

- [ ] **5.2 实现 backfill_from_db 方法**
  - 按 design.md §3.6 `backfill_from_db(db, max_seconds=600, warn_seconds=120, chunk_size=10000)` 实现。
  - 参数读取：
    - `max_seconds` 从 `Settings.sliding_counter_backfill_max_seconds` 读取，默认 600（情况 C 硬超时）
    - `warn_seconds` 从 `Settings.sliding_counter_backfill_warn_seconds` 读取，默认 120（情况 A/B 的分界）← ★ 新增，便于测试时注入小值
  - 返回 `tuple[bool, int, float]` = (是否成功, 回填条数, 实际耗时秒)。
  - 流式读（`session.execute(stmt).partitions(chunk_size)`），每 chunk 检查累计耗时。
  - 日志：
    - 耗时 ≤ `warn_seconds`：INFO `"sliding-counter backfill 完成：耗时 X.Xs，回填 N 条"`（情况 A）
    - 耗时 (`warn_seconds`, `max_seconds`]：WARN `"sliding-counter backfill 慢速成功：耗时 X.Xs，回填 N 条"`（情况 B）
    - 超过 `max_seconds`：ERROR `"sliding-counter backfill failed: 超过 Xs 硬上限..."`（情况 C）
    - 抛异常：ERROR `"sliding-counter backfill failed: <异常>"`（情况 D）
  - _Requirements: Req 6.5, 6.7（情况 A/B/C/D 四种分支）_

- [ ] **5.3 新增单元测试 `tests/test_l2_sliding_counter.py`**
  - 测试用例：
    - `test_add_and_count_same_window` - add 3 次 "BTC"，`count('BTC', '1h') == 3`
    - `test_count_different_windows` - add 一次，`count('BTC', '15min') == 1` 同时 `count('BTC', '7d') == 1`
    - `test_expired_entries_lazily_cleaned` - 用 `time.time() - 7200` 作为 ts add，`count('BTC', '1h') == 0`
    - `test_active_entities` - add 两个 entity 在 24h 内，add 一个在 7d 但 24h 外 → `active_entities('24h')` 只返回两个
    - `test_unknown_window_raises` - `count('BTC', '2h')` 抛 ValueError
    - `test_backfill_fast_success_info_log` - 注入 `warn_seconds=1.0` `max_seconds=2.0`，mock 一个耗时 0.1s 的 stream：返回 `(True, N, <1.0)` 且只产生 INFO 日志（情况 A）
    - `test_backfill_slow_success_warns` - 注入 `warn_seconds=0.1` `max_seconds=2.0`，mock 一个耗时 0.3s 的 stream：返回 `(True, N, ~0.3)` 且产生 WARN 日志（情况 B，覆盖 Req 6.7 情况 B，之前遗漏）
    - `test_backfill_hard_timeout` - 注入 `warn_seconds=0.1` `max_seconds=0.3`，mock 一个耗时 1s 的 stream：返回 `(False, ...)` 且产生 ERROR 日志（情况 C）
    - `test_backfill_db_exception` - mock 数据库抛异常：返回 `(False, ...)` 且产生 ERROR 日志（情况 D）
  - _Requirements: Req 6.1~6.7_

---

## Task 6：L1 Entity Extractor（Req 4 剩余部分）

> ⚠️ **依赖前置**：Task 6 依赖 Task 2（改造后的 prefilter）+ Task 3.4（EntityMentionsRepo / NormalizedMessagesRepo）+ Task 5（SlidingCounter 单例）。

- [ ] **6.1 实现 EntityExtractor `services/l1_entity_extractor.py`**
  - 按 design.md §3.4.3 代码骨架：
    - 从 `NormalizedMessagesRepo.fetch_unprocessed_for_l1` 拉取消息
    - 对每条调 `prefilter.classify(text)` 拿 `entities`
    - 判断 KOL：`(msg.author or "").lower()` 在 `dicts.kols` 中则 `is_kol_mention=True`（Phase 1 简化：直接用 author 字符串匹配 kol name，后续 Phase 可拆 Twitter handle）
    - `entity_mentions` 批量 UPSERT + `normalized_messages.l1_processed_at = NOW()` 同事务
    - 落库成功后同步调 `sliding_counter.add(entity, ts.timestamp())`（失败不更新内存，避免脏数据）
    - `batch_size` 从 `Settings.entity_extractor_batch_size` 读取，默认 500
  - _Requirements: Req 4.5, 4.6, 4.7, 4.8_

- [ ] **6.2 新增单元测试 `tests/test_l1_entity_extractor.py`**
  - 测试用例（SQLite in-memory）：
    - `test_entity_extractor_writes_mentions` - 注入一条 `normalized_messages` 含 `"$BTC heating up"`，跑一次 `run_once`，`entity_mentions` 多一条
    - `test_entity_extractor_dedup_regex_and_dict` - `"$BTC 比特币"` 只写一条（confidence=1.0）
    - `test_entity_extractor_zero_entities_still_marks_processed` - 消息无任何实体命中，`entity_mentions` 不写入但 `l1_processed_at` 被设置
    - `test_entity_extractor_idempotent` - 同一条消息被 `run_once` 两次，`entity_mentions` 不重复
    - `test_entity_extractor_skips_duplicates` - `is_duplicate=True` 的消息不被扫描（`fetch_unprocessed_for_l1` 过滤）
    - `test_entity_extractor_updates_sliding_counter` - 调 `run_once` 后 `sliding_counter.count('BTC', '1h') >= 1`
    - `test_entity_extractor_kol_flag` - author 在 `kols.yaml` 中时 `is_kol_mention=True`
    - `test_entity_extractor_returns_false_on_empty` - 没有待处理消息时返回 False（覆盖 Req 8.5 返回值语义）
  - _Requirements: Req 4.5~4.8, 8.5_

---

## Task 7：L2 Hotness Service（Req 7）

- [ ] **7.1 实现 HotnessService `services/l2_hotness.py`**
  - 按 design.md §3.7 代码骨架：
    - `align_to_quarter(dt) -> datetime` 工具函数（向下对齐到 `:00/:15/:30/:45`）
    - `_last_window_end` 状态字段：防止同一 `window_end` 重复处理
    - `_counter_ready` 状态字段：由 `main.py` 在 backfill 后注入；False 时本轮跳过并记录 `hotness skipped: sliding counter not ready`，然后置回 True
    - `run_once() -> bool`：
      1. 检查 `_counter_ready`，False 直接跳过
      2. `window_end = align_to_quarter(datetime.now(tz))`，与 `_last_window_end` 比较
      3. 基线充足性检查：`mentions_repo.count_since(s, window_end - 7d) < 100` → INFO `"hotness skipped: baseline data insufficient (count=<N> < 100)"` 并返回 False
      4. 从 `sliding_counter.active_entities('24h')` 取候选
      5. 对每个 entity 计算 `growth_rate` / `cross_source` / `final_score` / `is_new_entity`
      6. 排序：`(-final_score, -count_short, entity)` 三级稳定排序
      7. 取前 `top_k=20` 条，UPSERT 到 `hotness_snapshots`，`window_type='1h'`
      8. 记录耗时，> 60s WARN；更新 `_last_window_end`，返回 True
    - 失败回滚事务，返回 False
  - 配置全部从 `Settings` 读（`hotness_top_k` / `hotness_smoothing` / `hotness_short_hours` / `hotness_baseline_days` / `hotness_min_baseline_count`）。
  - _Requirements: Req 7.1~7.10_

- [ ] **7.2 新增单元测试 `tests/test_l2_hotness.py`**
  - 测试用例：
    - `test_growth_rate_formula` - 直接造数据 short=900 baseline=20/h → growth=45（误差在 1% 内）
    - `test_smoothing_prevents_zero_division` - baseline=0 short=5 → growth=5/2=2.5（用 SMOOTHING）
    - `test_new_entity_flag` - baseline=0 short=5 → `is_new_entity=True`；baseline=0 short=4 → `is_new_entity=False`
    - `test_final_score_cross_source_weight` - cross_source=1 → score=growth；cross_source=3 → score=growth*1.6
    - `test_stable_ordering` - 两个 entity final_score 相同，按 count_short 降序；再相同按字母序升序
    - `test_baseline_insufficient_skips` - count_since < 100 → `run_once` 返回 False + INFO 日志匹配 `"baseline data insufficient"`
    - `test_counter_not_ready_skips_and_resets` - `_counter_ready=False` 第一次跳过，第二次自动置 True
    - `test_align_to_quarter` - `10:23` → `10:15`；`10:45:30` → `10:45`
    - `test_upsert_overwrites_same_window` - 同 `(window_end, entity)` 调两次 `upsert_batch`，最后一次覆盖
    - `test_write_failure_rolls_back` - mock `upsert_batch` 抛异常 → `run_once` 返回 False，`hotness_snapshots` 无任何脏数据，下一轮（通过修改 `_last_window_end` 模拟）仍能重试（覆盖 Req 7.9，之前遗漏）
  - _Requirements: Req 7.1~7.10_

---

## Task 8：Worker 扩展与 main.py 注入（Req 8）

- [ ] **8.1 扩展 Jobs 类**
  - 修改 `scheduler/jobs.py`：
    - `__init__` 追加参数 `new_services: Sequence[object] = ()`（带默认值确保兼容性）
    - `self._new_services = new_services`
    - `_worker_loop` 按 design.md §3.8.2 的 "level1 → level2 → new_services" 固定顺序迭代
    - 异常捕获：任意 service `run_once()` 抛错 → log.error + 继续下一个 service，不让单个 service 拖死整个 worker
    - `_stop_event.is_set()` 检查嵌入内层循环（已有）
  - 启动日志追加 `new={len(new_services)}` 字段。
  - _Requirements: Req 8.1, 8.3, 8.7, 8.8_

- [ ] **8.2 扩展 `config/settings.py` 增加 10 个新字段**
  - 按 design.md §4，在 `Settings` dataclass 追加：
    ```python
    # === 6. Phase 1 新流水线配置 ===
    normalizer_batch_size: int = 500
    dedup_hamming_threshold: int = 3
    dedup_window_hours: int = 24
    entity_extractor_batch_size: int = 500
    hotness_top_k: int = 20
    hotness_smoothing: float = 2.0
    hotness_short_hours: int = 1
    hotness_baseline_days: int = 7
    hotness_min_baseline_count: int = 100
    sliding_counter_backfill_max_seconds: int = 600
    sliding_counter_backfill_warn_seconds: int = 120    # Req 6.7 情况 A/B 分界，便于测试注入小值
    ```
  - 为每个字段加一行中文注释说明语义与对应的 requirements 条目。
  - _Requirements: Req 2.6, 6.7, 7.2, 7.5, 7.7_

- [ ] **8.3 修改 main.py 注入新链路**
  - 按 design.md §3.8.3，在现有 `main()` 里（`Jobs(...)` 构造之前）追加以下步骤，严格按顺序执行：
    1. 加载词典：调 `load_dictionaries(base_dir / "dictionaries")`（失败直接抛错阻止启动）
    2. 构造 3 个新 repo 实例：`normalized_repo = NormalizedMessagesRepo()` / `mentions_repo = EntityMentionsRepo()` / `hotness_repo = HotnessSnapshotsRepo()`
    3. 构造 `SlidingCounter`（`sliding_counter = SlidingCounter()`），调 `sliding_counter.backfill_from_db(db)`，记录返回值 `ok, total, elapsed`
    4. 构造 `Deduplicator`（从 `Settings` 读阈值和窗口），调 `dedup.backfill_from_db(db)`
    5. **显式列出 3 个 service 的构造参数（关键：EntityExtractor 和 HotnessService 必须持有 `sliding_counter` 同一实例）**：
       - 5a. `normalizer_service = NormalizerService(db=db, normalized_repo=normalized_repo, dedup=dedup, batch_size=settings.normalizer_batch_size, timezone=settings.timezone)`
       - 5b. `entity_extractor = EntityExtractor(db=db, normalized_repo=normalized_repo, mentions_repo=mentions_repo, sliding_counter=sliding_counter, batch_size=settings.entity_extractor_batch_size)`  ← ★ 注意 sliding_counter
       - 5c. `hotness_service = HotnessService(db=db, mentions_repo=mentions_repo, hotness_repo=hotness_repo, sliding_counter=sliding_counter, top_k=settings.hotness_top_k, smoothing=settings.hotness_smoothing, short_hours=settings.hotness_short_hours, baseline_days=settings.hotness_baseline_days, min_baseline_count=settings.hotness_min_baseline_count, timezone=settings.timezone)`  ← ★ 同一个 sliding_counter 实例
    6. `hotness_service._counter_ready = ok`（sliding_counter 的回填结果）
    7. `Jobs(...)` 构造时传 `new_services=[normalizer_service, entity_extractor, hotness_service]`
  - **保留现有所有代码不动**（level1_services / level2_services 逻辑原样）。
  - _Requirements: Req 8.2, 8.4, 8.5, 8.6, 8.9_

- [ ] **8.4 新增 worker 单元测试 `tests/test_scheduler_jobs.py`**
  - 测试用例（mock service，不跑真实业务）：
    - `test_worker_runs_in_fixed_order` - 注入 3 个 mock service 到 `new_services`，按序调用 `run_once`
    - `test_one_service_exception_does_not_block_others` - 第 2 个 service 抛异常，第 3 个仍被调用，worker 不退出（覆盖 Req 8.7，之前遗漏）
    - `test_shutdown_interrupts_within_2s` - 注入一个 `run_once` 无限 sleep 的 mock service，调 `shutdown(wait=True)`，断言 `thread.is_alive() == False` 在 **2 秒内**（Req 8.8 生产阈值是 10s，单测用更严格的 2s 避免测试跑太慢；通过 `_stop_event.wait(poll_interval_seconds)` 的短 `poll_interval` 实现快速响应）
    - `test_worker_backward_compat_without_new_services` - 不传 `new_services` 参数，Jobs 仍能正常构造和启动（验证默认值 `()` 的兼容性）
  - _Requirements: Req 8.1, 8.3, 8.7, 8.8_

- [ ] **8.5 新增集成测试 `tests/test_phase1_pipeline.py`**
  - 使用 SQLite in-memory + `Base.metadata.create_all(engine)` + 所有模型。
  - 场景：
    1. 用 monkeypatch 让 `get_dictionaries` 返回一个含 `$BTC`, `AI_Agent` 的假词典
    2. 向 `twitter_posts` 塞 100 条含 `$BTC`（90 条）和 `$ETH`（10 条）的假数据（当前时间戳）
    3. 向 `twitter_posts` 再塞 100 条只含 `$ETH` 的基线数据（7 天前的 ts）
    4. 依次调用 `NormalizerService.run_once()` / `EntityExtractor.run_once()` /（EntityExtractor 会同步更新 SlidingCounter，不需要手动 add）/ `HotnessService.run_once()`
    5. 断言 `hotness_snapshots` 有至少 1 条记录，`rank=1` 的 entity 是 `BTC`（短窗 BTC=90 baseline=0 → growth≈45；ETH 短窗 10 baseline≈0.6 → growth≈5）
    6. **零 LLM 验证方式**：
       - 在 test fixture 里用 `unittest.mock.patch` 替换 `llm.ollama_client.OllamaClient.chat` 为 `Mock()`
       - 测试末尾断言 `mock_chat.call_count == 0`
       - 这样即使某个 service 意外调了 `OllamaClient().chat(...)`，也能明确捕捉（`import` 本身不会触发，必须 mock 实际调用点）
  - _Requirements: Req 8.9, 全链路 smoke test_

---

## Task 9：Gate 1 验收准备（不写代码，只准备材料）

- [ ] **9.1 写 `docs/gate1_checklist.md` 验收操作手册**
  - 按 requirements.md Success Metrics 7 条逐条列出：
    - 每条指标的观测 SQL 或命令
    - 验收通过的量化阈值
    - 失败时的诊断流程
  - 示例模板（用 4 个反引号做外层，内层 3 个反引号正常渲染）：

    ````markdown
    ### 指标 3：排行榜命中率 ≥ 60%

    **观测方式**：
    每天在 9:00 / 14:00 / 21:00 三个整点跑：

    ```sql
    SELECT entity, rank, growth_rate, cross_source, final_score
    FROM hotness_snapshots
    WHERE window_end = <整点时刻>
      AND window_type = '1h'
    ORDER BY rank ASC
    LIMIT 20;
    ```

    手动对比当时 Twitter 热搜/KOL 主要讨论的 10~20 个话题，
    数出排行榜中能在 Twitter 真实热点里找到的实体数。
    ≥ 12 / 20 即通过。连续 3 天取平均。
    ````

  - _Requirements: requirements.md Success Metrics §3_

- [ ] **9.2 写 `docs/rollback_plan.md` 回滚预案**
  - 如果 Gate 1 期间老链路产出下降超过 10%（Req 6 / Success Metrics §6），按以下步骤回滚：
    1. `Ctrl+C` 停服务
    2. 从 `main.py` 的 `Jobs(...)` 调用中移除 `new_services=[...]` 参数（改回三参数构造）
    3. 重启服务 → 老链路自动恢复独占资源
    4. 新链路表 `normalized_messages` / `entity_mentions` / `hotness_snapshots` 的数据保留（下次重新启用时直接继续用）
  - 如果需要彻底清空新链路数据：
    ```bash
    alembic downgrade -1   # 删三张表
    ```
  - _Requirements: requirements.md Success Metrics §6 风险兜底_

---

## 执行顺序与依赖图（v1.1 修正版）

```
Task 0 (依赖与脚手架)
   │
   ├─► Task 1 (词典加载器)
   │       │
   │       └─► Task 2 (prefilter 改造：2.1 → 2.2 → 2.3a → 2.3b → 2.4)
   │                │
   │                └───────────────────┐
   │                                    │
   ├─► Task 3 (数据库新表：3.1 → 3.2 → 3.3 → 3.4)
   │       │                            │
   │       └─► Task 4 (L0 Normalizer + Dedup)
   │                 │                  │
   │                 └─► Task 5 (L2 Sliding Counter) ← 顺序修正：必须先于 Task 6
   │                          │         │
   │                          └─► Task 6 (L1 Entity Extractor) ← 依赖 Task 2 + Task 5
   │                                   │
   │                                   └─► Task 7 (L2 Hotness)
   │                                            │
   │                                            └─► Task 8 (Worker + main.py)
   │                                                     │
   │                                                     └─► Task 9 (Gate 1 材料)
```

**可并行的 Task**（上下文隔离足够）：
- Task 1 与 Task 3 可并行（词典与 DB 互不依赖）
- Task 1.4 测试与 Task 3.4 repositories 可并行

**必须串行的 Task**：
- Task 2 必须在 Task 1 之后（prefilter 依赖 `get_dictionaries()`）
- Task 4 必须在 Task 3 之后（Normalizer 依赖 `NormalizedMessagesRepo`）
- **Task 5 必须在 Task 3 之后（SlidingCounter 回填读 `entity_mentions`）**
- **Task 6 必须在 Task 2 + Task 4 + Task 5 之后（EntityExtractor 依赖改造后的 prefilter、已归一化的数据、SlidingCounter 单例）** ← v1.0→v1.1 关键修正
- Task 7 必须在 Task 5 + Task 6 之后（Hotness 用 SlidingCounter + entity_mentions）
- Task 8 必须在 Task 1~7 全部完成后（main.py 注入需要所有 service 可用）

---

*文档版本：v1.2（两轮质量检查后修订）*
*基于：requirements.md v1.2 + design.md v1.0*
*预估工时：7 天（按 1 人全力推进）*

**v1.0 → v1.1 主要变更**：
1. Task 5 ↔ Task 6 互换（SlidingCounter 先于 EntityExtractor）
2. Task 2.3 拆成 2.3a（正则扩展）+ 2.3b（词典抽取 + 整合）
3. Task 1.4 新增 `test_missing_required_field_raises`（覆盖 Req 3.5 "必填字段缺失"）
4. Task 5.3（原 6.3）新增 `test_backfill_slow_success_warns`（覆盖 Req 6.7 情况 B）
5. Task 7.2 新增 `test_write_failure_rolls_back`（覆盖 Req 7.9）
6. Task 8 拆出 8.4 Worker 单元测试（覆盖 Req 8.7 异常隔离、Req 8.8 shutdown 打断）
7. Task 6.2 新增 `test_entity_extractor_returns_false_on_empty`（覆盖 Req 8.5）

**v1.1 → v1.2 主要变更**：
1. Task 4.3 明确构造器字段含 `dedup: Deduplicator`（避免实施时漏传依赖）
2. Task 8.3 显式列出 3 个新 service 的构造参数（关键：`sliding_counter` 必须同实例）
3. Task 8.5 修正零 LLM 验证方式：mock `chat` 并断言 `call_count == 0`
4. Task 2.3b 加词典 substring 性能备注
5. Task 5.2 / 8.2 引入可配置 `sliding_counter_backfill_warn_seconds`
6. Task 5.3 三个 backfill 测试改用小阈值配置
7. Task 8.4 shutdown 测试阈值 10s → 2s
8. Task 3.1 索引命名 `unproc` → `is_duplicate_l1_processed_at`（design.md 同步）
9. Task 9.1 Markdown 代码块嵌套语法修复
