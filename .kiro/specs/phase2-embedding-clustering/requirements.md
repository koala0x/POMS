# Phase 2 · Task 2.6 L4 Embedding 聚类 · Requirements

> Phase 2 路线图里**工程量最大**、**唯一一个明确突破"零新依赖"硬约束**的子任务。
> 在 Phase 1 SimHash 去重的基础上叠加一层语义级降噪：把"同一新闻的不同表述"
> 自动归并成事件簇，让 hotness 公式不再被同一新闻多次转发误导。

## 背景

Phase 1 用 SimHash 做精确去重，汉明距离 ≤ 3 视为重复。但 SimHash 抓不到
**字面不同但语义相同**的消息：

```
消息 A："$BTC 突破 70k 历史新高！"
消息 B："比特币创新高，70000 美元"
消息 C："BTC just hit new ATH at 70K"
消息 D："Bitcoin 大涨，破 7 万"
```

四条消息说的是同一件事，但 SimHash 视为 4 条独立消息——hotness 公式中的
`count_short` 被高估为 4，growth_rate 跟着虚高。这种"同一新闻被多次转发"
的场景在 Twitter 数据源里非常常见（一条新闻可能被几十个账户转发改写），
当前系统会把它误判成"BTC 突然热"。

**Task 2.6 目标**：用 bge-m3 把每条消息向量化，HDBSCAN 把过去 24h 的消息
聚成事件簇，每簇只保留一条代表消息，其它标记为重复（`is_duplicate=True`）。

下游的 EntityExtractor / HotnessService **完全不感知**聚类的存在——它们只看
`is_duplicate=False` 的消息，自动只统计代表消息。这是 SimHash 机制的语义升级，
不替代 SimHash（两者协同：先 SimHash 抓完全相同的，再聚类抓语义相同的）。

## 关于"零新依赖硬约束"被突破

Phase 1 / Phase 2.1~2.5 严守"零新依赖"——`requirements.txt` 全程不动。
Task 2.6 **明确突破**这条硬约束：

```
新增依赖：
  sentence-transformers  ~3.x   ★ 用 bge-m3 做向量化
  hdbscan                ~0.8.x ★ 聚类算法
  pgvector               ~0.3.x ★ ORM 适配 PG vector 类型
  torch (CPU)            ~2.x   ★ sentence-transformers 传递依赖
```

预估装包后磁盘 +1.5 GB（torch 占大头），首次启动下载 bge-m3 模型 ~600 MB，
CPU 模式向量化每条 ~50ms。

**用户必须在实施前做决策（详见 design §8）**：
- **方案 A 接受新依赖**：本 spec 默认方案
- **方案 B 走 Ollama embedding API**：HTTP 调远端 bge-m3，不本地装 torch
  （需先验证 Ollama 端 embedding 接口是否可用）
- **方案 C 跳过本任务**：只做 2.5 共现网络（共现也能部分降噪）

关于"零 LLM 硬约束"的特别说明：**bge-m3 是 embedding 模型，不生成 token，
与 LLM 推理（生成式）有本质区别**——本任务**不**违反"零 LLM"硬约束。
真正违反的只是"零新依赖"硬约束。

## 用户角色

- **唯一用户**：项目所有者（你，单人开发者，做加密货币早期热点发现）
- **设备**：本地 Mac mini（24/7 跑 worker）+ Telegram App
- **使用场景**：希望 hotness 榜单不再被"同一新闻被刷屏"误导，让真正"独立信号"
  浮出水面

### 关于 ROI 的诚实声明

本任务的价值是**间接**的——它**不直接产生新信号**，而是让其它信号更清晰。
四个 Phase 2 子任务里 **ROI 中等偏低**（只比 LLM 简报高），**应该在
2.4/2.5 上线后再观察"是否真有降噪需求"**。

如果 2.4/2.5 上线后发现：
- hotness 榜单已经够干净（Top-K 里很少出现"同一新闻多个 token 来源"的情况）
- 共现网络已经能识别"几个 token 在同一新闻里被一起提"

→ 本任务可以直接跳过，省 1~2 周工程量。

## 边界与非目标

### 包含

1. 两张新表 `message_embeddings` / `event_clusters`（DDL + ORM + repo）
2. 新依赖 `sentence-transformers / hdbscan / pgvector / torch`
3. PostgreSQL 启用 `pgvector` 扩展（`CREATE EXTENSION vector`）
4. 一个新封装 `llm/embedder_client.py` —— `BgeM3Embedder`
5. 两个新服务：`EmbedderService`（向量化）+ `ClusteringService`（聚类）
6. 8 个新配置字段（`embedding_*` / `clustering_*`）
7. 与 SimHash 协同：聚类后通过 `is_duplicate=True` + `dup_of` 标记非代表消息
8. 单元测试 + 集成测试覆盖核心路径

### 不包含（Phase 3 / 永不实施）

1. ❌ 用 Embedding 替代 SimHash（两者协同不替代——SimHash 抓字面，
   Embedding 抓语义）
2. ❌ Embedding 替代 hotness 公式 / 共现网络 / 任何信号产生链路
3. ❌ GPU 模式（用户 Mac mini 没独立 GPU）—— 本 spec 默认 CPU
4. ❌ 实时向量化（消息一进来就算 embedding）—— 本 spec 走批量异步，
   每轮 worker 拉一批未向量化消息处理
5. ❌ 跨语言对齐微调（bge-m3 已经多语言，开箱即用即可）
6. ❌ Embedding 历史数据回填到 Phase 1 已有的 4943 条消息——
   首次启动会自动按"未向量化"扫描覆盖到全部历史
7. ❌ 聚类质量自动评估（Phase 3 真有需求时再做"silhouette 自动报警"）
8. ❌ 聚类簇命名（聚类只输出 cluster_id，不给簇起名；Phase 2.7 LLM 简报里再做）

## Requirements

### Req 1：数据模型

#### 1.1 新增 `message_embeddings` 表

```sql
CREATE EXTENSION IF NOT EXISTS vector;  -- ★ 必须先启用

CREATE TABLE message_embeddings (
    msg_id          BIGINT PRIMARY KEY,           -- 逻辑引用 normalized_messages.id
    embedding       vector(1024) NOT NULL,        -- bge-m3 输出 1024 维
    model_version   VARCHAR(32) NOT NULL,         -- "bge-m3-v1"
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_msg_embeddings_hnsw
    ON message_embeddings USING hnsw (embedding vector_cosine_ops);
```

设计要点：
- `msg_id` 主键 + 逻辑引用 `normalized_messages.id`（不建外键，与 Phase 1 同款）
- `model_version` 字段允许未来切换模型时区分（例如 "bge-m3-v2"）
- HNSW 索引支持 cosine 最近邻查询，10 万行规模下查询 < 10ms

#### 1.2 新增 `event_clusters` 表

```sql
CREATE TABLE event_clusters (
    id                      BIGSERIAL PRIMARY KEY,
    cluster_id              VARCHAR(64) NOT NULL UNIQUE,  -- "evt_<window_end>_<n>"
    window_end              TIMESTAMPTZ NOT NULL,
    msg_ids                 BIGINT[] NOT NULL,             -- 簇内所有 msg_id
    representative_msg_id   BIGINT NOT NULL,               -- 代表消息（簇中心最近）
    centroid                vector(1024),                  -- 簇质心
    cluster_size            INTEGER NOT NULL,
    cohesion                FLOAT,                         -- 簇内平均 cosine
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_event_clusters_window ON event_clusters(window_end DESC);
```

#### 1.3 ORM 与 Repo

应在 `db/models.py` 新增 `MessageEmbedding` 与 `EventCluster` 两个 ORM。

应新增两个 repo：
- `db/repositories/message_embeddings_repo.py`：
  - `bulk_insert(session, records)` —— ON CONFLICT DO NOTHING
  - `fetch_unembedded_msg_ids(session, limit)` —— 拉未向量化消息
  - `fetch_embeddings_in_window(session, since, until) -> list[(msg_id, np.ndarray)]`
- `db/repositories/event_clusters_repo.py`：
  - `upsert_for_window(session, window_end, clusters)` —— 同窗口走覆盖
  - `fetch_recent(session, window_end)`

#### 1.4 不动现有 schema

`normalized_messages.is_duplicate` / `dup_of` 字段已经在 Phase 1 schema 里——
ClusteringService 只更新值不改 schema。

### Req 2：BgeM3Embedder 接口

应新增 `llm/embedder_client.py`，提供 `BgeM3Embedder` frozen dataclass。

2.1 构造参数：
- `model_name: str = "BAAI/bge-m3"`
- `cache_dir: Path` —— 默认 `~/.cache/huggingface`（与 sentence-transformers 默认一致）
- `device: str = "cpu"` —— Mac mini 没 GPU，默认 CPU

2.2 `__post_init__` 加载模型：
- `SentenceTransformer(model_name, cache_folder=str(cache_dir), device=device)`
- 首次启动会下载 ~600MB 模型，启动日志要打 INFO

2.3 公开方法 `encode_batch(texts: list[str]) -> np.ndarray`：
- 输入 N 条文本，输出 (N, 1024) 的 float32 数组
- 内部用 `model.encode(texts, batch_size=32, normalize_embeddings=True)`
- `normalize_embeddings=True` 保证输出是单位向量，cosine 相似度 = 点积，加速

2.4 公开方法 `cosine(a: np.ndarray, b: np.ndarray) -> float`：
- 点积；输入要求都是单位向量（已 normalize）

2.5 模块级 import 必须**懒加载**：
```python
# 模块顶部不直接 import sentence_transformers，让 import 失败时仍能启动
def _load_model(model_name, cache_dir, device):
    from sentence_transformers import SentenceTransformer  # ★ 懒加载
    return SentenceTransformer(model_name, cache_folder=str(cache_dir), device=device)
```
原因：用户决策方案 C（跳过本任务）时不会装 sentence-transformers，
模块级 import 会让全项目启动崩溃。

### Req 3：EmbedderService 接口

应新增 `services/l4_embedder.py`，提供 `EmbedderService` dataclass。

3.1 构造参数：
- `db / normalized_repo / embeddings_repo / embedder` —— 依赖注入
- `batch_size: int = 32`

3.2 公开方法 `run_once() -> bool`：
- 拉 N 条 `is_duplicate=False AND id NOT IN (SELECT msg_id FROM message_embeddings)`
  的消息（详见 Req 1.3 `fetch_unembedded_msg_ids`）
- `len(msgs) == 0` → 返回 False
- 调 `embedder.encode_batch(texts)` 一次性向量化
- 批量 INSERT 到 message_embeddings
- 失败回滚 + 下一轮重试（与 Phase 1 同款幂等模式）
- 至少处理一条返回 True

3.3 状态字段：无（无状态）

3.4 `is_duplicate=True` 的消息**不向量化**——SimHash 已经判它是重复，
没必要再算 embedding，省 CPU 与磁盘。

### Req 4：ClusteringService 接口

应新增 `services/l4_clustering.py`，提供 `ClusteringService` dataclass。

4.1 构造参数：
- `db / embeddings_repo / clusters_repo / normalized_repo`
- `min_cluster_size: int = 3`
- `min_samples: int = 2`
- `window_hours: int = 24`
- `timezone: ZoneInfo`

4.2 公开方法 `run_once() -> bool`：
- 用 `align_to_quarter(now)` 计算 window_end（与 HotnessService 同步对齐）
- 与 `_last_window_end` 比较，相同跳过
- 拉过去 `window_hours` 小时所有有 embedding 的消息
- 数量 < `min_cluster_size * 5` → 跳过 + INFO 日志
- 调内部 `_cluster_window(embeddings)` 跑 HDBSCAN
- 每个 cluster 选代表（与 centroid cosine 最大）
- UPSERT event_clusters
- 把 cluster 内非代表的 msg 标记 `is_duplicate=True, dup_of=代表 id`
- **绝不**覆盖已有 `dup_of`（SimHash 已标记的不动，与 Req 5 一致）

4.3 内部方法 `_cluster_window(embeddings: np.ndarray) -> dict[int, list[int]]`：
- HDBSCAN(min_cluster_size, min_samples, metric="cosine") 聚类
- 返回 `dict[cluster_label, list[msg_id]]`
- 噪声点 cluster_label=-1 直接跳过（不入簇 = 不标记重复）

4.4 内部方法 `_select_representative(cluster_msg_ids, embeddings)` -> int：
- 计算簇内所有消息向量的 centroid
- 找与 centroid cosine 最大的 msg_id 作代表

4.5 内部方法 `_compute_cohesion(cluster_msg_ids, embeddings)` -> float：
- 簇内所有消息对的平均 cosine 相似度
- 簇质量评估指标：cohesion < 0.5 时 INFO 日志提示"低质量簇"

### Req 5：与 SimHash 协同（关键）

5.1 ClusteringService 标记 `is_duplicate=True` 时**只覆盖** `dup_of IS NULL` 的行：

```sql
UPDATE normalized_messages
SET is_duplicate = TRUE,
    dup_of       = :representative_msg_id
WHERE id = ANY(:non_representative_ids)
  AND dup_of IS NULL;     -- ★ 关键：不覆盖 SimHash 已标记的
```

5.2 这意味着 SimHash 与 Embedding 聚类**互不干扰**：
- SimHash 已标记的（字面相同）→ 保持原样
- SimHash 未标记的（字面不同但语义相同）→ Embedding 聚类标记
- 两者都未标记的 → 仍是 `is_duplicate=False`，下游 EntityExtractor / HotnessService 看到的"独立信号"

5.3 下游服务**完全不感知聚类**：

```
EntityExtractor.run_once()
  → fetch_unprocessed_for_l1: WHERE is_duplicate=FALSE AND l1_processed_at IS NULL
                                    ↑ Phase 1 已有的 SQL，自动只拉代表消息
```

这是本任务**最重要的不变量**：聚类完全旁挂，不改任何 Phase 1/2.x service。

### Req 6：配置（NewPipelineSettings 扩展 8 字段）

应在 `config/_new.py` 末尾追加：

```python
# Phase 2.6 Embedding 聚类
embedding_enabled: bool = True
embedding_model: str = "BAAI/bge-m3"
embedding_batch_size: int = 32
clustering_enabled: bool = True
clustering_window_hours: int = 24
clustering_min_cluster_size: int = 3
clustering_min_samples: int = 2
clustering_dim_reduction: bool = False  # Phase 3 用 UMAP 降维加速预留位
```

字段加载验证：
```bash
.venv/bin/python -c "from config.settings import get_settings; s=get_settings(); \
  print(s.embedding_enabled, s.embedding_model, s.clustering_min_cluster_size)"
```
预期：`True BAAI/bge-m3 3`

### Req 7：与 Worker 集成

7.1 `main.py` 新增构造代码（在 hotness_services 之后、AlertTriggerService 之前）：
- 启动时一次性加载 BgeM3Embedder（耗时几分钟下载模型，**不阻塞**主进程
  启动——首次启动可以接受）
- 构造 EmbedderService + ClusteringService
- 加入 `new_services` 列表
- 调度顺序：**EmbedderService 先于 ClusteringService**（embedder 喂数据
  给 clustering）
- ClusteringService **必须在 EntityExtractor 之前**——这样 EntityExtractor
  下一轮才能看到刚被标记的 `is_duplicate=True`，保证只统计代表消息

7.2 推荐顺序：

```
NormalizerService             # L0
  → EmbedderService           # ★ Phase 2.6 新增
    → ClusteringService       # ★ Phase 2.6 新增
      → EntityExtractor       # L1（下一轮看到聚类标记）
        → HotnessService × 3  # L2 多窗口
          → AlertTriggerService
            → BriefingService # Phase 2.7（如启用）
```

7.3 配置缺失即降级：
- `embedding_enabled=False` 或 `clustering_enabled=False` → 跳过该 service 构造
- 装包失败（用户没装 sentence-transformers）→ 启动时 try/except 兜底，
  log.error 后跳过整段 Embedding 模块，hotness 主流程不受影响

### Req 8：测试覆盖（净新增 19 用例）

#### 8.1 `tests/test_embedder_client.py`（4 用例）

1. `test_encode_returns_1024_dim` —— shape == (n, 1024)
2. `test_encode_batch_consistency` —— 同输入两次输出向量相等
3. `test_cosine_similarity_for_known_pairs` —— 同义句 cosine > 0.7
4. `test_handles_chinese_and_english` —— bge-m3 多语言能力

⚠️ 这部分测试会真的下载模型（首次 ~600MB）。如果 CI 环境不便，可以
mock SentenceTransformer 替换成纯单位向量返回。

#### 8.2 `tests/test_l4_embedder.py`（5 用例）

5. `test_embeds_only_unembedded_messages` —— 已有 embedding 的不重复算
6. `test_skips_when_no_pending_messages` —— 无未向量化消息时返回 False
7. `test_batch_size_respected` —— 一次拉 batch_size 条
8. `test_write_failure_rolls_back` —— DB 写失败 rollback，下一轮重试
9. `test_idempotent_on_duplicate_msg_id` —— ON CONFLICT DO NOTHING

#### 8.3 `tests/test_l4_clustering.py`（10 用例）

10. `test_cluster_basic_correctness` —— 构造已知向量验证聚类
11. `test_outliers_not_clustered` —— HDBSCAN 噪声点 label=-1 跳过
12. `test_representative_is_centroid_nearest` —— 代表选择正确
13. `test_marks_non_representative_as_duplicate` —— is_duplicate=True 标记
14. `test_does_not_overwrite_simhash_dup` —— **关键**：已有 dup_of 不覆盖
15. `test_skips_when_window_unchanged` —— 同 window_end 第二次跳过
16. `test_skips_when_too_few_embeddings` —— 数据稀疏跳过
17. `test_cohesion_calculation` —— 簇内平均 cosine 公式
18. `test_handles_empty_window` —— 0 条 embedding 时优雅返回
19. `test_upsert_overwrites_same_window` —— 同窗口跑两次 event_clusters 行数一致

#### 8.4 测试约束

- 不允许真连 PG（用 SQLite 内存库）
- 不允许真调 Ollama / 外部服务
- 测试 14（不覆盖 SimHash dup）是**最关键的回归保护**

### Req 9：日志规范

9.1 启动 INFO：
```
BgeM3Embedder 加载中：BAAI/bge-m3 (cache=/Users/.../huggingface, device=cpu)
BgeM3Embedder 加载完成：dim=1024, model_version=bge-m3-v1
EmbedderService 启动：batch_size=32
ClusteringService 启动：window_hours=24 min_cluster_size=3 min_samples=2
```

9.2 运行 INFO：
```
embedder 本轮：处理 32 条消息 → 写入 32 条 embedding（耗时 1.6s）
clustering window_end=... clusters=N representatives=M cohesion_avg=0.72 elapsed=2.3s
```

9.3 跳过 INFO：
```
clustering skipped: window unchanged
clustering skipped: too few embeddings (count=8 < 15)
embedder skipped: no pending messages
```

9.4 错误 ERROR：
```
embedder failed: <reason>
clustering failed: <reason>
BgeM3Embedder 加载失败：<reason>，跳过 EmbedderService 与 ClusteringService 构造
```

## Success Metrics

### 阶段验收（部署后 24 小时内）

- [ ] **测试基线**：`pytest` 100% pass，135 → 154（+19，0 回归）
- [ ] **服务启动**：日志含 `EmbedderService` / `ClusteringService` 启动行
- [ ] **历史向量化完成**：4943 条历史消息全部进入 `message_embeddings`
      （`SELECT count(*) FROM message_embeddings` ≥ 4943）
- [ ] **首份事件簇产出**：等下一个 quarter，`event_clusters` 至少 1 条记录

### 业务验收（部署后 7~14 天内）

- [ ] **聚类质量人工评估**：随机抽 10 个簇，人工评估"代表消息确实代表本簇语义"
      （≥ 7/10 算合格）
- [ ] **降噪效果可观察**：对比聚类前后同一时刻 1h 榜，热点实体的 `count_short`
      下降（说明同事件消息被合并），长尾"独特讨论"实体相对靠前
- [ ] **不影响主流程**：Embedding 服务挂掉时（手动 kill）hotness 继续产出
- [ ] **资源占用可控**：单轮聚类 < 30s，向量化 < 5s/批

### 反向验证

- [ ] **关闭 Embedding 模块**：`embedding_enabled=False` + `clustering_enabled=False`
      重启后行为与 Phase 2.5 等价（grep 启动日志确认两个 service 都跳过）
- [ ] **不破坏 SimHash**：抽 10 条 `dup_of IS NOT NULL` 的消息，确认它们仍然
      是 SimHash 标记的（不被聚类覆盖）
- [ ] **下游服务零感知**：grep `services/l1_entity_extractor.py` /
      `services/l2_hotness.py` 确认它们没有 import `event_clusters_repo`

## 硬约束（沿用 + 第 3 条明确突破）

### 1. 零 LLM ✅ **不违反**（重要解释）

`bge-m3` 是 embedding 模型，输入文本输出向量，**不生成 token**。这与 LLM 推理
（生成式，逐 token 采样）有本质区别。所以本任务**不违反**"零 LLM"硬约束。
真正的 LLM 调用在 Phase 2.7 简报任务里。

### 2. 不阻塞主流程 ✅

EmbedderService / ClusteringService 失败 → 不影响 hotness / alert / 其它 service。

### 3. ~~零新依赖~~ ❌ **明确突破**

新增 sentence-transformers / hdbscan / pgvector / torch 共 ~1.5 GB。这是 Phase 2
路线图里唯一明确突破"零新依赖"的任务。详细论证见 design §8。

### 4. 不破坏向后兼容 ✅

- 现有 135 个测试 100% pass
- 关闭开关后行为与 Phase 2.5 等价
- 不修改任何 Phase 1 / Phase 2.1~2.5 service

### 5. 配置缺失即降级 ✅

`embedding_enabled=False` 或装包失败 → 跳过整段 Embedding 模块，hotness 主流程
照常工作。

## 依赖与风险

### 依赖

- **PostgreSQL pgvector 扩展**：必须 `CREATE EXTENSION vector` 成功
- **HuggingFace 可达**：首次启动下载 bge-m3 模型 ~600MB（国内服务器需 VPN
  或预先下载好模型缓存）
- **磁盘空间** ≥ 2 GB：模型 600MB + venv 包增量 1.5GB + 数据增长缓冲
- **Phase 1 normalized_messages 表的 is_duplicate / dup_of 字段**（已有）

### 风险

| 风险 | 概率 | 缓解 |
|---|---|---|
| **新依赖 1.5GB 让仓库变重** | 高 | 用户决策（方案 A/B/C）；方案 B 走 Ollama 完全不装本地包 |
| pgvector 不可用 | 高 | DBA 协助安装；或方案 B 用 numpy 文件存向量（性能差但能跑） |
| bge-m3 下载需要 VPN | 高 | 国内服务器需预先下载到 `~/.cache/huggingface` 再启动 |
| CPU 模式向量化慢（每条 ~50ms）| 中 | batch_size=32 + 增量异步处理；4943 条全量首次启动约 4 分钟 |
| HDBSCAN 在大数据量下慢 | 中 | window_hours=24 限制候选；Phase 3 加 UMAP 降维 |
| 聚类把语义相似但**不同事件**的消息合并 | 中 | min_cluster_size=3 + cohesion 阈值；观察后调 |
| message_embeddings 表磁盘占用 | 低 | 1024 × 4B × 1M 行 = 4GB；Phase 3 保留 30 天即可 |
| 与 ClusteringService / HotnessService 共享窗口对齐 | 低 | 都走 align_to_quarter |
| 装包失败（用户没装 / 包冲突） | 低 | main.py try/except 兜底，跳过整段 |

---

*文档版本：v1.0*
*基于：tasks.md v1.0 + 终极设计文档 §9 L4 Embedding 聚类*
*预估工时：实施前补 design + 用户决策 1~2 天 + 编码 5~7 天 ≈ 1~2 周*
*测试基线：135 → 154 passed（+19，0 回归）*
*ROI 评级：⭐⭐⭐ 间接价值（让其它信号更清晰，但不直接产生新信号）*
