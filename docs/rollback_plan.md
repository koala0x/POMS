# Phase 1 回滚预案

> 当 Gate 1 期间出现**老链路产出下降 > 10%** 或**新链路严重异常**时，按本
> 预案回滚，保证老服务（Level1Service / Level2Service）不被 Phase 1 连累。
>
> 基于 `.kiro/specs/crypto-narrative-radar/requirements.md` Success Metrics
> §6 的风险兜底条款。

---

## 1. 触发条件

满足**任一**即触发回滚讨论，人工确认后执行：

1. **老链路产出下降 > 10%**（Success Metrics §6 被违反）
   - `summary_level1` / `summary_level2` 近 24h 条数对比部署前 baseline 下降
     超过 10%，且连续观察 2 个采样周期（通常 8 小时）仍未恢复
2. **新链路严重错误持续产生**
   - `logs/service.log` 中 `ERROR` 级别日志在 10 分钟内 ≥ 50 条
   - 进程 OOM（RSS 超过初始 3 倍）
   - PG 连接数持续 > 50（连接池彻底失控）
3. **数据污染疑虑**
   - 新链路往老表（`twitter_posts.is_summarized` 等）写入了意料外的数据
     （这是 Req 1.8 的红线，属于重大代码缺陷）

---

## 2. 执行步骤（推荐：软回滚，不清数据）

### Step 1：停服

```bash
# 找到主进程 PID
pgrep -fl "python.*main.py"

# 优雅退出（SIGINT 触发 main.py 里的 KeyboardInterrupt 分支 + jobs.shutdown）
kill -INT <PID>

# 验证已退出
sleep 15 && pgrep -fl "python.*main.py" || echo "已停止"
```

> 不要用 `kill -9`：那会跳过 `jobs.shutdown(wait=False)`，可能留下半提交事务。

### Step 2：从 main.py 移除 new_services

编辑 `main.py`，把：

```python
jobs = Jobs(
    level1_services=level1_services,
    level2_services=level2_services,
    poll_interval_seconds=settings.poll_interval_seconds,
    new_services=new_services,
)
```

改回三参数旧式构造：

```python
jobs = Jobs(
    level1_services=level1_services,
    level2_services=level2_services,
    poll_interval_seconds=settings.poll_interval_seconds,
)
```

同时把 main.py 里"Phase 1 新链路初始化"整段代码（7 步）注释掉，留着下次
恢复用：

```python
# ======================================================================
# Phase 1 新链路（crypto-narrative-radar）—— 回滚期暂停，留原代码以便恢复
# ======================================================================
# try:
#     dicts = get_dictionaries()
#     ...（整段保留到 new_services = [...] 一行都注释）...
```

> Jobs 的 `new_services` 参数带默认值 `()`（Task 8.1 的兼容性设计），所以
> 三参数构造仍能正常跑，worker 只迭代 level1 / level2 两组，完全等同于
> Phase 1 部署前的行为。

### Step 3：保留 Phase 1 表不清理

`normalized_messages` / `entity_mentions` / `hotness_snapshots` 三张新表
**不清空**。理由：

- 这些表和老链路完全隔离（Req 5.10 无外键），留着不影响任何东西
- 下次恢复新链路时，SlidingCounter backfill 可直接复用已累积的 7 天数据，
  避免再次冷启动
- 数据本身有观测价值：可以离线分析"为什么 Gate 1 没过"

若未来确认彻底放弃 Phase 1，用 `alembic downgrade -1` 清除（见 Step 6）。

### Step 4：重启服务

```bash
cd /path/to/PomsAI
.venv/bin/python main.py 2>&1 | tee -a logs/service.log
```

验证启动日志**不再**包含下面这些行：

```
词典就绪：...
SlidingCounter backfill 结束：...
summary worker 启动:level1=3,level2=3,new=3,...
```

应改成：

```
summary worker 启动:level1 services=3,level2 services=3,空闲 sleep 30s
```

（注意 `new=` 这一项消失，且分隔符回到老格式——这是 Task 8.1 扩展前后的
日志差异，可作为回滚成功的视觉确认）

### Step 5：监控老链路恢复情况

每 2 小时检查一次 24h 产出：

```sql
SELECT source, count(*) AS cnt_24h
FROM summary_level1
WHERE created_at >= now() - INTERVAL '24 hours'
GROUP BY source;
```

回滚后 24h 内，`cnt_24h` 应回到部署前 baseline ±5% 区间。若未恢复：
排查是否有其他系统资源被抢占（`top` / `iostat` / PG 侧慢查询），
和 Phase 1 无关。

### Step 6（可选，彻底清除新链路）

确认不再需要新链路数据后：

```bash
# 注意：这会删除 normalized_messages / entity_mentions / hotness_snapshots
# 三张表的全部数据和索引。只在确实要放弃 Phase 1 时执行。
alembic downgrade -1
```

执行前务必：

1. `pg_dump --data-only --table=normalized_messages --table=entity_mentions \
   --table=hotness_snapshots all_new > phase1_data_backup_$(date +%Y%m%d).sql`
2. 确认备份文件大小合理、可读
3. 再执行 downgrade

---

## 3. 回滚后的复盘清单

无论回滚原因是什么，72h 冷静期内完成以下事项才能考虑再次启用 Phase 1：

- [ ] **写 incident 报告**：记录触发条件、时间线、影响范围、人工介入动作
- [ ] **根因定位**：
  - 代码问题 → 在 `tests/` 补对应回归用例
  - 配置问题 → 在 `docs/gate1_checklist.md` 的"前置检查"里加防护项
  - 数据分布问题（词典覆盖、SimHash 阈值等）→ 在 `.kiro/specs/.../design.md`
    对应章节更新经验教训
- [ ] **老链路验证**：回滚后连续 48h 观测，老链路产出完全稳定
- [ ] **修复验证**：针对根因的修复跑过一次完整的 `pytest tests/ --ignore=tests/test_ollama_client.py -q`，且在 staging 环境（如有）跑过 24h 烟测

---

## 4. 快速命令速查

```bash
# 立刻回滚（执行 Step 1 + 2 + 4 的一键版）
# 注意：Step 2 的 main.py 修改仍需手工进行

kill -INT $(pgrep -f "python.*main.py")
sleep 15
vim main.py  # 手工改：new_services 那一行和上面 7 步初始化
nohup .venv/bin/python main.py > logs/service.log 2>&1 &

# 确认回滚成功
tail -n 20 logs/service.log | grep "worker 启动"
# 期望：看到 "level1 services=3,level2 services=3,空闲 sleep 30s"
# 不期望：看到 "new=3"
```

---

## 5. 为什么要做回滚预案（设计哲学）

Phase 1 的核心设计承诺是"**老链路零退化**"——即使新链路完全崩坏，也必须能
在 30 分钟内恢复到部署前的服务形态。这条承诺靠三个机制实现：

1. **代码层**：Jobs.`new_services` 参数带默认值 `()`，拿掉就等于 Phase 1
   未部署（Task 8.1 的兼容性设计）
2. **数据层**：新三张表和老 5 张表完全无外键关系（Req 5.10），回滚时既不
   需要迁移也不需要清理
3. **调度层**：同一个 worker 线程串行跑所有 service（Req 8.3），拿掉新
   services 不会留下孤儿线程

所以本预案的执行成本≈**改 1 处 main.py 代码 + 重启进程**，总耗时 < 15 分钟。
这种"回滚便宜"正是让我们敢于在生产上直接跑 Phase 1 而不是先去搭 staging
的底气——因为搞砸的代价是可控的。
