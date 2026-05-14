"""
管道漏斗诊断：从原始消息一直到 Telegram 推送，看每一层筛掉了多少。

跟 tune_helper.py 的区别：
- tune_helper.py = 出口侧（看 growth 分布、阈值反推）
- pipeline_inspect.py = 漏斗侧（看每一层转化率、问题在哪一层）

用法：
    .venv/bin/python scripts/pipeline_inspect.py
    .venv/bin/python scripts/pipeline_inspect.py --hours 6   # 默认 1 小时
    .venv/bin/python scripts/pipeline_inspect.py --hours 24

输出 4 个段落：
1. 漏斗每层的输入 → 输出 + 转化率
2. prefilter 命中率细节（词典 vs 正则 vs 价格）
3. hotness 实际写入分析（被 exclude / top_k 过滤了多少）
4. 异常诊断 + 调参建议（哪一层不正常、对应哪个参数）

设计意图：
你看到 "告警太少" 时，第一反应不应该是 "调阈值"，而是 "管道哪一层把消息卡住了"。
本工具帮你定位问题层。配合 docs/tuning_guide.md 第 7 节使用。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from config.settings import get_settings  # noqa: E402
from db.connection import Database  # noqa: E402


# ---------------------------------------------------------------------------
# 输出工具
# ---------------------------------------------------------------------------


def _print_header(title: str) -> None:
    print()
    print("=" * 72)
    print(f" {title}")
    print("=" * 72)


def _bar(pct: float, width: int = 30) -> str:
    """
    生成一个文本进度条。pct ∈ [0, 1]
    """
    filled = int(round(pct * width))
    return "█" * filled + "░" * (width - filled)


def _pct(numerator: int, denominator: int) -> str:
    """格式化为 '57.3%'，分母为 0 返回 '-'"""
    if denominator <= 0:
        return "-"
    return f"{100.0 * numerator / denominator:.1f}%"


# ---------------------------------------------------------------------------
# 1. 漏斗主表
# ---------------------------------------------------------------------------


def fetch_funnel_counts(db: Database, hours: int) -> dict:
    """
    一次拉齐漏斗各层的计数。返回 dict 字段：

    - raw_total: 三张原始表过去 N 小时新增总数
    - raw_by_source: {twitter, binance_square, discord}
    - normalized_total: normalized_messages 过去 N 小时新增
    - dedup_kept: is_duplicate=FALSE 的
    - dedup_dropped: is_duplicate=TRUE 的
    - prefilter_kept_msgs: entity_mentions 关联的 distinct msg_id 数
                            （即至少抽到 1 个实体的消息数；prefilter keep 但抽不到实体的看不到）
    - extracted_entities: entity_mentions 总行数
    - dict_hits: confidence=1.0 的实体行数
    - regex_hits: confidence=0.95 的实体行数
    """
    result: dict = {}

    with db.get_session() as s:
        # 三张原始表分别取 N 小时内新增数
        # 注意：raw_post 的 created_at 是抓取时间，用它而不是 ts 因为 ts 可能很老
        rows = s.execute(
            text(
                """
                SELECT 'twitter' AS src, COUNT(*) AS n
                FROM twitter_posts
                WHERE created_at >= NOW() - make_interval(hours => :h)
                UNION ALL
                SELECT 'binance_square', COUNT(*)
                FROM binance_square_posts
                WHERE created_at >= NOW() - make_interval(hours => :h)
                UNION ALL
                SELECT 'discord', COUNT(*)
                FROM discord_messages
                WHERE created_at >= NOW() - make_interval(hours => :h)
                """
            ),
            {"h": hours},
        ).all()
        result["raw_by_source"] = {r.src: int(r.n) for r in rows}
        result["raw_total"] = sum(result["raw_by_source"].values())

        # normalized_messages：is_duplicate 分组
        rows = s.execute(
            text(
                """
                SELECT
                  is_duplicate,
                  COUNT(*) AS n
                FROM normalized_messages
                WHERE created_at >= NOW() - make_interval(hours => :h)
                GROUP BY is_duplicate
                """
            ),
            {"h": hours},
        ).all()
        dedup_map = {bool(r.is_duplicate): int(r.n) for r in rows}
        result["dedup_kept"] = dedup_map.get(False, 0)
        result["dedup_dropped"] = dedup_map.get(True, 0)
        result["normalized_total"] = result["dedup_kept"] + result["dedup_dropped"]

        # entity_mentions：消息维度 + 实体维度
        # 用 ts 而不是 created_at（NormalizedMessage.ts = 发布时间，
        # entity_mentions 沿用该 ts，回填语义一致）
        row = s.execute(
            text(
                """
                SELECT
                  COUNT(*) AS total_entities,
                  COUNT(DISTINCT msg_id) AS distinct_msgs,
                  COUNT(*) FILTER (WHERE confidence = 1.0) AS dict_hits,
                  COUNT(*) FILTER (WHERE confidence = 0.95) AS regex_hits
                FROM entity_mentions
                WHERE ts >= NOW() - make_interval(hours => :h)
                """
            ),
            {"h": hours},
        ).one()
        result["extracted_entities"] = int(row.total_entities or 0)
        result["prefilter_kept_msgs"] = int(row.distinct_msgs or 0)
        result["dict_hits"] = int(row.dict_hits or 0)
        result["regex_hits"] = int(row.regex_hits or 0)

    return result


def print_funnel(counts: dict, hours: int) -> None:
    _print_header(f"🚿 消息漏斗（过去 {hours} 小时）")

    raw = counts["raw_total"]
    norm = counts["normalized_total"]
    kept = counts["dedup_kept"]
    pre_kept = counts["prefilter_kept_msgs"]
    ents = counts["extracted_entities"]

    # 第一段：raw → normalized → dedup
    print()
    print("  ① 原始消息（三张 raw 表过去时段新增）")
    if counts["raw_by_source"]:
        for src, n in counts["raw_by_source"].items():
            print(f"     {src:<18} {n:>6}")
    print(f"     {'TOTAL':<18} {raw:>6}")

    print()
    print("  ② NormalizerService 归一化后")
    rate = _pct(norm, raw)
    print(f"     normalized_messages  {norm:>6}     ({rate} 来自原始)")
    if raw > 0 and norm < raw * 0.5:
        print("     ⚠️  归一化产出 < 原始 50%，可能 normalizer_batch_size 卡住或还没消费完")

    print()
    print("  ③ Deduplicator 去重")
    print(f"     去重后送 L1       {kept:>6}     ({_pct(kept, norm)} of normalized)")
    print(f"     被判重不送 L1     {counts['dedup_dropped']:>6}     ({_pct(counts['dedup_dropped'], norm)} of normalized)")
    print(f"     {_bar(kept / norm if norm else 0)} 保留率")

    print()
    print("  ④ prefilter + 实体抽取")
    print(f"     至少抽到 1 个实体  {pre_kept:>6}     ({_pct(pre_kept, kept)} of 送 L1)")
    no_ent_msgs = kept - pre_kept
    print(f"     抽不到任何实体     {no_ent_msgs:>6}     ({_pct(no_ent_msgs, kept)} of 送 L1)")
    print("     说明：'抽不到任何实体' 包含 prefilter drop + keep 但词典/正则都没命中")
    print(f"     {_bar(pre_kept / kept if kept else 0)} 实体产出率")

    print()
    print("  ⑤ entity_mentions 总条数")
    avg = ents / pre_kept if pre_kept > 0 else 0
    print(f"     总实体提及          {ents:>6}     (平均每条消息 {avg:.2f} 个实体)")
    print(f"     词典命中 conf=1.0    {counts['dict_hits']:>6}     ({_pct(counts['dict_hits'], ents)})")
    print(f"     正则命中 conf=0.95   {counts['regex_hits']:>6}     ({_pct(counts['regex_hits'], ents)})")


# ---------------------------------------------------------------------------
# 2. prefilter / 词典命中率细节
# ---------------------------------------------------------------------------


def fetch_entity_type_distribution(db: Database, hours: int) -> list:
    """按 entity_type 分组的命中条数"""
    with db.get_session() as s:
        rows = s.execute(
            text(
                """
                SELECT
                  entity_type,
                  COUNT(*) AS n,
                  COUNT(*) FILTER (WHERE confidence = 1.0) AS dict_hits,
                  COUNT(*) FILTER (WHERE confidence = 0.95) AS regex_hits
                FROM entity_mentions
                WHERE ts >= NOW() - make_interval(hours => :h)
                GROUP BY entity_type
                ORDER BY n DESC
                """
            ),
            {"h": hours},
        ).all()
    return rows


def print_entity_type_distribution(rows, hours: int) -> None:
    _print_header(f"🏷  实体类型分布（过去 {hours} 小时）")

    if not rows:
        print("\n  (没有数据)")
        return

    total = sum(int(r.n) for r in rows)
    print()
    print(f"  {'entity_type':<14} {'count':>8} {'词典':>6} {'正则':>6} {'占比':>8}")
    print("  " + "-" * 50)
    for r in rows:
        et = r.entity_type or "(unknown)"
        print(
            f"  {et:<14} {int(r.n):>8} {int(r.dict_hits):>6} "
            f"{int(r.regex_hits):>6} {_pct(int(r.n), total):>8}"
        )

    print()
    print("  说明：")
    print("    - ticker/chain/narrative/kol：词典命中（来自 dictionaries/*.yaml）")
    print("    - project：正则命中（EVM 0x.. / Solana 地址）")
    print("    - 各类型占比偏离正常预期 → 词典需要补充或 prefilter 规则需调整")


# ---------------------------------------------------------------------------
# 3. hotness 写入 → 告警的最后一段过滤
# ---------------------------------------------------------------------------


def fetch_hotness_funnel(db: Database, hours: int, settings) -> dict:
    """
    最近一份 1h 榜的"理论候选 → 实际写入 → 通过阈值"的过滤数据。

    考虑到 hotness 每 15min 写一份，过去 N 小时大约 4N 份。
    我们看：每份榜平均写 X 条，其中 growth >= 阈值的多少条。
    """
    result: dict = {}

    with db.get_session() as s:
        # 各窗口最近 N 小时写入的快照总数
        rows = s.execute(
            text(
                """
                SELECT
                  window_type,
                  COUNT(*) AS total_records,
                  COUNT(DISTINCT entity) AS distinct_entities,
                  COUNT(DISTINCT window_end) AS distinct_windows
                FROM hotness_snapshots
                WHERE window_end >= NOW() - make_interval(hours => :h)
                GROUP BY window_type
                ORDER BY window_type
                """
            ),
            {"h": hours},
        ).all()
        result["per_window"] = {
            r.window_type: {
                "records": int(r.total_records),
                "entities": int(r.distinct_entities),
                "windows": int(r.distinct_windows),
            }
            for r in rows
        }

        # 各窗口在自己阈值下"通过门槛"的快照条数（不含 cooldown 影响）
        thresholds = {
            "1h": settings.alert_growth_threshold,
            "3h": settings.alert_3h_growth_threshold,
            "6h": settings.alert_6h_growth_threshold,
            "24h": settings.alert_24h_growth_threshold,
        }
        result["thresholds"] = thresholds

        passes = {}
        for wt, th in thresholds.items():
            row = s.execute(
                text(
                    """
                    SELECT COUNT(*) AS n
                    FROM hotness_snapshots
                    WHERE window_type = :wt
                      AND window_end >= NOW() - make_interval(hours => :h)
                      AND growth_rate >= :th
                    """
                ),
                {"wt": wt, "h": hours, "th": th},
            ).one()
            passes[wt] = int(row.n)
        result["pass_threshold"] = passes

    return result


def print_hotness_funnel(data: dict, hours: int) -> None:
    _print_header(f"📈 hotness → 告警 漏斗（过去 {hours} 小时）")

    print()
    print(
        f"  {'window':<6} {'写入快照':>10} {'独立 entity':>12} "
        f"{'阈值':>6} {'≥阈值':>8} {'≈ 告警/h':>12}"
    )
    print("  " + "-" * 70)

    for wt in ("1h", "3h", "6h", "24h"):
        per = data["per_window"].get(wt, {})
        records = per.get("records", 0)
        entities = per.get("entities", 0)
        th = data["thresholds"][wt]
        passes = data["pass_threshold"][wt]
        per_hour = passes / hours if hours > 0 else 0
        print(
            f"  {wt:<6} {records:>10} {entities:>12} "
            f"{th:>6.1f} {passes:>8} {per_hour:>12.2f}"
        )

    print()
    print("  说明：")
    print("    - '写入快照' = 该窗口过去时段产出的 hotness_snapshots 行数（含所有 entity）")
    print("    - '≥ 阈值' = 其中 growth_rate ≥ 当前 alert 阈值的快照数")
    print("    - '≈ 告警/h' = 是上界，实际告警还会被 cooldown 进一步压缩")


# ---------------------------------------------------------------------------
# 4. 异常诊断 + 建议
# ---------------------------------------------------------------------------


def print_diagnosis(counts: dict, hotness_data: dict, hours: int) -> None:
    _print_header("🔬 异常诊断 + 调参建议")

    raw = counts["raw_total"]
    norm = counts["normalized_total"]
    kept = counts["dedup_kept"]
    pre_kept = counts["prefilter_kept_msgs"]
    ents = counts["extracted_entities"]

    issues: list[str] = []
    healthy: list[str] = []

    # ---- ① 原始流量 ----
    rate_per_hour = raw / hours if hours > 0 else 0
    if raw == 0:
        issues.append(
            "❌ 原始三张表过去时段都没新数据。检查上游抓取服务是否在跑。"
        )
    elif rate_per_hour < 50:
        issues.append(
            f"⚠️  原始消息流量低（{rate_per_hour:.0f} 条/小时）。"
            "数据稀的话 hotness 公式靠 smoothing 兜底，growth 普遍偏低，告警自然少。"
        )
    else:
        healthy.append(f"✅ 原始消息流量稳健（{rate_per_hour:.0f} 条/小时）")

    # ---- ② 归一化 ----
    if raw > 0:
        norm_rate = norm / raw
        if norm_rate < 0.5:
            issues.append(
                f"⚠️  归一化产出率低（{norm_rate:.0%}）。"
                "可能 normalizer_batch_size 跟不上抓取速率，调大 batch_size 或调密 worker 轮询。"
            )
        else:
            healthy.append(f"✅ 归一化产出率正常（{norm_rate:.0%}）")

    # ---- ③ 去重 ----
    if norm > 0:
        dup_rate = counts["dedup_dropped"] / norm
        if dup_rate > 0.6:
            issues.append(
                f"⚠️  去重率偏高（{dup_rate:.0%} 被判重）。"
                "可能 dedup_hamming_threshold 设太松，或上游抓取在重复抓同一时段。"
                "检查 config/_new.py 的 dedup_hamming_threshold（默认 3，调到 2 更严）。"
            )
        elif dup_rate < 0.05 and norm > 100:
            issues.append(
                f"⚠️  去重率偏低（{dup_rate:.0%}）。"
                "通常 crypto 圈消息有 10%~40% 转发重复，太低说明 SimHash 阈值可能漏判。"
            )
        else:
            healthy.append(f"✅ 去重率合理（{dup_rate:.0%}）")

    # ---- ④ prefilter ----
    if kept > 0:
        ent_rate = pre_kept / kept
        if ent_rate < 0.10:
            issues.append(
                f"⚠️  实体抽取覆盖率极低（{ent_rate:.0%} 的 keep 消息抽到了实体）。"
                "可能词典覆盖不够（看下面 🏷 实体类型分布），或 prefilter 规则把太多有信号的消息丢了。"
            )
        elif ent_rate < 0.30:
            issues.append(
                f"💡 实体抽取覆盖率偏低（{ent_rate:.0%}）。"
                "看 dictionaries/*.yaml 是否需要补充新出的 ticker / narrative。"
            )
        else:
            healthy.append(f"✅ 实体抽取覆盖率正常（{ent_rate:.0%}）")

    # ---- ⑤ 词典 / 正则比例 ----
    if ents > 0:
        dict_rate = counts["dict_hits"] / ents
        if dict_rate < 0.30:
            issues.append(
                f"💡 词典命中率偏低（{dict_rate:.0%}），多数实体来自正则（$XXX）。"
                "词典命中 confidence=1.0 是高质量信号，命中低意味着用户在聊词典外的新币。"
                "可以观察一周后把高频出现的新币加到 dictionaries/tickers.yaml。"
            )
        else:
            healthy.append(f"✅ 词典 / 正则命中比例合理（词典 {dict_rate:.0%}）")

    # ---- ⑥ hotness 阈值 ----
    for wt in ("1h", "3h", "6h", "24h"):
        passes = hotness_data["pass_threshold"][wt]
        per_day_estimate = passes * 24 / hours if hours > 0 else 0
        # 用 cooldown 因子估算实际告警频率
        cooldown_factor = {"1h": 0.33, "3h": 0.50, "6h": 0.65, "24h": 0.80}[wt]
        effective_per_day = per_day_estimate * (1 - cooldown_factor)
        if effective_per_day < 0.3:
            issues.append(
                f"⚠️  {wt} 窗口阈值过严：当前阈值 {hotness_data['thresholds'][wt]} → "
                f"预估每天告警 < 1 条。跑 tune_helper.py 看推荐阈值。"
            )
        elif effective_per_day > 30:
            issues.append(
                f"⚠️  {wt} 窗口阈值过松：当前阈值 {hotness_data['thresholds'][wt]} → "
                f"预估每天告警 > 30 条（可能刷屏）。跑 tune_helper.py 看推荐阈值。"
            )

    # 输出
    print()
    if healthy:
        for h in healthy:
            print(f"  {h}")
    print()
    if issues:
        print("  发现问题：")
        for issue in issues:
            print(f"    {issue}")
    else:
        print("  🎉 各层指标都健康，没有明显异常。")
    print()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="管道漏斗诊断：看消息从原始到告警每一层筛掉了多少",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=1,
        help="分析过去多少小时的数据（默认 1）",
    )
    args = parser.parse_args()

    settings = get_settings()
    db = Database(settings)

    counts = fetch_funnel_counts(db, args.hours)
    if counts["raw_total"] == 0 and counts["normalized_total"] == 0:
        print()
        print("⚠️  过去 {} 小时三张表都没有数据。".format(args.hours))
        print("   - 上游抓取服务可能没在跑")
        print("   - 也可能时段太短，调大 --hours 参数")
        return

    hotness_data = fetch_hotness_funnel(db, args.hours, settings)
    type_rows = fetch_entity_type_distribution(db, args.hours)

    print_funnel(counts, args.hours)
    print_entity_type_distribution(type_rows, args.hours)
    print_hotness_funnel(hotness_data, args.hours)
    print_diagnosis(counts, hotness_data, args.hours)

    print("📚 完整管道说明：docs/tuning_guide.md §7")
    print("📚 出口侧调参（看分布定阈值）：scripts/tune_helper.py")
    print()


if __name__ == "__main__":
    main()
