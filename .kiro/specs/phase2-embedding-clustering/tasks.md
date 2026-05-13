# Phase 2 · Task 2.6 L4 Embedding 聚类 · Implementation Tasks

> 终极设计文档 §9 L4 Embedding 聚类的最小实现版。把语义相似的消息**聚成事件簇**，
> 让"同一件事的不同说法"被识别成同一个事件。是 Phase 1 SimHash 去重的语义升级。
>
> ⚠️ **本文档是 tasks-only 草案**，实施前必须补 design.md / requirements.md，
> 因为本任务工程量大、引入新依赖、对现有架构改动较多。

---

## 背景

Phase 1 用 SimHash 做精确去重（汉明距离 ≤ 3 视为重复）。但 SimHash 抓不到
**语义相似但字面不同**的消息：

```
消息 A："$BTC 突破 70k 历史新高！"
消息 B："比特币创新高，70000 美元"
消息 C："BTC just hit new ATH at 70K"
```

三条消息说的是同一件事，但 SimHash 视为 3 条独立消息——这让 hotness 公式中的
`count_short` 被高估（同一新闻的转发被算 3 次，growth_rate 虚高）。

**Task 2.6 目标**：用 bge-m3 向量化 + HDBSCAN 聚类，把"同一件事的不同说法"归并成
事件簇，每簇产生一条代表性消息进入下游统计。

## 设计草案

### 数据模型

新增三张表（不动 schema 已有的）：

```sql
-- 消息 embedding 缓存
CREATE TABLE message_embeddings (
    msg_id          BIGINT PRIMARY KEY,        -- FK to normalized_messages.id（逻辑引用）
    embedding       vector(1024) NOT NULL,     -- bge-m3 输出 1024 维
    model_version   VARCHAR(32) NOT NULL,      -- "bge-m3-v1"
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_msg_embeddings_hnsw ON message_embeddings
    USING hnsw (embedding vector_cosine_ops);

-- 事件簇
CREATE TABLE event_clusters (
    id              BIGSERIAL PRIMARY KEY,
    cluster_id      VARCHAR(64) NOT NULL UNIQUE,  -- "evt_<window_end>_<n>"
    window_end      TIMESTAMPTZ NOT NULL,
    msg_ids         BIGINT[] NOT NULL,            -- 簇内所有 msg_id
    representative_msg_id BIGINT NOT NULL,        -- 代表消息（簇中心最近）
    centroid        vector(1024),                 -- 簇质心
    cluster_size    INTEGER NOT NULL,
    cohesion        FLOAT,                        -- 簇内平均 cosine 相似度
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_event_clusters_window ON event_clusters(window_end);
```

⚠️ **依赖 PostgreSQL 扩展 `pgvector`**：本任务必须先在 PG 上启用 pgvector
（`CREATE EXTENSION vector`），否则 vector(1024) 类型不可用。这是**新基础设施依赖**，
比 Phase 1 / 2.x 的"零新依赖"约束更重，需用户决策。

### 新增依赖

```
sentence-transformers==3.x   # bge-m3 推理（CPU 模式，每条 ~50ms）
hdbscan==0.8.x               # 聚类
pgvector==0.3.x              # PG 向量类型 ORM 支持
torch==2.x (CPU)             # sentence-transformers 依赖
```

预估装包后磁盘 +1.5 GB（torch 占大头），首次启动下载 bge-m3 模型 ~600 MB。

### 新增服务

`services/l4_embedder.py` —— `EmbedderService`（向量化）
`services/l4_clustering.py` —— `ClusteringService`（聚类）

```python
# Embedder 流程
class EmbedderService:
    def run_once(self) -> bool:
        """
        每轮拉 N 条 message_embeddings 表里**没有** embedding 的消息，
        批量调 bge-m3.encode(texts, batch_size=32)，写入 message_embeddings。
        """
        ...

# Clustering 流程
class ClusteringService:
    def run_once(self) -> bool:
        """
        每 15 分钟整点：
          1. 拉过去 24h 的所有 embedding（向量化已完成的）
          2. HDBSCAN 聚类（min_cluster_size=3, min_samples=2）
          3. 每个簇选代表消息（cosine 与 centroid 最近的）
          4. UPSERT 到 event_clusters
          5. 把簇内非代表消息的 normalized_messages.is_duplicate 置 True，dup_of=代表
          → hotness 自动只算代表消息，无需改 HotnessService
        """
        ...
```

### 与现有架构的衔接

**关键设计点**：聚类后通过 `is_duplicate=True` + `dup_of` 标记非代表消息，
让 EntityExtractor / HotnessService **完全不感知聚类的存在**——它们只看
`is_duplicate=False` 的消息，自动只统计代表消息。这跟 SimHash 是同一个机制
（向量化是 SimHash 的语义升级，不是替代）。

### 五条硬约束的重新评估

| 约束 | 本任务的妥协 |
|---|---|
| 零 LLM | ✅ bge-m3 是 embedding 模型，**不算 LLM**（不生成 token） |
| 不阻塞主流程 | ✅ EmbedderService / ClusteringService 失败不影响 hotness |
| **不引入新依赖** | ❌ **必须违反**：sentence-transformers / hdbscan / pgvector / torch |
| 不破坏向后兼容 | ✅ is_duplicate 机制已在，新增向量化只是补充 |
| 配置缺失即降级 | ✅ `embedding_enabled=False` 时跳过 |

**用户决策点**：第 3 条硬约束被打破。是否接受？参考方案：

- **A. 接受新依赖**（本任务方案）
- **B. 用 Ollama API 调 bge-m3**（仍然用 HTTP，避免本地 torch）—— 但 Ollama
  embedding API 在你环境下是否可用需先验证
- **C. 跳过本任务**，只做 2.5 共现网络（共现也能部分解决"同一件事被多次提"
  的问题，因为同一新闻多个 token 会一起出现）

---

**执行约定**：
- 每完成一个 Task 跑测试，pass 数只增不减
- 测试基线起点：**135 passed**（Phase 2.1 完工状态）
- 全部 Task 完成后落点：**154 passed**（+19，0 回归）
- 本任务工程量大，建议分两期实施：Task 1~5 第一期（向量化基础），Task 6~9 第二期（聚类与集成）

---

## Task 0：可行性 + 基础设施

- [ ] **0.1 跑 pytest 确认基线 135 passed**
- [ ] **0.2 用户决策：是否启用 pgvector**
  - PG 上跑 `CREATE EXTENSION vector;` 测试是否可用
  - 不可用 → 申请 DBA 协助安装 / 切到方案 B（Ollama embedding）/ 跳过本任务
- [ ] **0.3 用户决策：是否接受新依赖**
  - 装包后磁盘 +1.5 GB
  - 首次启动下载 bge-m3 模型 ~600 MB
  - CPU 模式每条 ~50ms（4943 条历史数据全量向量化约 4 分钟）
- [ ] **0.4 决策：方案 A / B / C**

## Task 1：依赖安装 + pgvector schema

- [ ] **1.1 装新依赖**
  - `.venv/bin/python -m pip install sentence-transformers hdbscan pgvector`
  - 不要直接 `pip install`（venv shebang 问题）
  - 验证 `python -c "from sentence_transformers import SentenceTransformer; print('ok')"`
- [ ] **1.2 验证 pgvector**
  - `psql -c "CREATE EXTENSION IF NOT EXISTS vector;"`
  - `psql -c "SELECT '[1,2,3]'::vector;"`
- [ ] **1.3 新建 alembic 迁移 `003_phase2_embeddings.py`**
  - 创建 `message_embeddings` + `event_clusters` 两表
  - 加 hnsw 索引
- [ ] **1.4 跑迁移**
  - `alembic upgrade head`
  - 验证两张表结构

## Task 2：bge-m3 客户端封装

- [ ] **2.1 创建 `llm/embedder_client.py`**
  - 注意：放在 llm/ 目录下但**不引入 ollama_client**——这只是 embedding，
    不算 LLM 生成
  - `class BgeM3Embedder` frozen dataclass
  - `__post_init__` 里加载模型（`SentenceTransformer("BAAI/bge-m3")`），
    缓存到 `~/.cache/huggingface`
  - `encode_batch(texts: list[str]) -> list[np.ndarray]`
- [ ] **2.2 单元测试 `tests/test_embedder_client.py`**（4 个用例）
  - test_encode_returns_1024_dim
  - test_encode_batch_consistency（同输入同输出）
  - test_cosine_similarity_for_known_pairs
  - test_handles_chinese_and_english（bge-m3 是多语言模型）
- [ ] **2.3 跑测试**
  - 预期 135 + 4 = **139 passed**
  - ⚠️ 这部分测试会真的下载模型（首次 ~600MB），CI 环境可能要 mock

## Task 3：EmbedderService

- [ ] **3.1 创建 `services/l4_embedder.py`**
  - `EmbedderService` dataclass
  - 字段：db / normalized_repo / embedder（BgeM3Embedder）/ batch_size
  - `run_once()`：
    - 拉 N 条 `is_duplicate=False AND id NOT IN (SELECT msg_id FROM message_embeddings)`
    - 调 embedder.encode_batch
    - 批量 INSERT 到 message_embeddings
- [ ] **3.2 加 repo `db/repositories/message_embeddings_repo.py`**
- [ ] **3.3 单元测试 `tests/test_l4_embedder.py`**（5 个用例）
  - test_embeds_only_unembedded_messages
  - test_skips_when_no_pending_messages
  - test_batch_size_respected
  - test_write_failure_rolls_back
  - test_idempotent_on_duplicate_msg_id（已有 embedding 的消息不重复算）
- [ ] **3.4 跑测试**
  - 预期 139 + 5 = **144 passed**

## Task 4：HDBSCAN 聚类核心

- [ ] **4.1 创建 `services/l4_clustering.py`**
  - `ClusteringService` dataclass
  - 字段：db / embeddings_repo / clusters_repo / normalized_repo /
    min_cluster_size=3 / min_samples=2 / window_hours=24
- [ ] **4.2 实现 `_cluster_window(window_end)` 内部方法**
  - 拉 24h 内所有有 embedding 的消息
  - 用 sklearn StandardScaler 归一化
  - HDBSCAN(min_cluster_size, min_samples, metric="cosine") 聚类
  - 返回 `dict[cluster_label, list[msg_id]]`
- [ ] **4.3 实现 `run_once()`**
  - align_to_quarter
  - 调 _cluster_window
  - 每个 cluster 选代表（与 centroid cosine 最高）
  - UPSERT event_clusters
  - 把 cluster 内非代表的 msg 标记 `is_duplicate=True`, `dup_of=代表 id`
  - 注意：不要标记**已经是 dup**的消息（避免覆盖 SimHash 判重结果）
- [ ] **4.4 单元测试 `tests/test_l4_clustering.py`**（10 个用例）
  - test_cluster_basic_correctness（构造已知向量，验证聚类结果）
  - test_outliers_not_clustered（HDBSCAN 噪声点 cluster_label=-1 应跳过）
  - test_representative_is_centroid_nearest
  - test_marks_non_representative_as_duplicate
  - test_does_not_overwrite_simhash_dup（已有 dup_of 的消息保持不动）
  - test_skips_when_window_unchanged
  - test_skips_when_too_few_embeddings
  - test_cohesion_calculation（簇内平均相似度公式）
  - test_handles_empty_window
  - test_upsert_overwrites_same_window
- [ ] **4.5 跑测试**
  - 预期 144 + 10 = **154 passed**

## Task 5：配置扩展

- [ ] **5.1 改 `config/_new.py` 加 8 字段**
  - `embedding_enabled: bool = True`
  - `embedding_model: str = "BAAI/bge-m3"`
  - `embedding_batch_size: int = 32`
  - `clustering_enabled: bool = True`
  - `clustering_window_hours: int = 24`
  - `clustering_min_cluster_size: int = 3`
  - `clustering_min_samples: int = 2`
  - `clustering_dim_reduction: bool = False`（Phase 3 用 UMAP 降维加速）
- [ ] **5.2 验证配置加载**
- [ ] **5.3 跑测试**
  - 预期仍 154 passed

## Task 6：main.py 注入

- [ ] **6.1 改 `main.py` Step 5f**
  - 启动时加载 BgeM3Embedder（一次性，缓存模型）
  - 构造 EmbedderService + ClusteringService
  - 加入 `new_services` 列表
  - 调度顺序：Embedder 先于 Clustering（embedder 喂数据给 clustering）
- [ ] **6.2 配置缺失即降级**：embedding_enabled=False → 跳过整段
- [ ] **6.3 验证 main.py 仍能 import**
- [ ] **6.4 跑测试**
  - 预期仍 154 passed

## Task 7：本地端到端验收

- [ ] **7.1 启动服务首次冷启动**
  - 首次会下载 bge-m3（~600MB），耗时几分钟
  - 然后开始向量化历史 4943 条消息（CPU ~5 分钟）
  - 启动日志含 "EmbedderService 启动"、"ClusteringService 启动"
- [ ] **7.2 等下一个 quarter，SQL 验证**
  - `SELECT COUNT(*) FROM message_embeddings;` 应 ≥ 4943
  - `SELECT COUNT(*), SUM(cluster_size) FROM event_clusters;`
  - 看几个簇的代表消息：
    `SELECT cluster_id, cluster_size, cohesion, representative_msg_id FROM event_clusters ORDER BY cluster_size DESC LIMIT 10;`
- [ ] **7.3 对比聚类前后的 hotness 榜变化**
  - 同一时刻聚类前 / 聚类后的榜单差异
  - 期望：headline 新闻类实体的 count_short 下降（同事件被合并），
    长尾"独特讨论"类实体相对靠前

## Task 8：检测共现 + Embedding 协同效果

- [ ] **8.1 如果 Task 2.5（L3 共现）已完成**
  - 看 entity_cooccurrence 在聚类后变化（应更稳定，因为 noise 被合并）
- [ ] **8.2 反向验证**
  - 关掉 clustering（`clustering_enabled=False`），对比 hotness 榜
  - 决定是否长期开启

## Task 9：文档

- [ ] **9.1 改 `docs/operations_guide.md` §6.4 Embedding 聚类调参**
  - bge-m3 模型缓存位置
  - HDBSCAN 参数解释
  - CPU vs GPU（如果未来上 GPU 怎么改）
- [ ] **9.2 加 `docs/faq_design_decisions.md` Q10**
  - "为什么必须用 pgvector，不能用文件存？"
  - "为什么 bge-m3 不算 LLM？"
  - "聚类后下游服务为什么不用改？"

## 执行顺序与依赖图

```
Task 0 (可行性 + 用户决策)
   └─► Task 1 (装依赖 + schema)
           └─► Task 2 (BgeM3Embedder → 139)
                   └─► Task 3 (EmbedderService → 144)
                           └─► Task 4 (ClusteringService → 154)
                                   └─► Task 5 (配置)
                                           └─► Task 6 (main.py 注入)
                                                   └─► Task 7 (端到端)
                                                           └─► Task 8 (协同验证)
                                                                   └─► Task 9 (文档)
```

## 完工后状态

```
新增文件：
  alembic/versions/003_phase2_embeddings.py
  llm/embedder_client.py
  db/repositories/message_embeddings_repo.py
  db/repositories/event_clusters_repo.py
  services/l4_embedder.py
  services/l4_clustering.py
  tests/test_embedder_client.py
  tests/test_l4_embedder.py
  tests/test_l4_clustering.py

修改文件：
  db/models.py                       +MessageEmbedding +EventCluster
  config/_new.py                     +8 字段
  main.py                            +Embedder + Clustering 构造
  requirements.txt                   ★ +sentence-transformers +hdbscan +pgvector +torch
  docs/operations_guide.md           +§6.4
  docs/faq_design_decisions.md       +Q10

测试基线：135 → 154 passed（+19，0 回归）
新增能力：语义级去重 + 事件聚类
```

## 风险与未决议题（实施前在 design.md 解决）

| 风险 | 优先级 | 解决方向 |
|---|---|---|
| **新依赖 1.5GB** 让仓库变重 | 高 | 用户决策；或方案 B 走 Ollama embedding API |
| pgvector 不可用 | 高 | DBA 协助 / 自托管 / 改用 numpy 文件 |
| bge-m3 下载需要 huggingface 访问 | 高 | 国内服务器需 VPN；或预先下载好 cache |
| CPU 模式向量化太慢（每条 ~50ms） | 中 | 批量大小 32 + 后台跑；或上 GPU |
| HDBSCAN 在大数据量下慢 | 中 | 加 UMAP 降维（Phase 3）；或限制窗口 |
| 聚类把语义相似但**不同事件**的消息合并 | 中 | min_cluster_size=3 + cohesion 阈值；观察后调 |
| message_embeddings 表磁盘占用 | 低 | 1024 维 × 4B × 1M 行 = 4GB；保留 30 天即可 |
| ClusteringService 与 HotnessService 共享窗口对齐 | 低 | 都走 align_to_quarter |
| **零 LLM 硬约束被突破** | 中 | 用户决策接受；或方案 C 跳过本任务 |

---

*文档版本：v1.0*
*预估工时：实施前补 design 1~2 天 + 编码 5~7 天 ≈ 1~2 周*
*对早期热点发现的契合度：⭐⭐⭐ 间接价值——降噪让其它信号更清晰，但不直接产生新信号*
