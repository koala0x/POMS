from __future__ import annotations

"""
Deduplicator 单元测试（Task 4.4，对应 requirements.md Req 2.1~2.3）。

测试策略：
- 纯内存，不依赖 DB / Ollama
- 用固定时间戳手动控制小时桶分布，避免 time.time() 导致的不确定性
- 直接断言 `_buckets` 字典的结构（受控白盒测试），便于排查回归

覆盖的子任务：
- test_identical_text_is_duplicate       —— Req 2.1：同文判重 + 返回 dup_of
- test_near_text_within_hamming_threshold —— Req 2.2：近似文本在汉明阈值内判重
- test_different_text_not_duplicate       —— Req 2.2：差异文本不判重
- test_old_bucket_evicted                 —— Req 2.3：window_hours 外的桶被淘汰
- test_hamming_calculation                —— 辅助纯函数自检
"""

from services.l0_dedup import Deduplicator


# ---------------------------------------------------------------------------
# 工具：固定的小时基准
# ---------------------------------------------------------------------------

# 任选一个整点 UTC 时间戳作为"现在"，避免 time.time() 引入非确定性
_BASE_TS = 1_700_000_000.0  # 2023-11-14 22:13:20 UTC，落在 bucket = 472222


# ---------------------------------------------------------------------------
# Req 2.1 + 2.2：判重正例
# ---------------------------------------------------------------------------


def test_identical_text_is_duplicate() -> None:
    """
    Req 2.1：同一条文本前后两次进入，第二次必须被判重，
    dup_of 指向第一条的 msg_id。
    """
    dedup = Deduplicator()

    text = "Bitcoin breaking $73000 on ETF inflows"
    sh1 = dedup.compute_simhash(text)
    dedup.add(sh1, msg_id=101, ts=_BASE_TS)

    # 相同文本 → SimHash 相同 → 汉明距离 = 0 → 必判重
    sh2 = dedup.compute_simhash(text)
    is_dup, dup_of = dedup.is_duplicate(sh2, _BASE_TS)
    assert is_dup is True
    assert dup_of == 101


def test_near_text_within_hamming_threshold() -> None:
    """
    Req 2.2：改动少量词汇（大部分 token 仍重合）时 SimHash 汉明距离应保持在阈值 3 内。

    simhash 库以空白切分 token，忽略标点。想让两串文本产生"不同但接近"的指纹，
    必须真的替换至少一个词（如 globe → world），并用较长的主干让单 token 改动
    在指纹中的扰动维持在 1~3 bit 之间。此处用约 45 token 的句子 + 一个同义词替换，
    已经通过脚本验证汉明距离 = 1。
    """
    dedup = Deduplicator(hamming_threshold=3, window_hours=24)

    original = (
        "The market for cryptocurrency is rapidly expanding as more "
        "institutional investors continue to pour billions of dollars into "
        "Bitcoin and Ethereum while regulators around the globe work hard on "
        "establishing clearer legal frameworks for digital assets next year"
    )
    tweaked = (
        "The market for cryptocurrency is rapidly expanding as more "
        "institutional investors continue to pour billions of dollars into "
        "Bitcoin and Ethereum while regulators around the world work hard on "
        "establishing clearer legal frameworks for digital assets next year"
    )

    sh1 = dedup.compute_simhash(original)
    dedup.add(sh1, msg_id=201, ts=_BASE_TS)

    sh2 = dedup.compute_simhash(tweaked)
    # 确认确实是"近似不同"（sh 值不完全相等），否则这条测试没意义
    assert sh1 != sh2, "测试前提失效：预期两串文本产生不同 SimHash"
    # 再确认距离的确在阈值内（白盒自检，便于未来 simhash 行为漂移时快速定位）
    dist = Deduplicator._hamming(sh1, sh2)
    assert 1 <= dist <= 3, f"测试前提失效：期望 hamming 距离在 [1,3]，实际 {dist}"

    is_dup, dup_of = dedup.is_duplicate(sh2, _BASE_TS)
    assert is_dup is True, f"near-text 必须在 hamming_threshold 内判重，sh1={sh1}, sh2={sh2}"
    assert dup_of == 201


def test_different_text_not_duplicate() -> None:
    """
    Req 2.2：完全不同的文本（不同话题、不同 token）汉明距离远超阈值，不判重。
    """
    dedup = Deduplicator(hamming_threshold=3, window_hours=24)

    sh1 = dedup.compute_simhash(
        "Solana TVL rising 15 percent this week as DeFi protocols onboard"
    )
    dedup.add(sh1, msg_id=301, ts=_BASE_TS)

    sh2 = dedup.compute_simhash(
        "美联储九月议息会议纪要释放鸽派信号，黄金创下新高"
    )
    is_dup, dup_of = dedup.is_duplicate(sh2, _BASE_TS)
    assert is_dup is False
    assert dup_of is None


# ---------------------------------------------------------------------------
# Req 2.3：窗口淘汰
# ---------------------------------------------------------------------------


def test_old_bucket_evicted() -> None:
    """
    Req 2.3：超过 window_hours 的旧桶在下一次 add 时被清理。

    场景：
    - 在 t0 桶 add 一条消息
    - 在 t0 + (window_hours + 1) * 3600 的桶再 add 一条
    - _evict_old 触发后，t0 所在桶应从 `_buckets` 中消失
    - 此时以 t0 时刻去 is_duplicate 查询也不应命中（桶已经没了）
    """
    dedup = Deduplicator(hamming_threshold=3, window_hours=24)

    old_ts = _BASE_TS
    old_sh = dedup.compute_simhash("old message that should be evicted")
    dedup.add(old_sh, msg_id=401, ts=old_ts)
    old_bucket = int(old_ts // 3600)
    assert old_bucket in dedup._buckets, "前置：旧消息应在桶里"

    # 25 小时后再添加一条（触发 _evict_old，cutoff = new_bucket - 24 = old_bucket + 1）
    new_ts = old_ts + 25 * 3600
    new_sh = dedup.compute_simhash("completely different topic about something else")
    dedup.add(new_sh, msg_id=402, ts=new_ts)

    assert old_bucket not in dedup._buckets, (
        "旧桶应被淘汰，实际桶列表：" + str(sorted(dedup._buckets.keys()))
    )

    # 再以 old_sh 查询，因桶已被清理，不会命中
    # 注意 is_duplicate 用 now_ts 决定扫描范围，这里用 new_ts 查询
    is_dup, _ = dedup.is_duplicate(old_sh, new_ts)
    assert is_dup is False, "旧消息所在桶已被淘汰，对它的近似查询不应命中"


# ---------------------------------------------------------------------------
# 纯函数自检
# ---------------------------------------------------------------------------


def test_hamming_calculation() -> None:
    """
    `_hamming(a, b)` 返回两个整数异或后 1 bit 的数量。

    用几组简单的输入做表驱动断言，确认实现正确。
    """
    # 0b1010 ^ 0b0101 = 0b1111 → 4 个 1
    assert Deduplicator._hamming(0b1010, 0b0101) == 4
    # 相同整数 → 异或为 0 → 0 个 1
    assert Deduplicator._hamming(0xDEADBEEF, 0xDEADBEEF) == 0
    # 只差最低 1 bit → 1
    assert Deduplicator._hamming(0xFF, 0xFE) == 1
    # 一个 64 位全 1 与 0 的汉明距离 = 64
    assert Deduplicator._hamming((1 << 64) - 1, 0) == 64
