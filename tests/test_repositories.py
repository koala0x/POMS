from __future__ import annotations

"""
Repository 集成测试，基于真实 PostgreSQL。

策略：
- 通过 .env 读取 DB 配置；若环境不可用则 pytest.skip 而不报错
- 每个用例使用独立 Session，在 finally 中清理本测试插入的行（按 id 范围）
- 不污染表结构，直接复用上游维护好的业务表（本服务不负责建表）

历史变更（2026-05）：
  - 老链路（Level1Service / Level2Service）已淘汰
  - 删除：Level1Repo / Level2Repo / TwitterRepo / BinanceRepo / DiscordRepo
  - 删除：test_twitter_repo_count_fetch_mark / test_binance_repo_basic /
          test_level1_repo_insert_fetch_mark / test_level2_repo_insert
  - 保留：本文件作为新链路 repo 的集成测试占位（当前为空）；
          test_seed_50_twitter_posts 也一并删除（依赖已不存在的链路）
"""

# 当前没有需要 PG 的集成测试。新链路 repo（normalized_messages_repo /
# entity_mentions_repo / hotness_snapshots_repo / cooccurrence_repo /
# briefings_repo）的功能由各自的 service 测试通过 SQLite in-memory 覆盖
# （见 test_l0_normalizer.py / test_l2_hotness.py / test_l3_cooccurrence.py /
# test_l5_briefing.py 等），不需要再走真 PG。
#
# 如果未来需要写新链路 repo 的真 PG 集成测试，按这个文件历史版本的
# 模板：fixture pytest.skip 兜底 + 每用例 finally 清理插入数据。
