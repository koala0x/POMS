"""
一次性落库健康检查脚本。

用法: python scripts/check_db_landed.py

对四张核心表逐一体检,输出:
- 总行数
- 最近 1h / 6h / 24h 新增
- 未摘要/未二次摘要堆积
- 最新一条记录的时间戳
- 原生 ID(tweet_id / post_id)的覆盖率,辅助判断去重链路
- summary_level1 / summary_level2 的 raw_count / level1_count 与 array_length 是否一致
"""
from __future__ import annotations

import sys
from pathlib import Path

# 让脚本无论从哪个 cwd 起都能 import 到工程包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from config.settings import get_settings
from db.connection import Database


def _fmt_int(n) -> str:
    return f"{int(n or 0):,}"


def _print_section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def _check_raw_table(session, table: str, native_id_col: str | None) -> None:
    _print_section(f"[{table}]")
    row = session.execute(
        text(
            f"""
            SELECT
              COUNT(*)                                                      AS total,
              COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '1 hour')  AS last_1h,
              COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '6 hour')  AS last_6h,
              COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '24 hour') AS last_24h,
              COUNT(*) FILTER (WHERE is_summarized = FALSE)                   AS unsummarized,
              MAX(created_at)                                                 AS max_created_at,
              MAX(posted_at)                                                  AS max_posted_at
            FROM {table}
            """
        )
    ).one()
    print(f"总行数:       {_fmt_int(row.total)}")
    print(f"近 1h 新增:   {_fmt_int(row.last_1h)}")
    print(f"近 6h 新增:   {_fmt_int(row.last_6h)}")
    print(f"近 24h 新增:  {_fmt_int(row.last_24h)}")
    print(f"未摘要堆积:   {_fmt_int(row.unsummarized)}")
    print(f"最近入库时间: {row.max_created_at}")
    print(f"最新发帖时间: {row.max_posted_at}")

    if native_id_col:
        cov = session.execute(
            text(
                f"""
                SELECT
                  COUNT(*)                                                AS total,
                  COUNT({native_id_col})                                  AS with_id,
                  COUNT({native_id_col}) - COUNT(DISTINCT {native_id_col}) AS dup_native
                FROM {table}
                """
            )
        ).one()
        miss = int(cov.total or 0) - int(cov.with_id or 0)
        print(
            f"原生 {native_id_col} 覆盖: {_fmt_int(cov.with_id)}/"
            f"{_fmt_int(cov.total)} (缺 {_fmt_int(miss)})"
        )
        # 仅统计非 NULL 行内是否有同 ID 重复;UNIQUE 约束下必须 = 0
        print(f"同 {native_id_col} 重复行数: {_fmt_int(cov.dup_native)}  (UNIQUE 约束下应为 0)")

    # 最近 5 条样例
    print("最近 5 条:")
    sample = session.execute(
        text(
            f"SELECT id, created_at, LEFT(content, 60) AS snippet "
            f"FROM {table} ORDER BY id DESC LIMIT 5"
        )
    ).all()
    for r in sample:
        print(f"  id={r.id}  created_at={r.created_at}  content={r.snippet!r}")


def _check_summary_level1(session) -> None:
    _print_section("[summary_level1]")
    row = session.execute(
        text(
            """
            SELECT
              COUNT(*)                                                      AS total,
              COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '1 hour')  AS last_1h,
              COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '24 hour') AS last_24h,
              COUNT(*) FILTER (WHERE is_summarized_l2 = FALSE)                AS pending_l2,
              MAX(created_at)                                                 AS max_created_at
            FROM summary_level1
            """
        )
    ).one()
    print(f"总行数:       {_fmt_int(row.total)}")
    print(f"近 1h 新增:   {_fmt_int(row.last_1h)}")
    print(f"近 24h 新增:  {_fmt_int(row.last_24h)}")
    print(f"待二次摘要:   {_fmt_int(row.pending_l2)}")
    print(f"最近生成时间: {row.max_created_at}")

    per_source = session.execute(
        text(
            """
            SELECT source,
                   COUNT(*)                                     AS total,
                   SUM(raw_count)                               AS total_raw,
                   AVG(raw_count)::NUMERIC(10,2)                AS avg_raw,
                   COUNT(*) FILTER (WHERE is_summarized_l2=FALSE) AS pending_l2,
                   MAX(created_at)                              AS last_at
            FROM summary_level1
            GROUP BY source
            ORDER BY source
            """
        )
    ).all()
    print("按 source 统计:")
    for r in per_source:
        print(
            f"  {r.source:<16} total={_fmt_int(r.total)} raw_sum={_fmt_int(r.total_raw)} "
            f"avg_raw={r.avg_raw} pending_l2={_fmt_int(r.pending_l2)} last={r.last_at}"
        )

    # 一致性: raw_count 应等于 array_length(raw_ids)
    bad = session.execute(
        text(
            """
            SELECT COUNT(*) FROM summary_level1
            WHERE raw_count <> COALESCE(array_length(raw_ids, 1), 0)
            """
        )
    ).scalar_one()
    print(f"raw_count 与 raw_ids 长度不一致行数: {_fmt_int(bad)}  (应为 0)")


def _check_summary_level2(session) -> None:
    _print_section("[summary_level2]")
    row = session.execute(
        text(
            """
            SELECT
              COUNT(*)                                                      AS total,
              COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '24 hour') AS last_24h,
              MAX(created_at)                                                 AS max_created_at
            FROM summary_level2
            """
        )
    ).one()
    print(f"总行数:       {_fmt_int(row.total)}")
    print(f"近 24h 新增:  {_fmt_int(row.last_24h)}")
    print(f"最近生成时间: {row.max_created_at}")

    per_source = session.execute(
        text(
            """
            SELECT source,
                   COUNT(*)                           AS total,
                   SUM(level1_count)                  AS total_l1,
                   AVG(level1_count)::NUMERIC(10,2)   AS avg_l1,
                   MAX(created_at)                    AS last_at
            FROM summary_level2
            GROUP BY source
            ORDER BY source
            """
        )
    ).all()
    print("按 source 统计:")
    for r in per_source:
        print(
            f"  {r.source:<16} total={_fmt_int(r.total)} l1_sum={_fmt_int(r.total_l1)} "
            f"avg_l1={r.avg_l1} last={r.last_at}"
        )

    bad = session.execute(
        text(
            """
            SELECT COUNT(*) FROM summary_level2
            WHERE level1_count <> COALESCE(array_length(level1_ids, 1), 0)
            """
        )
    ).scalar_one()
    print(f"level1_count 与 level1_ids 长度不一致行数: {_fmt_int(bad)}  (应为 0)")

    # period_start/end 合法性
    bad_period = session.execute(
        text("SELECT COUNT(*) FROM summary_level2 WHERE period_start >= period_end")
    ).scalar_one()
    print(f"period_start >= period_end 的行数: {_fmt_int(bad_period)}  (应为 0)")


def main() -> None:
    settings = get_settings()
    print(f"数据库: {settings.db_user}@{settings.db_host}:{settings.db_port}/{settings.db_name}")
    db = Database(settings)
    with db.get_session() as session:
        _check_raw_table(session, "twitter_posts", "tweet_id")
        _check_raw_table(session, "binance_square_posts", "post_id")
        _check_raw_table(session, "discord_messages", "post_id")
        _check_summary_level1(session)
        _check_summary_level2(session)
    print()
    print("检查完成。")


if __name__ == "__main__":
    main()
