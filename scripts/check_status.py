"""
一键自检：看 Phase 1 新链路当前的产出状态。

用法：
    .venv/bin/python scripts/check_status.py

输出包括：
- 5 张表的最近更新时间（判断系统是否在干活）
- 最近 1 小时实体提及 Top 20
- 最新一份排行榜（Top 20）
"""
from __future__ import annotations

import sys
from pathlib import Path

# 让脚本能 import 项目模块
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from config.settings import get_settings  # noqa: E402
from db.connection import Database  # noqa: E402


def _print_section(title: str) -> None:
    print()
    print("=" * 70)
    print(f" {title}")
    print("=" * 70)


def main() -> None:
    db = Database(get_settings())

    # -------- 1. 5 张表的"最后更新时间" --------
    _print_section("1. 表更新时间（看系统是否在干活）")

    with db.get_session() as s:
        rows = s.execute(
            text(
                """
                SELECT 'normalized_messages' AS tbl, max(created_at)::text AS last_update,
                       count(*)::text AS rows FROM normalized_messages
                UNION ALL
                SELECT 'entity_mentions',     max(ts)::text,         count(*)::text FROM entity_mentions
                UNION ALL
                SELECT 'hotness_snapshots',   max(window_end)::text, count(*)::text FROM hotness_snapshots
                """
            )
        ).all()
        # 按 tbl 名固定顺序
        order = {
            "normalized_messages": 1,
            "entity_mentions": 2,
            "hotness_snapshots": 3,
        }
        rows_sorted = sorted(rows, key=lambda r: order.get(r[0], 99))
        print(f"{'表名':30s} {'总行数':>10s}  {'最近更新':25s}")
        print("-" * 70)
        for tbl, last_update, total in rows_sorted:
            last_update = last_update or "(空表)"
            print(f"{tbl:30s} {total:>10s}  {last_update}")

    # -------- 2. 三源消息分布（上游抓取健康度） --------
    _print_section("2. 三源消息分布（上游抓取健康度）")

    with db.get_session() as s:
        rows = s.execute(
            text(
                """
                SELECT raw_source,
                       count(*)                                                    AS total_24h,
                       count(*) FILTER (WHERE ts > now() - INTERVAL '1 hour')      AS last_1h,
                       count(*) FILTER (WHERE ts > now() - INTERVAL '10 minutes')  AS last_10min,
                       max(ts)                                                     AS last_seen
                FROM normalized_messages
                WHERE ts >= now() - INTERVAL '24 hours'
                GROUP BY raw_source
                ORDER BY total_24h DESC
                """
            )
        ).all()

        if not rows:
            print("(最近 24 小时无任何归一化消息——多半是上游抓取服务全停了)")
        else:
            # 计算 24h 总量做百分比
            total_all = sum(r[1] for r in rows)
            print(f"{'源':18s} {'24h':>8s} {'1h':>6s} {'10min':>7s} {'占比':>7s}  {'最近':25s}")
            print("-" * 80)
            for src, total, h1, m10, last_seen in rows:
                pct = 100.0 * total / total_all if total_all else 0
                print(
                    f"{src:18s} {total:>8d} {h1:>6d} {m10:>7d} {pct:>6.1f}%  {str(last_seen):25s}"
                )
            # 检测掉队的源：哪个源 24h 占比 < 5% → 提醒
            print()
            laggards = [r for r in rows if r[1] / total_all < 0.05]
            if laggards:
                print("⚠️ 占比 < 5% 的源（很可能上游抓取服务慢/停了）：")
                for src, total, _, _, _ in laggards:
                    print(f"   - {src}: 仅 {total} 条 / 24h")
            elif len(rows) < 3:
                missing = {"twitter", "binance_square", "discord"} - {r[0] for r in rows}
                if missing:
                    print(f"⚠️ 完全无数据的源（24h 内 0 条）：{', '.join(sorted(missing))}")

    # -------- 3. 最近 1 小时实体提及 Top 20 --------
    _print_section("3. 最近 1 小时被提到最多的 Top 20 实体（entity_mentions）")

    with db.get_session() as s:
        rows = s.execute(
            text(
                """
                SELECT entity, entity_type, count(*) AS cnt
                FROM entity_mentions
                WHERE ts >= now() - INTERVAL '1 hour'
                GROUP BY entity, entity_type
                ORDER BY cnt DESC
                LIMIT 20
                """
            )
        ).all()
        if not rows:
            print("(最近 1 小时无任何实体被提及——可能数据源没新数据，或冷启动还没攒够数据)")
        else:
            print(f"{'#':>3s}  {'实体':30s} {'类型':12s} {'次数':>6s}")
            print("-" * 70)
            for i, (entity, etype, cnt) in enumerate(rows, 1):
                print(f"{i:>3d}  {entity:30s} {etype or '':12s} {cnt:>6d}")

    # -------- 4. 最近 24 小时实体提及 Top 20（看更长时段累计热度） --------
    _print_section("4. 最近 24 小时被提到最多的 Top 20 实体（entity_mentions）")

    with db.get_session() as s:
        rows = s.execute(
            text(
                """
                SELECT entity, entity_type, count(*) AS cnt
                FROM entity_mentions
                WHERE ts >= now() - INTERVAL '24 hours'
                GROUP BY entity, entity_type
                ORDER BY cnt DESC
                LIMIT 20
                """
            )
        ).all()
        if not rows:
            print("(最近 24 小时无任何实体被提及)")
        else:
            print(f"{'#':>3s}  {'实体':30s} {'类型':12s} {'次数':>6s}")
            print("-" * 70)
            for i, (entity, etype, cnt) in enumerate(rows, 1):
                print(f"{i:>3d}  {entity:30s} {etype or '':12s} {cnt:>6d}")

    # -------- 5. 最新一份排行榜 --------
    _print_section("5. 最新一份排行榜（hotness_snapshots）")

    with db.get_session() as s:
        # 先看最近一次窗口的时间
        latest = s.execute(
            text(
                """
                SELECT max(window_end)
                FROM hotness_snapshots
                WHERE window_type = '1h'
                """
            )
        ).scalar()

        if latest is None:
            print(
                "(还没出过排行榜。可能原因：\n"
                "  - 冷启动期 entity_mentions 没攒够 100 条 → 看 logs/service.log\n"
                "    里的 'baseline data insufficient' 日志，等数据攒够\n"
                "  - 或者还没到第一个 :00/:15/:30/:45 整点)"
            )
        else:
            # 计算距现在多久前，给个直观的"几分钟前"说明
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc)
            # latest 是带 tz 的 datetime（PG TIMESTAMPTZ）
            delta = now - latest.astimezone(timezone.utc)
            mins_ago = int(delta.total_seconds() // 60)

            print(f"窗口时刻: {latest}")
            print(
                f"  含义：截至 {latest.strftime('%H:%M')} 的"
                f"过去 1 小时（{(latest.replace(microsecond=0)).strftime('%H:%M')} 之前 1 小时）热点"
            )
            print(f"  距现在: {mins_ago} 分钟前")
            if mins_ago > 16:
                print(
                    "  ⚠️ 距今超过 15 分钟，说明已经错过了下一个整点产出，"
                    "可能 worker 卡住或数据稀薄被跳过"
                )
            print()
            rows = s.execute(
                text(
                    """
                    SELECT
                      rank,
                      entity,
                      entity_type,
                      count_short,
                      round(cast(count_baseline AS numeric), 2) AS baseline_per_h,
                      round(cast(growth_rate AS numeric), 2)   AS growth,
                      cross_source,
                      round(cast(final_score AS numeric), 2)   AS score,
                      is_new_entity
                    FROM hotness_snapshots
                    WHERE window_end = :latest
                      AND window_type = '1h'
                    ORDER BY rank ASC
                    """
                ),
                {"latest": latest},
            ).all()
            print(
                f"{'#':>3s}  {'实体':22s} {'类型':10s} "
                f"{'1h次数':>7s} {'基线':>7s} {'增长':>7s} {'跨源':>4s} {'总分':>7s} {'新':>3s}"
            )
            print("-" * 80)
            for r in rows:
                is_new_mark = "★" if r[8] else ""
                print(
                    f"{r[0]:>3d}  {r[1]:22s} {r[2] or '':10s} "
                    f"{r[3]:>7d} {float(r[4]):>7.2f} {float(r[5]):>7.2f} "
                    f"{r[6]:>4d} {float(r[7]):>7.2f} {is_new_mark:>3s}"
                )

    print()
    print("=" * 70)
    print(" 提示")
    print("=" * 70)
    print(
        "- 想看详细操作 → docs/operations_guide.md\n"
        "- 想看叙事词典格式 → dictionaries/narratives.yaml 里的注释\n"
        "- 想看实时日志 → tail -f logs/service.log"
    )
    print()


if __name__ == "__main__":
    main()
