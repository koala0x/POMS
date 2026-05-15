"""
调参诊断工具：5 秒钟看清当前配置在你的数据上意味着什么。

用法：
    .venv/bin/python scripts/tune_helper.py
    .venv/bin/python scripts/tune_helper.py --days 7      # 默认 7 天
    .venv/bin/python scripts/tune_helper.py --days 14
    .venv/bin/python scripts/tune_helper.py --quiet       # 只输出推荐值，不输出过程

输出包括：
1. 4 个窗口的 growth_rate 分布（p50 / p75 / p90 / p95 / p99 / max）
2. 当前阈值在过去 N 天会触发多少次（含 cooldown 估算）
3. 按"每天 X 条告警"的目标反推阈值建议
4. 过去 24h 真正爆发过的实体清单（人工对照"告警是不是漏了真热点"）

设计意图：
把"凭体感调参"换成"看分布定阈值 + backtest 验证"的闭环流程。
配合 docs/tuning_guide.md 一起用。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 让脚本能 import 项目模块
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from config.settings import get_settings  # noqa: E402
from db.connection import Database  # noqa: E402


# ---------------------------------------------------------------------------
# 输出工具
# ---------------------------------------------------------------------------


def _print_header(title: str, char: str = "=") -> None:
    print()
    print(char * 72)
    print(f" {title}")
    print(char * 72)


def _print_section(title: str) -> None:
    print()
    print(f"--- {title} ---")


# 4 个窗口顺序，与 hotness_snapshots.window_type 一致
_WINDOWS = ("1h", "3h", "6h", "24h")

# 不同窗口数据频率不同：1h/3h/6h 是 96 份/天（每 15min 一份），24h 也是 96 份/天
# 实际告警还会被 cooldown 压缩；这里给一个经验比例
# 1h 窗口 cooldown 压缩率 ~33%（同 entity 60min 内多次上榜只发 1 次）
# 24h 窗口压缩率 ~80%（同 entity 几乎全天都在榜，60min cooldown 把它压得很狠）
_COOLDOWN_FACTOR = {
    "1h": 0.33,
    "3h": 0.50,
    "6h": 0.65,
    "24h": 0.80,
}


# ---------------------------------------------------------------------------
# 1. 分布统计
# ---------------------------------------------------------------------------


def fetch_distribution(db: Database, days: int) -> dict[str, dict]:
    """
    返回 {window_type: {p50, p75, p90, p95, p99, max, total}}
    """
    with db.get_session() as s:
        rows = s.execute(
            text(
                """
                SELECT
                  window_type,
                  COUNT(*) AS total,
                  percentile_cont(0.50) WITHIN GROUP (ORDER BY growth_rate) AS p50,
                  percentile_cont(0.75) WITHIN GROUP (ORDER BY growth_rate) AS p75,
                  percentile_cont(0.90) WITHIN GROUP (ORDER BY growth_rate) AS p90,
                  percentile_cont(0.95) WITHIN GROUP (ORDER BY growth_rate) AS p95,
                  percentile_cont(0.97) WITHIN GROUP (ORDER BY growth_rate) AS p97,
                  percentile_cont(0.99) WITHIN GROUP (ORDER BY growth_rate) AS p99,
                  MAX(growth_rate) AS max_val
                FROM hotness_snapshots
                WHERE window_end >= NOW() - make_interval(days => :days)
                GROUP BY window_type
                """
            ),
            {"days": days},
        ).all()

    result: dict[str, dict] = {}
    for r in rows:
        result[r.window_type] = {
            "total": r.total or 0,
            "p50": float(r.p50 or 0),
            "p75": float(r.p75 or 0),
            "p90": float(r.p90 or 0),
            "p95": float(r.p95 or 0),
            "p97": float(r.p97 or 0),
            "p99": float(r.p99 or 0),
            "max": float(r.max_val or 0),
        }
    return result


def print_distribution(dist: dict[str, dict], days: int) -> None:
    _print_header(f"📊 growth_rate 分布（过去 {days} 天）")
    print(
        f"  {'window':<6} {'total':>8} {'p50':>8} {'p75':>8} "
        f"{'p90':>8} {'p95':>8} {'p99':>8} {'max':>10}"
    )
    print("  " + "-" * 70)
    for w in _WINDOWS:
        d = dist.get(w)
        if d is None:
            print(f"  {w:<6} {'(无数据)':>8}")
            continue
        print(
            f"  {w:<6} {d['total']:>8} "
            f"{d['p50']:>8.2f} {d['p75']:>8.2f} {d['p90']:>8.2f} "
            f"{d['p95']:>8.2f} {d['p99']:>8.2f} {d['max']:>10.2f}"
        )

    print(
        "\n  解读：p99 = 1% 概率超过的值（明显异常）；"
        "p95 = 5% 概率（轻度异常）；p50 = 中位数（日常水平）"
    )


# ---------------------------------------------------------------------------
# 2. backtest：当前阈值在过去 N 天会触发多少次
# ---------------------------------------------------------------------------


def fetch_threshold_hits(
    db: Database,
    days: int,
    thresholds: list[float] = [2, 3, 5, 7, 10, 15, 20, 30],
) -> dict[str, dict[float, int]]:
    """
    返回 {window_type: {threshold: hits}}
    hits 是"窗口快照中 growth >= threshold 的记录条数"，
    实际告警数会被 cooldown 进一步压缩，详见 _COOLDOWN_FACTOR。
    """
    cases = ", ".join(
        f"COUNT(*) FILTER (WHERE growth_rate >= {t}) AS at_{int(t * 10)}"
        for t in thresholds
    )

    sql = f"""
        SELECT window_type, {cases}
        FROM hotness_snapshots
        WHERE window_end >= NOW() - make_interval(days => :days)
        GROUP BY window_type
    """

    with db.get_session() as s:
        rows = s.execute(text(sql), {"days": days}).all()

    result: dict[str, dict[float, int]] = {}
    for r in rows:
        m = r._mapping
        result[r.window_type] = {
            t: int(m[f"at_{int(t * 10)}"] or 0) for t in thresholds
        }
    return result


def print_threshold_hits(
    hits: dict[str, dict[float, int]],
    settings,
    days: int,
) -> None:
    _print_header(f"🎯 阈值 backtest（过去 {days} 天会触发多少条告警）")

    current_thresholds = {
        "1h": settings.alert_growth_threshold,
        "3h": settings.alert_3h_growth_threshold,
        "6h": settings.alert_6h_growth_threshold,
        "24h": settings.alert_24h_growth_threshold,
    }

    print(
        f"  {'window':<6} {'当前阈值':<10} {'快照命中':<10} "
        f"{'cooldown 后预估':<15} {'≈ 次/天':<10}"
    )
    print("  " + "-" * 60)

    for w in _WINDOWS:
        cur_th = current_thresholds[w]
        win_hits = hits.get(w, {})
        # 找 ≤ cur_th 的最大阈值（backtest 表里有的几个值之一）作为近似
        snapshot_hits = 0
        for t in sorted(win_hits.keys(), reverse=True):
            if t <= cur_th:
                snapshot_hits = win_hits[t]
                break
        # cooldown 压缩
        post_cooldown = int(snapshot_hits * (1 - _COOLDOWN_FACTOR[w]))
        per_day = post_cooldown / days if days > 0 else 0
        verdict = _frequency_verdict(per_day)
        print(
            f"  {w:<6} {cur_th:<10.1f} {snapshot_hits:<10} "
            f"{post_cooldown:<15} {per_day:>6.1f} {verdict}"
        )

    print(
        "\n  说明：'快照命中' 是榜单中 growth ≥ 阈值的快照数；"
        "实际告警数受 cooldown 压缩"
    )
    print("        cooldown 后预估 = 快照命中 × (1 - cooldown 因子)，仅供参考")


def _frequency_verdict(per_day: float) -> str:
    """按每天告警数给出体感标签"""
    if per_day < 0.5:
        return "❄️  太冷（一天不到 1 条）"
    if per_day < 3:
        return "🌤  适中"
    if per_day < 10:
        return "🔥 较热"
    if per_day < 30:
        return "🔥🔥 偏吵"
    return "🚨 刷屏"


# ---------------------------------------------------------------------------
# 3. 按目标反推阈值建议
# ---------------------------------------------------------------------------


def print_recommendations(dist: dict[str, dict], days: int) -> None:
    _print_header("💡 阈值建议（按目标频率反推）")
    print()
    print(f"  {'window':<6} {'~1 条/天':<12} {'~5 条/天':<12} {'~10 条/天':<12}")
    print("  " + "-" * 50)

    # 推荐：用 percentile 反推
    # 1h/3h/6h 都是 96 份/天 × 7 天 = 672 份；想每天 1 条 → 7/672 ≈ 99 分位
    # 想每天 5 条 → 35/672 ≈ 95 分位
    # 想每天 10 条 → 70/672 ≈ 90 分位
    # 注意还要叠加 cooldown 压缩，所以实际阈值要"再低一档"才能达到目标
    # 这里直接用 p99/p95/p90 的近似值给推荐
    for w in _WINDOWS:
        d = dist.get(w)
        if d is None:
            continue
        # cooldown 压缩意味着实际告警 = 快照命中 × (1 - cooldown_factor)
        # 反推：如果想每天 X 条告警，需要每天有 X / (1 - cooldown_factor) 条快照命中
        # 1h 窗口压缩 33% → 想 1 条告警需 ~1.5 条快照
        # 24h 窗口压缩 80% → 想 1 条告警需 ~5 条快照
        # 简化：直接用 p99 / p95 / p90 给建议，不再叠 cooldown 修正（误差范围内）
        print(
            f"  {w:<6} {d['p99']:<12.1f} {d['p95']:<12.1f} {d['p90']:<12.1f}"
        )

    print()
    print("  用法：")
    print("    1. 选定你能接受的告警频率（每天 1/5/10 条之一）")
    print("    2. 把对应列的值写入 config/_alerts.py 的对应阈值")
    print("    3. ./scripts/restart.sh 重启 → 24~48 小时后再次跑本脚本验证")


# ---------------------------------------------------------------------------
# 4. 最近爆发过的实体（人工对照，告警有没有漏掉真热点）
# ---------------------------------------------------------------------------


def print_recent_bursts(db: Database, days: int = 1, top: int = 15) -> None:
    _print_header(f"🔥 过去 {days} 天 growth 最高的实体（看告警漏没漏）")

    with db.get_session() as s:
        rows = s.execute(
            text(
                """
                SELECT
                  entity,
                  window_type,
                  ROUND(growth_rate::numeric, 2) AS growth,
                  count_short,
                  cross_source,
                  is_new_entity,
                  window_end
                FROM hotness_snapshots
                WHERE window_end >= NOW() - make_interval(days => :days)
                ORDER BY growth_rate DESC
                LIMIT :top
                """
            ),
            {"days": days, "top": top},
        ).all()

    if not rows:
        print("  （暂无数据）")
        return

    print(
        f"  {'#':<3} {'entity':<22} {'win':<5} {'growth':>8} "
        f"{'count':>6} {'src':>4} {'new':<4} {'time'}"
    )
    print("  " + "-" * 70)
    for i, r in enumerate(rows, 1):
        new_mark = "★" if r.is_new_entity else " "
        print(
            f"  {i:<3} {r.entity:<22} {r.window_type:<5} "
            f"{float(r.growth):>8.1f} {r.count_short:>6} "
            f"{r.cross_source:>4} {new_mark:<4} "
            f"{r.window_end.strftime('%m-%d %H:%M')}"
        )

    print(
        "\n  对照检查：上面这些 growth 很高的实体，应该都收到过告警。"
        "如果某条实际你没收到（且不在 alert_exclude_entities 黑名单里），"
        "说明阈值偏高或 cooldown 撞到了，可考虑下调阈值。"
    )


# ---------------------------------------------------------------------------
# 5. 当前配置摘要
# ---------------------------------------------------------------------------


def print_current_config(settings) -> None:
    _print_header("⚙️  当前关键配置（只看影响大的 5 个字段）")
    print()
    print(f"  alert_growth_threshold      = {settings.alert_growth_threshold}")
    print(f"  alert_3h_growth_threshold   = {settings.alert_3h_growth_threshold}")
    print(f"  alert_6h_growth_threshold   = {settings.alert_6h_growth_threshold}")
    print(f"  alert_24h_growth_threshold  = {settings.alert_24h_growth_threshold}")
    print(f"  alert_exclude_entities      = {settings.alert_exclude_entities}")
    print()
    print("  其他 60+ 字段默认值已经按数学规律算好，不建议动")
    print("  详见 docs/tuning_guide.md 的'调参禁忌'一节")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="调参诊断工具")
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="分析过去多少天的数据（默认 7）",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="只输出推荐值，跳过分布 / backtest 详情",
    )
    args = parser.parse_args()

    settings = get_settings()
    db = Database(settings)

    # 拉数据
    dist = fetch_distribution(db, args.days)
    if not dist:
        print("⚠️  hotness_snapshots 表里没有数据。")
        print("   可能原因：")
        print("   1. 服务刚启动，还没跑过整点 → 等 15 分钟再试")
        print("   2. 基线数据不足，hotness 一直跳过 → 看 logs/service.log")
        return

    # 输出
    if not args.quiet:
        print_current_config(settings)
        print_distribution(dist, args.days)
        hits = fetch_threshold_hits(db, args.days)
        print_threshold_hits(hits, settings, args.days)
        print_recent_bursts(db, days=1)

    print_recommendations(dist, args.days)
    print()
    print("📚 完整调参方法论：docs/tuning_guide.md")
    print("📚 配置字段速查：docs/configuration.md §0")
    print()


if __name__ == "__main__":
    main()
