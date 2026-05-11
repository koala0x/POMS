# Design Document · crypto-narrative-radar Phase 1

## 1. 概述与设计目标

### 1.1 范围

本设计文档**只覆盖 Phase 1**（requirements.md 中的 Req 1~8）：在现有代码基座之上扩展 L0（归一化 + 去重）→ L1（实体抽取）→ L2（滑动窗口 + 热度统计）三层流水线，产出每 15 分钟刷新的 Top-20 实体热度排行榜（`hotness_snapshots` 表）。

**明确不包含**：L3 共现网络、L4 Embedding 聚类、L5 LLM 简报、L6 Telegram 告警、Streamlit 面板——这些是 Phase 2/3 的范围。

### 1.2 三条核心哲学（贯穿所有设计决策）

1. **增长率优先**：所有排序基于 `growth_rate × cross_source` 加权分，不基于绝对提及量。
2. **零 LLM**：Phase 1 新流水线绝不 `import llm.ollama_client`；老 `Level1Service` / `Level2Service` 继续跑 LLM 但行为不变。
3. **复杂度是第一风险**：不引入 Redis / FAISS / Milvus / Kafka / pgvector；所有状态放 PostgreSQL 或进程内内存。

### 1.3 Design 阶段要解决的关键问题

| 问题 | 解决方案 | 对应章节 |
|------|---------|---------|
| 如何扩展 `prefilter.FilterDecision` 不破坏老 `Level1Service` 调用方 | `entities` 字段 `field(default_factory=list)` + 老调用方继续解构 `(keep, reason)` 两元组 | §3.4 |
| 如何让新 `new_services` 和老 `level1/level2` 共用同一个 worker 不互相传染异常 | 扩展 `Jobs.__init__` 新增带默认值的 `new_services` 参数 + `_worker_loop` 固定顺序 + 异常隔离 | §3.8 |
| 如何保证 `Hotness_Service` 严格每 15 分钟触发一次且 `window_end` 对齐到整点 | `window_end` 向下对齐到最近的 `XX:00 / XX:15 / XX:30 / XX:45` + 状态字段记录上次触发的 `window_end` | §3.7 |
| 启动回填如何在 10 分钟硬上限内完成 | 流式分批 + 按 `ts DESC` 只取最近 7 天 + 超时强制中止 + 下一轮重试 | §3.6 |
| 词典从 Python 源码迁移到 YAML 时 prefilter 与 entity_extractor 如何共享同一份数据不重复维护 | 新增 `dictionaries/loader.py` 单例加载，返回 `frozen dataclass` + `MappingProxyType` | §3.3 |

---

## 2. 总架构图

```
┌──────────────────────────────────────────────────────────────────────────┐
│  现有数据源表（不动）                                                      │
│  twitter_posts · binance_square_posts · discord_messages                  │
└───────────────────────┬──────────────────────────────────────────────────┘
                        │
                        ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  Normalizer_Service（新）                                                  │
│  services/l0_normalizer.py                                                 │
│  · 扫三源未归一化记录 → 清洗 → 计算 SimHash → 判重                          │
│  · 内嵌 Deduplicator（共用同一 run_once 事务）                              │
│  · 幂等写入 normalized_messages                                            │
└───────────────────────┬──────────────────────────────────────────────────┘
                        │
                        ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  Entity_Extractor（新）                                                    │
│  services/l1_entity_extractor.py                                           │
│  · 消费 normalized_messages 中 is_duplicate=FALSE 且 l1_processed_at=NULL  │
│  · 调 prefilter.classify_with_entities() 拿 (keep, reason, entities)        │
│  · entities 写入 entity_mentions（UPSERT 幂等）                             │
│  · 同步调 Sliding_Counter.add(entity, ts)                                  │
│  · 更新 normalized_messages.l1_processed_at = NOW()                        │
└───────────────────────┬──────────────────────────────────────────────────┘
                        │
                        ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  Sliding_Counter（内存单例，新）                                            │
│  services/l2_sliding_counter.py                                            │
│  · dict[window_name, dict[entity, deque[ts]]]                              │
│  · add(entity, ts) / count(entity, window) / growth_rate(entity)            │
│  · 启动时从 entity_mentions 回填最近 7 天                                   │
└───────────────────────┬──────────────────────────────────────────────────┘
                        │
                        ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  Hotness_Service（新）                                                     │
│  services/l2_hotness.py                                                    │
│  · run_once：检查距上次 window_end 是否已过 15 分钟                          │
│  · 计算所有候选 entity 的 growth_rate / cross_source / final_score           │
│  · Top-20 UPSERT 写入 hotness_snapshots                                     │
│  · ★ 整个流程不 import ollama_client                                        │
└──────────────────────────────────────────────────────────────────────────┘

scheduler/jobs.py worker 主循环：
    ┌───────────────────────────────────────────────────────────────┐
    │  while not stop_event:                                         │
    │    for svc in level1_services:      svc.run_once()  # 老链路   │
    │    for svc in level2_services:      svc.run_once()  # 老链路   │
    │    for svc in new_services:         svc.run_once()  # 新链路   │
    │      [Normalizer_Service,                                      │
    │       Entity_Extractor,                                        │
    │       Hotness_Service]                                         │
    │    if processed_any: continue                                  │
    │    else: stop_event.wait(poll_interval_seconds)                │
    └───────────────────────────────────────────────────────────────┘
```

---

## 3. 详细设计

### 3.1 Normalizer_Service（Req 1）

**文件**：`services/l0_normalizer.py`（新增）

**职责**：把 `twitter_posts` / `binance_square_posts` / `discord_messages` 三张原始表的新记录统一成 `NormalizedMessage`，写入 `normalized_messages`。**内嵌调用 `Deduplicator`，共用同一个 `run_once` 事务**（见 §3.2）。

**接口**：

```python
from dataclasses import dataclass
from db.connection import Database
from db.repositories.normalized_messages_repo import NormalizedMessagesRepo
from services.l0_dedup import Deduplicator

@dataclass(frozen=True)
class NormalizerService:
    db: Database
    normalized_repo: NormalizedMessagesRepo
    dedup: Deduplicator
    batch_size: int = 500  # 每轮最多处理多少条原始记录，避免一轮跑太久
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"))

    def run_once(self) -> bool:
        """
        返回 True 表示本轮处理了 ≥1 条记录（worker 不 sleep 立刻进下一轮）；
        返回 False 表示三源都没有新记录。
        """
        ...
```

**扫描策略**：

三源各自独立扫描，按 `(raw_source, raw_id)` 在 `normalized_messages` 中不存在判定。SQL 伪代码：

```sql
-- twitter 源
SELECT tp.id, tp.content, tp.author, tp.posted_at, tp.created_at
FROM twitter_posts tp
LEFT JOIN normalized_messages nm
  ON nm.raw_source = 'twitter' AND nm.raw_id = tp.id
WHERE nm.id IS NULL
ORDER BY tp.created_at ASC, tp.id ASC
LIMIT 500;
```

对 `binance_square_posts` / `discord_messages` 同样模式。三源在同一个 `run_once` 里**各自 500 条并行扫描**，合并后做去重判断。

**author 字段映射**：

| 源 | author 值 |
|----|-----------|
| twitter | `TwitterPost.author` 原值 |
| binance_square | `BinanceSquarePost.author` 原值 |
| discord | `f"#{channel_name} @{username}"`（复用现有 `DiscordMessage.author` 派生属性） |

**engagement 字段**：Phase 1 三源都没有点赞/转发计数字段，统一写 `0`（Req 5 已规定字段 default=0）。Phase 2 如果抓取层补了互动量再改。

**清洗规则**（Req 1.5）：

- `content.strip()` 后长度 = 0 → 跳过，INFO 日志（修订自 Req 1.5 要求的 DEBUG，便于生产可见）
- 其他清洗（去链接、统一大写）**Phase 1 不做**，保留原文给下游 L1 正则用

**幂等写入**：

```sql
INSERT INTO normalized_messages (raw_source, raw_id, text, author, ts, ...)
VALUES (...)
ON CONFLICT (raw_source, raw_id) DO NOTHING;
```

**不修改原始表**（Req 1.8）：整个流程绝不写 `twitter_posts.is_summarized` / `binance_square_posts.is_summarized` / `discord_messages.is_summarized`——那是老 `Level1Service` 专属。

---

### 3.2 Deduplicator（Req 2）

**文件**：`services/l0_dedup.py`（新增）

**算法选型决策**：**使用 `simhash` 库**（`pip install simhash==2.1.2`），**不使用 `datasketch`**。

理由：
1. SimHash 对"文本语义近似"有直觉的汉明距离度量（Req 2.2 要求 ≤ 3）；`datasketch.MinHashLSH` 基于 Jaccard 相似度，阈值调参更麻烦。
2. `simhash` 库单依赖、pure Python，和 Req 2 的"阶段一简单方案"哲学一致。
3. Phase 3 若升级到 Embedding 精筛（终极设计文档 §5.2 阶段二），`datasketch` 本来也要扔，现在不引入反而省事。

**内存索引数据结构**：

```python
from collections import defaultdict, deque
from typing import Optional
from dataclasses import dataclass, field

@dataclass
class Deduplicator:
    hamming_threshold: int = 3
    window_hours: int = 24

    # 按"小时桶"分片，每桶是 deque[(simhash, msg_id)]
    # 查询时只扫当前小时 + 前 window_hours 个桶，比全量遍历快
    _buckets: dict[int, deque[tuple[int, int]]] = field(default_factory=lambda: defaultdict(deque))

    def compute_simhash(self, text: str) -> int:
        """调用 simhash 库，返回 64 位整数指纹。"""
        from simhash import Simhash
        return Simhash(text).value

    def is_duplicate(self, sh: int, now_ts: float) -> tuple[bool, Optional[int]]:
        """
        在过去 window_hours 小时的内存桶里查近似。
        返回 (is_dup, dup_of_msg_id)。
        """
        cur_bucket = int(now_ts // 3600)
        for h in range(cur_bucket - self.window_hours, cur_bucket + 1):
            if h not in self._buckets:
                continue
            for existing_sh, existing_id in self._buckets[h]:
                if self._hamming(sh, existing_sh) <= self.hamming_threshold:
                    return True, existing_id
        return False, None

    def add(self, sh: int, msg_id: int, ts: float) -> None:
        bucket = int(ts // 3600)
        self._buckets[bucket].append((sh, msg_id))
        self._evict_old(bucket)

    def _evict_old(self, cur_bucket: int) -> None:
        """清理超过 window_hours 的旧桶，释放内存。"""
        cutoff = cur_bucket - self.window_hours
        for h in list(self._buckets.keys()):
            if h < cutoff:
                del self._buckets[h]

    @staticmethod
    def _hamming(a: int, b: int) -> int:
        return bin(a ^ b).count("1")
```

**启动回填**（Req 2.5）：

```sql
-- 从 normalized_messages 读最近 24 小时的 simhash，重建内存桶
SELECT id, simhash, ts
FROM normalized_messages
WHERE ts >= NOW() - INTERVAL '24 hours'
  AND simhash IS NOT NULL
  AND is_duplicate = FALSE  -- 只回填原版，不回填重复
ORDER BY ts ASC;
```

回填策略：按 `ts ASC` 流式读，每 10000 条 `session.yield_per(10000)`，避免一次性载入全部到内存。回填失败的日志与 Req 6.7 情况 C 合并（见 §3.6）。

**嵌入 Normalizer 的位置**：

```python
# NormalizerService.run_once 伪代码
for raw in raw_records:
    text = raw.content.strip()
    if not text:
        logger.info("skip empty content: source={} id={}", raw.source, raw.id)
        continue
    ts_float = raw.posted_at.timestamp() if raw.posted_at else raw.created_at.timestamp()
    sh = self.dedup.compute_simhash(text)
    is_dup, dup_of = self.dedup.is_duplicate(sh, ts_float)
    # INSERT ... ON CONFLICT DO NOTHING
    new_id = self.normalized_repo.insert(
        session,
        raw_source=..., raw_id=raw.id, text=text, ...,
        simhash=sh, is_duplicate=is_dup, dup_of=dup_of,
    )
    if new_id is not None:  # 真插入了（不是 ON CONFLICT 跳过的）
        self.dedup.add(sh, new_id, ts_float)
```

**重复消息仍入库**（Req 2.4）：`is_duplicate=TRUE` + `dup_of=<原版id>`，下游 Entity_Extractor 会通过 `WHERE is_duplicate = FALSE` 过滤。

---

### 3.3 词典加载模块（Req 3）

**新增文件**：`dictionaries/loader.py`（作为词典加载的唯一入口）

**YAML schema**：

> **命名约定**：YAML 里的 `type:` 字段是**细分类**（category），会被 loader 存入 `DictionaryEntry.category`。
> `DictionaryEntry.entity_type` 由所在**文件**硬决定（tickers.yaml → `ticker` / chains.yaml → `chain` / narratives.yaml → `narrative` / kols.yaml → `kol`），
> 不允许被 YAML 覆盖——这是 Req 4.3 "五类约束"的强制落地方式。
> 换句话说：`type: layer1` 只是告诉未来做叙事分组时 BTC 属于 L1，不会让 BTC 的 `entity_type` 变成 `layer1`。

```yaml
# dictionaries/tickers.yaml（file-scope entity_type = ticker）
BTC:
  type: layer1                              # → DictionaryEntry.category
  aliases: [比特币, bitcoin, 大饼, 老大]
ETH:
  type: layer1
  aliases: [以太坊, ethereum, 以太]
HYPE:
  type: defi
  aliases: [hyperliquid, HL]
# ... 覆盖现有 prefilter.py 的 COIN_KEYWORDS_EN（60+ 个）和 COIN_KEYWORDS_ZH（7 个）

# dictionaries/chains.yaml（file-scope entity_type = chain）
Base:
  aliases: [Base L2, Coinbase L2]
Solana:
  aliases: [SOL生态]

# dictionaries/narratives.yaml（file-scope entity_type = narrative）
AI_Agent:
  keywords: [AI agent, autonomous agent, agent protocol, ai16z]
RWA:
  keywords: [real world asset, tokenized, RWA]

# dictionaries/kols.yaml（file-scope entity_type = kol）
kol_xxx:
  type: btc_kol                             # → DictionaryEntry.category，与 tickers.yaml 的 `type:` 字段同义
  weight: 3.0
  aliases: ["@cz_binance", cz]
```

**加载器代码**：

```python
# dictionaries/loader.py
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import yaml
from loguru import logger


@dataclass(frozen=True)
class DictionaryEntry:
    name: str                   # 标准名，如 "BTC"
    entity_type: str            # ticker / chain / narrative / kol（由所在文件决定，不从 YAML 读）
    category: str | None        # 细分类，如 layer1 / defi / meme；YAML 里的 `type:` 存到这里
    aliases: tuple[str, ...]    # 包含标准名本身 + 所有别名，全部小写

    # 仅 kol 类型使用
    weight: float = 1.0


@dataclass(frozen=True)
class Dictionaries:
    """全局词典快照。Phase 1 只在启动时加载一次，运行时不重载。"""
    tickers: Mapping[str, DictionaryEntry]      # key = 标准名，value = entry
    chains: Mapping[str, DictionaryEntry]
    narratives: Mapping[str, DictionaryEntry]
    kols: Mapping[str, DictionaryEntry]

    # 反查索引：alias_lowercase → (standard_name, entity_type)
    # 全局共享一份，避免每条消息匹配时重复构建
    alias_index: Mapping[str, tuple[str, str]] = field(repr=False)


def load_dictionaries(base_dir: Path) -> Dictionaries:
    """
    启动时调用一次；返回不可变 Dictionaries。
    严格检查：
    - YAML 语法错误 → raise
    - 跨文件同名 entity → raise
    - 空文件 → WARN 允许启动（Req 3.6）
    """
    tickers = _load_one(base_dir / "tickers.yaml", entity_type="ticker")
    chains = _load_one(base_dir / "chains.yaml", entity_type="chain")
    narratives = _load_one(base_dir / "narratives.yaml", entity_type="narrative")
    kols = _load_one(base_dir / "kols.yaml", entity_type="kol")

    # 跨文件查重
    all_names = set()
    for d in (tickers, chains, narratives, kols):
        dup = all_names & d.keys()
        if dup:
            raise RuntimeError(f"字典跨文件同名冲突：{dup}")
        all_names |= d.keys()

    # 构建 alias 反查索引
    alias_index: dict[str, tuple[str, str]] = {}
    for d in (tickers, chains, narratives, kols):
        for name, entry in d.items():
            for alias in entry.aliases:
                alias_lower = alias.lower()
                if alias_lower in alias_index:
                    raise RuntimeError(
                        f"别名冲突：'{alias}' 同时指向 "
                        f"{alias_index[alias_lower]} 和 ({name}, {entry.entity_type})"
                    )
                alias_index[alias_lower] = (name, entry.entity_type)

    logger.info(
        "词典加载完成：tickers={} chains={} narratives={} kols={} aliases={}",
        len(tickers), len(chains), len(narratives), len(kols), len(alias_index),
    )

    return Dictionaries(
        tickers=MappingProxyType(tickers),
        chains=MappingProxyType(chains),
        narratives=MappingProxyType(narratives),
        kols=MappingProxyType(kols),
        alias_index=MappingProxyType(alias_index),
    )


def _load_one(path: Path, entity_type: str) -> dict[str, DictionaryEntry]:
    """加载单个 YAML。语法错误 raise；空文件 WARN 返回空 dict。"""
    if not path.exists():
        raise RuntimeError(f"词典文件不存在：{path}")
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)  # yaml.YAMLError 向上抛
    if raw is None:
        logger.warning("词典文件为空：{}", path)
        return {}

    result: dict[str, DictionaryEntry] = {}
    for name, cfg in raw.items():
        cfg = cfg or {}
        aliases = tuple(
            [name.lower()] + [a.lower() for a in (cfg.get("aliases") or cfg.get("keywords") or [])]
        )
        result[name] = DictionaryEntry(
            name=name,
            # ★ entity_type 由所在文件决定（ticker / chain / narrative / kol），不从 YAML 读，
            #   硬落地 Req 4.3 的"五类约束"（project 类型由正则产生，不走词典）
            entity_type=entity_type,
            # YAML 里的 `type:` 改读作 category（细分类），Phase 1 只存不用，Phase 2+ 叙事分组时使用
            category=(str(cfg["type"]) if "type" in cfg else None),
            aliases=aliases,
            weight=float(cfg.get("weight", 1.0)),
        )
    return result
```

**全局单例**：通过 `dictionaries/__init__.py` 提供 `get_dictionaries() -> Dictionaries`，内部用 `functools.lru_cache(maxsize=1)` 实现进程内单例。`prefilter.py` 和 `l1_entity_extractor.py` 都调用它，保证同一份数据。

---

### 3.4 Entity_Extractor 与 prefilter 扩展（Req 4）

**文件**：
- 改造：`services/prefilter.py`
- 新增：`services/l1_entity_extractor.py`

#### 3.4.1 prefilter.FilterDecision 扩展

```python
# services/prefilter.py 的 FilterDecision 改造
from dataclasses import dataclass, field
from typing import NamedTuple

@dataclass(frozen=True)
class Entity:
    name: str           # 标准名，如 "BTC" / "Base"
    entity_type: str    # ticker / chain / narrative / project / kol
    confidence: float   # 词典=1.0，正则=0.95

@dataclass(frozen=True)
class FilterDecision:
    keep: bool
    reason: str
    entities: list[Entity] = field(default_factory=list)  # ★ 关键：必须带默认值
```

**老 `Level1Service` 兼容性验证**：

```python
# services/level1_service.py 第 134 行左右（现有代码）
kept, dropped = prefilter_split(posts)
drop_reasons = Counter(reason for _, reason in dropped)
```

`prefilter_split` 返回 `(kept, dropped)`：`kept` 是 `list[post]`（不是 `FilterDecision`），`dropped` 是 `list[tuple[post, reason_str]]`。`dropped` 的构造是 `dropped.append((p, decision.reason))`——**只读 `reason`，不读 `entities`**，完全兼容。✅

#### 3.4.2 prefilter.classify 的改造

```python
# services/prefilter.py
from dictionaries import get_dictionaries

def classify(content: str) -> FilterDecision:
    c = (content or "").strip()
    n = len(c)
    dicts = get_dictionaries()

    entities: list[Entity] = []
    entity_names_seen: set[str] = set()

    # 1. 正则命中（$TICKER 等），confidence=0.95
    for m in _DOLLAR_RE.finditer(c):
        ticker = m.group(1).upper()
        # 标准化：如果正则命中的 ticker 在词典里，提升到词典 confidence=1.0
        dict_hit = dicts.alias_index.get(ticker.lower())
        if dict_hit:
            name, etype = dict_hit
            if name not in entity_names_seen:
                entities.append(Entity(name, etype, 1.0))
                entity_names_seen.add(name)
        else:
            if ticker not in entity_names_seen:
                entities.append(Entity(ticker, "ticker", 0.95))
                entity_names_seen.add(ticker)

    # 2. EVM / Solana 合约地址（project 类型，confidence=0.95）
    for m in _EVM_ADDR_RE.finditer(c):
        addr = m.group(0)
        if addr not in entity_names_seen:
            entities.append(Entity(addr, "project", 0.95))
            entity_names_seen.add(addr)
    # Solana 类似...

    # 3. 词典命中，confidence=1.0
    c_lower = c.lower()
    for alias_lower, (name, etype) in dicts.alias_index.items():
        if alias_lower in c_lower and name not in entity_names_seen:
            # 简单 substring match（高频词典可用 Aho-Corasick 优化，Phase 1 不必）
            entities.append(Entity(name, etype, 1.0))
            entity_names_seen.add(name)

    # 4. 原有的 keep/drop 判断逻辑保持不变
    if _DOLLAR_RE.search(c):
        return FilterDecision(True, "A:$symbol", entities)
    if _EN_COIN_RE.search(c) or _ZH_COIN_RE.search(c):
        return FilterDecision(True, "B:coin_dict", entities)
    if n < _HARD_DROP_LEN:
        return FilterDecision(False, "D:length<20", entities)
    # ... 其余规则不变
    return FilterDecision(False, "default", entities)
```

**关键点**：
- `COIN_KEYWORDS_EN` / `COIN_KEYWORDS_ZH` 从 prefilter.py 删除，改由 `dictionaries/tickers.yaml` 维护；`_EN_COIN_RE` / `_ZH_COIN_RE` 在模块加载时根据词典动态构造。
- 正则构造在 `prefilter.py` 模块加载时完成一次，缓存到模块级常量，避免每次 `classify` 都重建。

#### 3.4.3 Entity_Extractor

```python
# services/l1_entity_extractor.py
from dataclasses import dataclass
from dictionaries import get_dictionaries
from services.prefilter import classify
from services.l2_sliding_counter import SlidingCounter

@dataclass(frozen=True)
class EntityExtractor:
    db: Database
    normalized_repo: NormalizedMessagesRepo
    mentions_repo: EntityMentionsRepo
    sliding_counter: SlidingCounter     # ★ 共享单例，同步更新
    batch_size: int = 500

    def run_once(self) -> bool:
        # 拉取未处理的原版消息
        with self.db.get_session() as s:
            msgs = self.normalized_repo.fetch_unprocessed_for_l1(s, limit=self.batch_size)

        if not msgs:
            return False

        # 批量抽取
        to_insert: list[dict] = []
        to_mark_ids: list[int] = []
        dicts = get_dictionaries()

        for m in msgs:
            decision = classify(m.text)
            is_kol = (m.author or "").lower() in dicts.kols  # 简化；严格实现可拆出 handle
            for e in decision.entities:
                to_insert.append({
                    "msg_id": m.id,
                    "entity": e.name,
                    "entity_type": e.entity_type,
                    "raw_source": m.raw_source,
                    "ts": m.ts,
                    "confidence": e.confidence,
                    "is_kol_mention": is_kol,
                    "engagement": m.engagement,
                    "author_weight": m.author_weight,
                })
            to_mark_ids.append(m.id)

        # 写库（同一事务）
        with self.db.get_session() as s:
            try:
                self.mentions_repo.bulk_upsert(s, to_insert)
                self.normalized_repo.mark_l1_processed(s, to_mark_ids)
                s.commit()
            except Exception:
                s.rollback()
                raise

        # 同步更新 Sliding_Counter（成功落库之后才更新，避免脏数据）
        for item in to_insert:
            self.sliding_counter.add(item["entity"], item["ts"].timestamp())

        logger.info("entity_extractor 处理 {} 条消息，产出 {} 条实体", len(msgs), len(to_insert))
        return True
```

**幂等写入**（Req 4.8）：
- `entity_mentions` 表增加 `UNIQUE(msg_id, entity)` 约束
- 写入用 `INSERT ... ON CONFLICT (msg_id, entity) DO NOTHING`

**0 Entity 情况**（Req 4.7）：
- `to_insert` 可能为空，但 `to_mark_ids` 仍包含该消息 id
- `mark_l1_processed` 把 `l1_processed_at = NOW()`，下轮不再拉取
- **存储决策**：`normalized_messages` 表新增 `l1_processed_at TIMESTAMPTZ NULL` 字段，而不是建中间表 ← 详见 §3.5 表结构

**老 `Level1Service` 的 `prefilter.split(posts)` 调用路径**：保持不变——`split` 函数内部仍然调用新版 `classify`，只不过 `FilterDecision` 多出一个 `entities` 字段，`split` 的返回值格式 `(kept, dropped)` 没变。老链路读不到 `entities`，但也不会报错（它只用 `keep` / `reason`）。

---

### 3.5 数据库新表 ORM（Req 5）

**文件**：
- 扩展：`db/models.py` 新增三个 ORM 类
- 新增：`db/migrations/versions/001_phase1_initial.py` Alembic 迁移脚本

#### 3.5.1 ORM 定义

```python
# db/models.py 追加

class NormalizedMessage(Base):
    """
    Phase 1 L0 产出：归一化 + 去重后的消息表。
    设计决策：
    - l1_processed_at 作为 L1 处理标记（NULL = 未处理，NOT NULL = 已处理时间戳）
      比布尔字段信息更丰富，可用于排查延迟问题
    - embedding 字段 Phase 1 不加；Phase 2/3 用到时再迁移
    """
    __tablename__ = "normalized_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    raw_source: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    author_weight: Mapped[float] = mapped_column(Float, nullable=False, server_default="1.0")
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    engagement: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    simhash: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sentiment_score: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    is_duplicate: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    dup_of: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    l1_processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("raw_source", "raw_id", name="uq_normalized_messages_source_raw"),
        Index("idx_normalized_messages_ts", "ts"),
        Index("idx_normalized_messages_source_ts", "raw_source", "ts"),
        Index("idx_normalized_messages_simhash", "simhash"),
        Index(
            "idx_normalized_messages_is_duplicate_l1_processed_at",
            "is_duplicate", "l1_processed_at",
        ),  # 加速 entity_extractor 的扫描
    )


class EntityMention(Base):
    """Phase 1 L1 产出：实体提及表。"""
    __tablename__ = "entity_mentions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    msg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)  # 逻辑引用 normalized_messages.id，不建 FK
    entity: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    raw_source: Mapped[str] = mapped_column(String(32), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    engagement: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    author_weight: Mapped[float] = mapped_column(Float, nullable=False, server_default="1.0")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default="1.0")
    is_kol_mention: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    __table_args__ = (
        UniqueConstraint("msg_id", "entity", name="uq_entity_mentions_msg_entity"),
        Index("idx_entity_mentions_entity_ts", "entity", "ts"),
        Index("idx_entity_mentions_ts", "ts"),
        Index("idx_entity_mentions_source_ts", "raw_source", "ts"),
    )


class HotnessSnapshot(Base):
    """Phase 1 L2 产出：热度排行榜快照表。"""
    __tablename__ = "hotness_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_type: Mapped[str] = mapped_column(String(16), nullable=False)
    entity: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    count_short: Mapped[int | None] = mapped_column(Integer, nullable=True)
    count_baseline: Mapped[float | None] = mapped_column(Float, nullable=True)
    growth_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    cross_source: Mapped[int | None] = mapped_column(Integer, nullable=True)
    engagement_sum: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_new_entity: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    final_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "window_end", "window_type", "entity",
            name="uq_hotness_snapshots_window_entity"
        ),
        Index("idx_hotness_snapshots_window_rank", "window_end", "window_type", "rank"),
        Index("idx_hotness_snapshots_entity_window", "entity", "window_end"),
    )
```

#### 3.5.2 迁移方案：引入 Alembic

**决策**：Phase 1 引入 Alembic（项目当前没有，但加入成本极低，后续 Phase 必然用到）。

**操作步骤**：

```bash
# 1. 加入依赖
echo "alembic==1.13.2" >> requirements.txt

# 2. 初始化（一次性）
alembic init alembic

# 3. 修改 alembic/env.py，让它读 config/settings.py 的 DB URL，并 import Base
```

**迁移脚本骨架**：

```python
# alembic/versions/001_phase1_initial.py
"""Phase 1: 新增 normalized_messages / entity_mentions / hotness_snapshots"""

revision = "001"
down_revision = None  # Phase 1 是第一个迁移

from alembic import op
import sqlalchemy as sa

def upgrade() -> None:
    op.create_table(
        "normalized_messages",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("raw_source", sa.String(32), nullable=False),
        sa.Column("raw_id", sa.BigInteger, nullable=False),
        # ... 其余字段照 ORM
        sa.UniqueConstraint("raw_source", "raw_id", name="uq_normalized_messages_source_raw"),
    )
    op.create_index("idx_normalized_messages_ts", "normalized_messages", ["ts"])
    # ... 其余索引
    # entity_mentions / hotness_snapshots 类似

def downgrade() -> None:
    op.drop_table("hotness_snapshots")
    op.drop_table("entity_mentions")
    op.drop_table("normalized_messages")
```

Alembic 自带 version 表跟踪，重复执行幂等。满足 Req 5.8 的两条验收。

---

### 3.6 Sliding_Counter（Req 6）

**文件**：`services/l2_sliding_counter.py`（新增）

**数据结构**：

```python
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional
import time

WINDOWS_SECONDS = {
    "15min": 900,
    "1h": 3600,
    "24h": 86400,
    "7d": 604800,
}

@dataclass
class SlidingCounter:
    """
    进程内单例。线程安全说明：
    Phase 1 下只有 worker 单线程调用 add/count（requirements.md Req 8
    明确新老 service 共用同一 worker），**不加锁**。
    未来若拆多线程，此处需加 threading.Lock 保护 _store。
    """
    _store: dict[str, dict[str, deque[float]]] = field(
        default_factory=lambda: {w: defaultdict(deque) for w in WINDOWS_SECONDS}
    )

    def add(self, entity: str, ts: float) -> None:
        for w in WINDOWS_SECONDS:
            self._store[w][entity].append(ts)

    def count(self, entity: str, window: str) -> int:
        """惰性清理过期 + 返回当前计数。"""
        if window not in WINDOWS_SECONDS:
            raise ValueError(f"unknown window: {window}")
        cutoff = time.time() - WINDOWS_SECONDS[window]
        dq = self._store[window].get(entity)
        if not dq:
            return 0
        while dq and dq[0] < cutoff:
            dq.popleft()
        return len(dq)

    def active_entities(self, window: str = "24h") -> list[str]:
        """返回在 window 内至少被提及过一次的所有实体（供 Hotness 扫描用）。"""
        cutoff = time.time() - WINDOWS_SECONDS[window]
        return [e for e, dq in self._store[window].items() if dq and dq[-1] >= cutoff]
```

**启动回填**（Req 6.5, 6.7）：

```python
# services/l2_sliding_counter.py
def backfill_from_db(
    self,
    db: Database,
    max_seconds: int = 600,  # Req 6.7 硬上限：10 分钟
    chunk_size: int = 10000,
) -> tuple[bool, int, float]:
    """
    返回 (是否成功, 回填条数, 实际耗时秒)。
    实现：stream yield_per，每 chunk 检查累计耗时。
    """
    from sqlalchemy import select
    from db.models import EntityMention
    from datetime import datetime, timedelta, timezone

    start = time.time()
    total = 0
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=WINDOWS_SECONDS["7d"])

    try:
        with db.get_session() as s:
            stmt = (
                select(EntityMention.entity, EntityMention.ts)
                .where(EntityMention.ts >= cutoff)
                .order_by(EntityMention.ts.asc())
                .execution_options(yield_per=chunk_size)
            )
            for batch in s.execute(stmt).partitions(chunk_size):
                for entity, ts in batch:
                    self.add(entity, ts.timestamp())
                    total += 1
                elapsed = time.time() - start
                if elapsed > max_seconds:
                    logger.error(
                        "sliding-counter backfill failed: 超过 {}s 硬上限，已回填 {} 条，强制中止",
                        max_seconds, total,
                    )
                    return False, total, elapsed

        elapsed = time.time() - start
        if elapsed > 120:
            logger.warning(
                "sliding-counter backfill 慢速成功：耗时 {:.1f}s，回填 {} 条",
                elapsed, total,
            )
        else:
            logger.info(
                "sliding-counter backfill 完成：耗时 {:.1f}s，回填 {} 条",
                elapsed, total,
            )
        return True, total, elapsed

    except Exception as e:
        elapsed = time.time() - start
        logger.error("sliding-counter backfill failed: {} (耗时 {:.1f}s)", e, elapsed)
        return False, total, elapsed
```

**启动时调用位置**：在 `main.py` 里，`Database` / `SlidingCounter` 创建之后、`Jobs.start()` 之前：

```python
# main.py
sliding_counter = SlidingCounter()
ok, total, elapsed = sliding_counter.backfill_from_db(db)
if not ok:
    logger.error("回填失败，Hotness 本轮会跳过，下一轮重试")
# 无论 ok 与否，进程继续启动（Req 6.7 约定）
```

**Hotness 本轮跳过实现**：`HotnessService.run_once` 检查一个 flag（见 §3.7）。

---

### 3.7 Hotness_Service（Req 7）

**文件**：`services/l2_hotness.py`（新增）

**15 分钟整点对齐算法**：

```python
from datetime import datetime, timedelta, timezone

def align_to_quarter(dt: datetime) -> datetime:
    """向下对齐到 :00 / :15 / :30 / :45"""
    minute = (dt.minute // 15) * 15
    return dt.replace(minute=minute, second=0, microsecond=0)
```

**run_once 主流程**：

```python
@dataclass
class HotnessService:
    db: Database
    mentions_repo: EntityMentionsRepo
    hotness_repo: HotnessSnapshotsRepo
    sliding_counter: SlidingCounter
    top_k: int = 20
    smoothing: float = 2.0
    short_hours: int = 1
    baseline_days: int = 7
    min_baseline_count: int = 100
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"))

    # 状态：上次写入的 window_end，避免重复处理同一刻
    _last_window_end: Optional[datetime] = None
    # 由 main.py 在 backfill 后注入：True=可运行，False=回填失败本轮跳过
    _counter_ready: bool = True

    def run_once(self) -> bool:
        if not self._counter_ready:
            logger.info("hotness skipped: sliding counter not ready")
            self._counter_ready = True  # 下轮允许再次尝试
            return False

        now = datetime.now(self.timezone)
        window_end = align_to_quarter(now)

        # 不到 15 分钟整点就跳过
        if self._last_window_end is not None and window_end <= self._last_window_end:
            return False
        # 如果 now 离 window_end 小于 30 秒（刚过整点），处理；否则等下一轮触发点
        # 简化：直接用 align 后的 window_end 做 key，内部幂等

        # 基线充足性检查
        with self.db.get_session() as s:
            baseline_count = self.mentions_repo.count_since(
                s, window_end - timedelta(days=self.baseline_days)
            )
        if baseline_count < self.min_baseline_count:
            logger.info(
                "hotness skipped: baseline data insufficient (count={} < {})",
                baseline_count, self.min_baseline_count,
            )
            return False

        # 开始计算
        start_t = time.time()
        candidates = self.sliding_counter.active_entities("24h")

        records = []
        for entity in candidates:
            short_count = self.sliding_counter.count(entity, "1h")
            if short_count == 0:
                continue

            # baseline 需要查 entity_mentions（SlidingCounter 只保证 7d 内，用 DB 更稳）
            with self.db.get_session() as s:
                baseline_total = self.mentions_repo.count_for_entity(
                    s, entity,
                    start=window_end - timedelta(days=self.baseline_days),
                    end=window_end - timedelta(hours=self.short_hours),
                )
                cross_source = self.mentions_repo.count_sources_for_entity(
                    s, entity,
                    start=window_end - timedelta(hours=self.short_hours),
                    end=window_end,
                )
            baseline_hours = self.baseline_days * 24 - self.short_hours
            baseline_per_hour = baseline_total / baseline_hours

            growth_rate = short_count / max(baseline_per_hour, self.smoothing)
            final_score = growth_rate * (1 + 0.3 * (cross_source - 1))
            is_new = (baseline_total == 0 and short_count >= 5)

            records.append({
                "entity": entity,
                "count_short": short_count,
                "count_baseline": baseline_per_hour,
                "growth_rate": growth_rate,
                "cross_source": cross_source,
                "is_new_entity": is_new,
                "final_score": final_score,
            })

        # 排序（Req 7.10 稳定排序）
        records.sort(key=lambda r: (-r["final_score"], -r["count_short"], r["entity"]))
        top = records[: self.top_k]

        # UPSERT
        with self.db.get_session() as s:
            try:
                self.hotness_repo.upsert_batch(
                    s,
                    window_end=window_end,
                    window_type="1h",
                    records=[{**r, "rank": i + 1} for i, r in enumerate(top)],
                )
                s.commit()
            except Exception:
                s.rollback()
                raise

        elapsed = time.time() - start_t
        if elapsed > 60:
            logger.warning("hotness run_once 耗时 {:.1f}s（>60s 警告）", elapsed)
        logger.info(
            "hotness window_end={} top_k={} elapsed={:.1f}s",
            window_end, len(top), elapsed,
        )

        self._last_window_end = window_end
        return True
```

**UPSERT 写入**（`EntityMentionsRepo.upsert_batch`）：

```python
from sqlalchemy.dialects.postgresql import insert

def upsert_batch(self, session, window_end, window_type, records):
    stmt = insert(HotnessSnapshot).values([
        {"window_end": window_end, "window_type": window_type, **r}
        for r in records
    ])
    stmt = stmt.on_conflict_do_update(
        constraint="uq_hotness_snapshots_window_entity",
        set_={
            "count_short": stmt.excluded.count_short,
            "count_baseline": stmt.excluded.count_baseline,
            "growth_rate": stmt.excluded.growth_rate,
            "cross_source": stmt.excluded.cross_source,
            "is_new_entity": stmt.excluded.is_new_entity,
            "final_score": stmt.excluded.final_score,
            "rank": stmt.excluded.rank,
        },
    )
    session.execute(stmt)
```

**性能注**：上面代码每个 entity 查两次 DB（baseline + cross_source），如果候选 entity 达到几千个可能慢。优化方案：一次性用一条 SQL 聚合查所有 entity（用 `GROUP BY entity`），留给实施阶段观测后决定是否优化。Phase 1 先走简单实现。

---

### 3.8 Worker 扩展（Req 8）

**文件**：`scheduler/jobs.py`（扩展）、`main.py`（扩展）

#### 3.8.1 Jobs.__init__ 兼容扩展

```python
# scheduler/jobs.py
class Jobs:
    def __init__(
        self,
        level1_services: Sequence[object],
        level2_services: Sequence[object],
        poll_interval_seconds: int,
        new_services: Sequence[object] = (),  # ★ 新增，默认空元组
    ) -> None:
        self._level1_services = level1_services
        self._level2_services = level2_services
        self._new_services = new_services
        self._poll_interval_seconds = poll_interval_seconds
        self._stop_event = threading.Event()
        self._worker_thread: threading.Thread | None = None
```

**兼容性**：默认值 `()` 保证现有 `Jobs(level1_services, level2_services, poll_interval_seconds)` 三参数调用方式不破。现有 `main.py` 需要显式传 `new_services=[...]`。

#### 3.8.2 _worker_loop 固定顺序 + 异常隔离

```python
def _worker_loop(self) -> None:
    logger.info(
        "worker 启动：level1={} level2={} new={} poll={}s",
        len(self._level1_services), len(self._level2_services),
        len(self._new_services), self._poll_interval_seconds,
    )
    while not self._stop_event.is_set():
        processed_any = False

        # 固定顺序：level1 → level2 → new_services
        # 好处：老链路先消化（LLM 慢活先做），新链路跟在后面不会卡住老的
        for group_name, group in [
            ("level1", self._level1_services),
            ("level2", self._level2_services),
            ("new", self._new_services),
        ]:
            for svc in group:
                if self._stop_event.is_set():
                    break
                try:
                    if svc.run_once():
                        processed_any = True
                except Exception as e:
                    logger.error("{} 服务 {} 异常（已隔离）：{}",
                                 group_name, type(svc).__name__, e)

        if processed_any:
            continue
        self._stop_event.wait(self._poll_interval_seconds)

    logger.info("worker 已停止")
```

**关键不变量**：
- 任意一个 service 抛异常 → 捕获 + log.error + 继续下一个 service（不让单个 service 拖死整个 worker）
- `_stop_event.is_set()` 检查嵌入到最内层循环，保证 shutdown 最多 10 秒内生效
- `shutdown()` 现有实现不动（`join(timeout=10)`）

#### 3.8.3 main.py 注入新 services

```python
# main.py 新增段落（现有 level1_services / level2_services 代码保持不变）
from dictionaries.loader import load_dictionaries
from services.l0_dedup import Deduplicator
from services.l0_normalizer import NormalizerService
from services.l1_entity_extractor import EntityExtractor
from services.l2_sliding_counter import SlidingCounter
from services.l2_hotness import HotnessService
from db.repositories.normalized_messages_repo import NormalizedMessagesRepo
from db.repositories.entity_mentions_repo import EntityMentionsRepo
from db.repositories.hotness_snapshots_repo import HotnessSnapshotsRepo

# 1. 加载词典（失败直接抛，阻止启动）
dicts_dir = base_dir / "dictionaries"
load_dictionaries(dicts_dir)  # Req 3.5：格式错直接 raise

# 2. 构造新仓储
normalized_repo = NormalizedMessagesRepo()
mentions_repo = EntityMentionsRepo()
hotness_repo = HotnessSnapshotsRepo()

# 3. 构造 SlidingCounter + 启动回填
sliding_counter = SlidingCounter()
ok, total, elapsed = sliding_counter.backfill_from_db(db)
logger.info("sliding counter 回填：ok={} total={} elapsed={:.1f}s", ok, total, elapsed)

# 4. 构造新 services
dedup = Deduplicator(
    hamming_threshold=settings.dedup_hamming_threshold,
    window_hours=settings.dedup_window_hours,
)
normalizer_service = NormalizerService(
    db=db, normalized_repo=normalized_repo, dedup=dedup,
    batch_size=settings.normalizer_batch_size, timezone=settings.timezone,
)
entity_extractor = EntityExtractor(
    db=db, normalized_repo=normalized_repo, mentions_repo=mentions_repo,
    sliding_counter=sliding_counter, batch_size=settings.entity_extractor_batch_size,
)
hotness_service = HotnessService(
    db=db, mentions_repo=mentions_repo, hotness_repo=hotness_repo,
    sliding_counter=sliding_counter,
    top_k=settings.hotness_top_k, smoothing=settings.hotness_smoothing,
    short_hours=settings.hotness_short_hours, baseline_days=settings.hotness_baseline_days,
    min_baseline_count=settings.hotness_min_baseline_count,
    timezone=settings.timezone,
)
hotness_service._counter_ready = ok  # 回填失败则本轮跳过

# 5. 注入 Jobs
jobs = Jobs(
    level1_services=level1_services,
    level2_services=level2_services,
    poll_interval_seconds=settings.poll_interval_seconds,
    new_services=[normalizer_service, entity_extractor, hotness_service],
)
jobs.start()
```

---

## 4. 配置项变更（`config/settings.py`）

在现有 `Settings` dataclass 中追加以下字段（保持现有字段顺序与分组风格）：

```python
# === 6. Phase 1 新流水线配置 ===

# L0 归一化：每轮最多扫描多少条原始记录（三源合计）
normalizer_batch_size: int = 500

# L0 SimHash 去重
dedup_hamming_threshold: int = 3    # Req 2.2
dedup_window_hours: int = 24        # Req 2.6

# L1 实体抽取：每轮最多处理多少条待处理消息
entity_extractor_batch_size: int = 500

# L2 Hotness 参数
hotness_top_k: int = 20                 # Req 7.5
hotness_smoothing: float = 2.0          # Req 7.2
hotness_short_hours: int = 1
hotness_baseline_days: int = 7
hotness_min_baseline_count: int = 100   # Req 7.7

# SlidingCounter 回填硬上限
sliding_counter_backfill_max_seconds: int = 600  # Req 6.7 情况 C
```

---

## 5. 目录结构与文件清单

```
PomsAI/
├── alembic/                                       [新增]
│   ├── env.py                                     [新增]
│   ├── script.py.mako                             [新增]
│   └── versions/
│       └── 001_phase1_initial.py                  [新增]
├── alembic.ini                                    [新增]
├── config/
│   └── settings.py                                [修改] 追加 10 个字段
├── db/
│   ├── connection.py                              [不动]
│   ├── models.py                                  [修改] 追加 3 个 ORM 类
│   └── repositories/
│       ├── (existing files)                       [不动]
│       ├── normalized_messages_repo.py            [新增]
│       ├── entity_mentions_repo.py                [新增]
│       └── hotness_snapshots_repo.py              [新增]
├── dictionaries/                                  [新增目录]
│   ├── __init__.py                                [新增] 暴露 get_dictionaries()
│   ├── loader.py                                  [新增]
│   ├── tickers.yaml                               [新增]
│   ├── chains.yaml                                [新增]
│   ├── narratives.yaml                            [新增]
│   └── kols.yaml                                  [新增]
├── llm/
│   └── ollama_client.py                           [不动]
├── main.py                                        [修改] 注入新 services
├── prompts/                                       [不动]
├── scheduler/
│   └── jobs.py                                    [修改] 扩展 new_services 参数
├── services/
│   ├── prefilter.py                               [修改] FilterDecision 扩展 + 词典改用 loader
│   ├── level1_service.py                          [不动] ← 关键：保持现状
│   ├── level2_service.py                          [不动]
│   ├── l0_normalizer.py                           [新增]
│   ├── l0_dedup.py                                [新增]
│   ├── l1_entity_extractor.py                    [新增]
│   ├── l2_sliding_counter.py                     [新增]
│   └── l2_hotness.py                              [新增]
├── tests/
│   ├── (existing files)                           [不动]
│   ├── test_l0_normalizer.py                      [新增]
│   ├── test_l0_dedup.py                           [新增]
│   ├── test_dictionary_loader.py                 [新增]
│   ├── test_l1_entity_extractor.py               [新增]
│   ├── test_l2_sliding_counter.py                [新增]
│   └── test_l2_hotness.py                         [新增]
└── requirements.txt                               [修改] 加 simhash + PyYAML + alembic
```

**requirements.txt 新增**：

```
simhash==2.1.2
PyYAML==6.0.2
alembic==1.13.2
```



---

## 6. 部署与迁移步骤

按以下顺序执行，前一步失败则后续步骤全部中止。

### Step 1：准备词典 v1

把 `services/prefilter.py` 中现有的 `COIN_KEYWORDS_EN`（60+ 个）与 `COIN_KEYWORDS_ZH`（7 个）整理到 `dictionaries/tickers.yaml`。**不强制扩充新词**，等 Gate 1 观测后再决定。

其他三个 YAML（`chains.yaml` / `narratives.yaml` / `kols.yaml`）可先写空骨架：

```yaml
# dictionaries/chains.yaml
{}
```

服务能正常启动，WARN 日志会提示"该词典为空"（Req 3.6）。

### Step 2：引入 Alembic 并跑迁移

```bash
pip install alembic==1.13.2 simhash==2.1.2 PyYAML==6.0.2
alembic init alembic
# 编辑 alembic/env.py 让它 import Base from db.models 并读 config/settings.py 的 DB URL
# 把 001_phase1_initial.py 放进 alembic/versions/
alembic upgrade head
```

**验证**：连到 PostgreSQL 检查三张新表存在、索引/约束全部创建、`alembic_version` 表已插入 `001`。

### Step 3：修改 `services/prefilter.py`

- 删除 `COIN_KEYWORDS_EN` / `COIN_KEYWORDS_ZH` 两个模块级常量
- 在模块加载时调用 `get_dictionaries()` 动态构造 `_EN_COIN_RE` / `_ZH_COIN_RE`
- `FilterDecision` 加 `entities` 字段（带默认值）
- `classify` 函数追加实体抽取逻辑，返回带 entities 的 `FilterDecision`

**验证**：跑现有 `tests/test_prefilter.py`，所有老测试必须通过（向后兼容）。

### Step 4：实现新 services + repositories

按 §3 的代码骨架逐个实现：

1. `services/l0_dedup.py`
2. `services/l0_normalizer.py`
3. `services/l2_sliding_counter.py`
4. `services/l1_entity_extractor.py`
5. `services/l2_hotness.py`
6. 对应的 3 个 repositories

### Step 5：修改 `main.py` 注入新 services

按 §3.8.3 的代码段追加到现有 `main()` 里，`Jobs(..., new_services=[...])`。

### Step 6：启动服务并观测冷启动期

```bash
python main.py
```

观察日志中以下关键点（按出现顺序）：

1. `词典加载完成：tickers=XX chains=0 narratives=0 kols=0 aliases=XX`
2. `sliding counter 回填：ok=True total=0 elapsed=0.1s`（首次无数据，瞬间完成）
3. `worker 启动：level1=3 level2=3 new=3 poll=30s`
4. `normalizer` / `entity_extractor` / `hotness` 三个 service 循环触发

**首个小时内**：`hotness skipped: baseline data insufficient` 是正常日志。

### Step 7：等首份有效排行榜产出

当 `entity_mentions` 累积 ≥ 100 条后（根据三源流量，通常 2~12 小时），下一个 15 分钟整点会产出首份 `hotness_snapshots`：

```sql
SELECT window_end, entity, growth_rate, final_score, rank
FROM hotness_snapshots
ORDER BY window_end DESC, rank ASC
LIMIT 20;
```

### Step 8：进入 Gate 1 的 72 小时验证窗口

从 Step 7 产出首份有效排行榜起算 72 小时，按 requirements.md Success Metrics 的 7 条指标验收。

---

## 7. 关键决策与权衡

### 7.1 为什么 SimHash 选 `simhash` 库而不是 `datasketch`

- **语义契合**：Req 2.2 明确用汉明距离 ≤ 3，与 SimHash 天然匹配；`datasketch` 基于 Jaccard / MinHash，阈值与汉明距离不是一一映射。
- **依赖取舍**：`simhash==2.1.2` 只依赖 numpy（传递依赖，约 30MB，纯 Python 代码本身 < 100 行）；`datasketch` 依赖 numpy + scipy + 更多。实测装了 simhash 只多拉了 numpy 一个传递依赖（Task 0.1 验证），且 numpy 在 Phase 2/3 升级 bge-m3 / HDBSCAN 时必然需要，**现在装等于预先铺路**。
- **Phase 3 升级路径**：终极设计文档 §5.2 规划阶段二去重升级到 Embedding（bge-m3 + cosine），`datasketch` 不会被复用，现在装了是浪费。

### 7.2 为什么 Sliding_Counter 用 `deque` 而不是 Redis

requirements.md §15 明确 Non-Goals：**不引入 Redis**。Phase 1 单进程单 worker，进程内 `deque` 已够。重启代价是 7 天内存重建（`backfill_from_db`，≤10 分钟），可接受。Phase 2+ 若需要多进程/多机部署再考虑 Redis（届时 Sliding_Counter 的接口已稳定，切换实现仅改一处）。

### 7.3 为什么 Hotness_Service 用"15 分钟整点对齐"而不是"启动时间对齐"

- **可复现性**：整点对齐让 `window_end` 固定为 `YYYY-MM-DD HH:{00,15,30,45}:00`，运维对排行榜"什么时候看"有明确预期。
- **幂等性**：如果 worker 稍有延迟导致本次触发滑到下一个整点，UPSERT 会覆盖同一个 `(window_end, window_type, entity)`，不产生重复数据。
- **数据对齐**：Gate 1 人工抽检时"挑 3 个整点时刻对比 Twitter 热点"操作更直观。

### 7.4 为什么 `l1_processed_at` 字段放 `normalized_messages` 而不是建中间表

- **省一张表**：中间表只为记一个"已处理"标记，信息量太小。
- **TIMESTAMPTZ 胜 BOOLEAN**：存时间戳而不是布尔，Phase 2 调试时能直接看出"这条消息 L1 处理延迟多久"。
- **索引友好**：`idx_normalized_messages_is_duplicate_l1_processed_at(is_duplicate, l1_processed_at)` 一条索引覆盖 entity_extractor 的全部扫描需求。

### 7.5 为什么新 services 与老 level1/level2 共用同一个 worker 线程

引用方案 4 与终极设计文档的核心约束：**本机 Ollama 同一时刻只能高效驻留一个模型**。即使 Phase 1 不调 LLM，Phase 2 会加回来；现在共用线程是为了保留这个约束，避免未来 Phase 2 要重新改调度。scheduler/jobs.py 现有实现已经把这点做对，新 services 插入后这个性质不破。

### 7.6 为什么 Phase 1 不做 Aho-Corasick / 高性能字符串匹配

prefilter 的词典匹配目前用 `substring in text_lower`，词典 100~500 条时性能够用（单条消息处理 < 1ms）。Phase 1 batch_size=500，整轮 < 1s，远远不是瓶颈。Aho-Corasick 实现复杂且引入 C 扩展依赖，**ROI 不合算**。留给 Phase 3 如果词典扩到上万条再做。

---

## 8. 风险与兜底

### 风险 A：三源历史数据巨大导致回填超过 10 分钟硬上限

**触发条件**：`entity_mentions` 累积了几千万条，`backfill_from_db` 扫不完。

**兜底**：
1. Req 6.7 情况 C 已规定：强制中止 + ERROR 日志 + 下一轮重试 + 进程继续启动。
2. 运维手段：若观测到多轮回填都失败，可人工 `DELETE FROM entity_mentions WHERE ts < NOW() - INTERVAL '7 days'`（反正 7 天外的数据对 Hotness 没用）。
3. Phase 2 考虑：分区表（按周分区）+ 回填只扫最近分区。

### 风险 B：词典 v1 覆盖率不足导致排行榜全是 `$TICKER`

**触发条件**：运维只迁移了 67 个硬编码词，没补足新叙事词（AI Agent、RWA 等），结果 `entity_mentions` 绝大多数 confidence=0.95（正则命中）而非 1.0（词典命中）。

**兜底**：
1. Gate 1 期间人工监控 `SELECT confidence, COUNT(*) FROM entity_mentions GROUP BY confidence`，如果 1.0 比例 < 30%，扩充词典。
2. **不开启小模型兜底**（Non-Goals §10），那是 Phase 3 的事。
3. Phase 1 即便如此也能产出排行榜，只是实体维度偏向代币、少了叙事维度。

### 风险 C：新流水线抢占老链路的 DB 连接资源

**触发条件**：`connection.py` 当前 `pool_size=5, max_overflow=5`（共 10 个连接），老的 6 个 service（Level1×3 + Level2×3）+ 新的 3 个 service 同时跑可能吃满。

**兜底**：
1. Gate 1 指标 6 明确监控老链路产出不下降超过 10%。
2. 若观测到老链路被拖慢，在 `connection.py` 把 `pool_size=10, max_overflow=10`（共 20），改动极小。
3. Phase 1 新 services 每个 `run_once` 多次开关 session，避免长期占用连接（已在代码里体现，每次事务完 `with` 退出）。

### 风险 D：`Hotness_Service` 单轮 DB 查询过多导致超时

**触发条件**：`active_entities` 返回几千个候选，每个都要查 `count_for_entity` + `count_sources_for_entity`，累计查询数上万。

**兜底**：
1. Req 7.8 已规定：单轮耗时 > 60s 产生 WARN 日志。
2. 优化方案（留给实施阶段观测后决定）：用一条聚合 SQL 批量查：

```sql
SELECT entity,
       COUNT(*) FILTER (WHERE ts >= :base_start AND ts < :base_end) AS baseline_cnt,
       COUNT(*) FILTER (WHERE ts >= :short_start AND ts < :short_end) AS short_cnt,
       COUNT(DISTINCT raw_source) FILTER (WHERE ts >= :short_start AND ts < :short_end) AS sources_cnt
FROM entity_mentions
WHERE ts >= :base_start AND ts < :short_end
  AND entity = ANY(:entity_list)
GROUP BY entity;
```

### 风险 E：Alembic 迁移破坏现有 5 张表

**触发条件**：实施时 `env.py` 配错，导致 Alembic 把现有表也 autogenerate 进迁移。

**兜底**：
1. 迁移文件 `001_phase1_initial.py` **手写**，不用 `--autogenerate`。只含三个 `create_table` + 对应索引/约束。
2. Review 阶段检查：迁移只对三张新表操作，没有 `op.drop_*` / `op.alter_column` 现有表。
3. 首次执行前先在测试库跑，`pg_dump --schema-only` 前后对比现有 5 张表 schema 应完全一致。

---

## 9. 测试策略（Phase 1 MVP 粒度）

### 9.1 单元测试（必做）

| 测试文件 | 覆盖对象 | 关键 case |
|---------|---------|----------|
| `tests/test_dictionary_loader.py` | `dictionaries/loader.py` | 正常加载 / YAML 语法错（raise） / 空文件（WARN 允许启动） / 跨文件同名冲突（raise） / 别名冲突（raise） |
| `tests/test_l0_dedup.py` | `services/l0_dedup.py` | 完全相同文本 → is_dup=True / 改一个字符 → 汉明距离 ≤ 3 仍判重 / 完全不同文本 → is_dup=False / 超过 24 小时的旧桶被清理 |
| `tests/test_l0_normalizer.py` | `NormalizerService` | Discord author 拼接 `#ch @user` / 空 content 跳过 / 幂等：同一条原始记录反复归一化最终只一条 |
| `tests/test_l2_sliding_counter.py` | `SlidingCounter` | add 后立即 count ≥1 / 超过窗口时长的 ts 被懒清理 / 多窗口计数独立 / active_entities 正确返回 |
| `tests/test_l2_hotness.py` | `HotnessService` | 公式边界：baseline=0 且 short_count=5 → is_new=True / growth_rate 分母用 SMOOTHING 保护 / 排序稳定性（final_score 相等时按 count_short 和 entity 二/三排序） / baseline_count < 100 时降级跳过 |
| `tests/test_l1_entity_extractor.py` | `EntityExtractor` + 改造后的 `prefilter.classify` | 正则 + 词典双命中 confidence 取 1.0 / 0 Entity 消息仍被标记 l1_processed_at / 幂等：同消息反复抽取 entity_mentions 不重复 |

### 9.2 集成测试（1 个就够）

`tests/test_phase1_pipeline.py`：

- 使用 **SQLite in-memory**（SQLAlchemy 允许本地测试用不同方言，Alembic 不跑，直接 `Base.metadata.create_all`）
- 注入一批假数据到 `twitter_posts` + `binance_square_posts` + `discord_messages`
- 手动触发 `NormalizerService.run_once()` → `EntityExtractor.run_once()` → `HotnessService.run_once()`
- 断言 `hotness_snapshots` 出现预期实体（预先构造数据让某个实体在短窗 vs 基线有明确的 growth_rate）
- 断言排行榜 `rank=1` 的实体确实是增长率最高的

### 9.3 不做的测试（Phase 1 不值得）

- **E2E 测试**：跑真实 Ollama 的老链路 + 真实三源数据的端到端验证——Phase 1 主要是"新链路跑通"，E2E 成本高收益低。
- **压力测试**：Phase 1 数据量小（72 小时 Gate 1），不需要负载测试。
- **老 prefilter 测试改造**：现有 `tests/test_prefilter.py` 保持不动，只做回归，不加新 case（新 case 归入 `test_l1_entity_extractor.py`）。

### 9.4 验证清单（Gate 1 开始前跑一遍）

```bash
# 1. 所有单元测试绿
pytest tests/test_dictionary_loader.py tests/test_l0_* tests/test_l1_* tests/test_l2_* -v

# 2. 老测试全部绿（回归）
pytest tests/test_prefilter.py tests/test_level1_service.py tests/test_level2_service.py -v

# 3. 集成测试绿
pytest tests/test_phase1_pipeline.py -v

# 4. 类型检查（如果项目用 mypy/pyright；当前仓库未看到配置，跳过）

# 5. Alembic 迁移本地演练
alembic upgrade head
alembic downgrade -1  # 验证 downgrade 可用
alembic upgrade head  # 再升一次，验证幂等
```

---

## 附录：与终极设计文档的映射

本 design.md 是终极设计文档的 **Phase 1 子集落地版**，对应关系如下：

| 本文档章节 | 终极设计文档章节 | Phase |
|-----------|-----------------|-------|
| §3.1 Normalizer_Service | §5.1 归一化（normalizer） | Phase 1 |
| §3.2 Deduplicator | §5.2 两阶段去重（阶段一 SimHash） | Phase 1 |
| §3.3 词典加载模块 | §6.1 第二层：词典（YAML 外置） | Phase 1 |
| §3.4 Entity_Extractor | §6 L1 实体抽取 | Phase 1 |
| §3.5 数据库新表 | §12.2 Phase 1 新增核心表 | Phase 1 |
| §3.6 Sliding_Counter | §7.5 滑动窗口计数器 | Phase 1 |
| §3.7 Hotness_Service | §7.1~7.4 核心算法、综合评分、新词检测 | Phase 1 |
| §3.8 Worker 扩展 | §14 调度时序与 worker 改造 | Phase 1 |
| — (未包含) | §7.6 激增告警触发器 | **Phase 2** |
| — (未包含) | §8 L3 共现网络 | **Phase 2** |
| — (未包含) | §9 L4 Embedding 聚类 | **Phase 2** |
| — (未包含) | §10 L5 LLM 定向简报 | **Phase 2** |
| — (未包含) | §11 L6 产出与警报 | **Phase 2/3** |

---

*文档版本：v1.1（实施期校准）*
*依赖：requirements.md v1.2（High/Medium 修复后）*
*下一步：tasks.md（基于本 design 产出可勾选的 Phase 1 实施任务清单）*

**v1.0 → v1.1 变更**：
- §3.3 词典加载：明确 `DictionaryEntry.entity_type` 由**所在文件**硬决定（ticker/chain/narrative/kol），新增 `DictionaryEntry.category` 字段承载 YAML 里的 `type:` 细分类值（原先 `entity_type=cfg.get("type", ...)` 的写法会让 BTC 的 `entity_type` 被污染为 `"layer1"`，违反 Req 4.3 五类约束）
- §3.3 YAML schema 样例：增加顶部"命名约定"说明 + 每个文件标注 file-scope entity_type；kols.yaml 的 `category:` 改为 `type:` 保持字段名统一
- §7.1 SimHash 选型：修正"无额外依赖"描述，承认 numpy 是传递依赖但论证可接受
- §7.4 索引命名：`idx_normalized_messages_unproc` → `idx_normalized_messages_is_duplicate_l1_processed_at`（与 tasks.md / models.py 对齐）
