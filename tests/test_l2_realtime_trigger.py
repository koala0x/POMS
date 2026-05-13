from __future__ import annotations

"""
RealtimeAlertService 单元测试（Phase 2 Task 2.4 → Task 1.4，对应
requirements.md Req 6.1 + design.md §5 测试矩阵 #1~#6）。

覆盖 6 个用例：
  1. test_notify_below_threshold_does_not_trigger
  2. test_notify_at_threshold_triggers_immediate
  3. test_immediate_uses_realtime_threshold（growth=25 不告警 / growth=35 告警）
  4. test_immediate_shares_alert_records_with_integral（验证 dict 引用语义）
  5. test_immediate_does_not_write_hotness_snapshots（dataclass 无 hotness_repo
     字段 + 跑完只读 mentions_repo）
  6. test_immediate_send_failure_does_not_consume_pending

测试策略（与 tests/test_l2_alert_trigger.py 风格一致）：
- 不真连 PG / 不真调 Telegram；mock SlidingCounter / EntityMentionsRepo /
  TelegramClient + monkeypatch services.l2_realtime_trigger.datetime
- 全部用 unittest.mock.MagicMock；db 走 MagicMock（with db.get_session() as s
  返回的 session 不会被 repo mock 真正使用）
- 公式 growth_rate = short_count / max(baseline_total / 167, smoothing=2)
  → short=70, baseline_total<<334 ⇒ growth=70/2=35（命中 threshold=30）
  → short=50, baseline_total<<334 ⇒ growth=50/2=25（不命中）
"""

from dataclasses import fields
from datetime import datetime, timezone
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from db.connection import Database
from notifications.telegram_client import TelegramClient
from services import l2_realtime_trigger as rt_module
from services.l2_realtime_trigger import RealtimeAlertService
from services.l2_sliding_counter import SlidingCounter


# ===========================================================================
# 辅助：mock 工厂 + 时间 patch + 默认 service 构造
# ===========================================================================


def _patch_now(monkeypatch, fake_now: datetime) -> None:
    """
    替换 services.l2_realtime_trigger 模块里的 datetime，让 datetime.now(tz)
    返回 fake_now（必须是 aware datetime）。

    与 tests/test_l2_alert_trigger.py 同一模式：替换整个 datetime 类，让
    模块内 timedelta / timezone 引用不受影响。
    """

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return fake_now.astimezone(tz)
            return fake_now

    monkeypatch.setattr(rt_module, "datetime", _FakeDateTime)


def _make_db_mock() -> MagicMock:
    """
    构造一个 MagicMock(spec=Database)。
    `with db.get_session() as session` 自动走 MagicMock 的 __enter__/__exit__，
    返回的 session 也是 MagicMock，但 repo 调用时 session 会被传给 mock 的
    count_for_entity / count_sources_for_entity，session 自身不会被使用。
    """
    return MagicMock(spec=Database)


def _make_mentions_mock(
    *, baseline_total: int = 0, cross_source: int = 1
) -> MagicMock:
    """构造 mentions_repo mock，count_for_entity / count_sources_for_entity 返回固定值。"""
    m = MagicMock()
    m.count_for_entity.return_value = baseline_total
    m.count_sources_for_entity.return_value = cross_source
    return m


def _make_sliding_counter_mock(
    *, candidates: list[str] | None = None, short_count: int = 0
) -> MagicMock:
    """构造 SlidingCounter mock。"""
    sc = MagicMock(spec=SlidingCounter)
    sc.active_entities.return_value = candidates if candidates is not None else []
    sc.count.return_value = short_count
    return sc


def _make_telegram_mock(*, send_ok: bool = True) -> MagicMock:
    """构造 TelegramClient mock，send_text 返回 send_ok。"""
    tg = MagicMock(spec=TelegramClient)
    tg.send_text.return_value = send_ok
    return tg


def _make_service(
    *,
    db: MagicMock,
    mentions_repo: MagicMock,
    sliding_counter: MagicMock,
    telegram_client: MagicMock,
    shared_alert_records: dict | None = None,
    burst_threshold: int = 50,
    growth_threshold: float = 30.0,
    min_count_short: int = 5,
) -> RealtimeAlertService:
    """统一构造 RealtimeAlertService。其他参数走 dataclass 默认值。"""
    return RealtimeAlertService(
        db=db,
        mentions_repo=mentions_repo,
        sliding_counter=sliding_counter,
        telegram_client=telegram_client,
        shared_alert_records=shared_alert_records
        if shared_alert_records is not None
        else {},
        burst_threshold=burst_threshold,
        growth_threshold=growth_threshold,
        min_count_short=min_count_short,
        smoothing=2.0,
        baseline_days=7,
        timezone=ZoneInfo("UTC"),
    )


# ===========================================================================
# 用例 1：notify(40) 未达阈值 → 不触发
# ===========================================================================


def test_notify_below_threshold_does_not_trigger(monkeypatch) -> None:
    """notify(40) 不触发 _trigger_immediate；send_text 0 次；_pending_count==40。"""
    _patch_now(
        monkeypatch, datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc)
    )

    db = _make_db_mock()
    mentions = _make_mentions_mock()
    sc = _make_sliding_counter_mock(candidates=["NEWMEME"], short_count=70)
    tg = _make_telegram_mock()

    svc = _make_service(
        db=db,
        mentions_repo=mentions,
        sliding_counter=sc,
        telegram_client=tg,
        burst_threshold=50,
    )

    svc.notify(40)

    assert tg.send_text.call_count == 0
    assert svc._pending_count == 40
    # _trigger_immediate 没跑 → 候选集 / DB / 时间戳都没动
    assert sc.active_entities.call_count == 0
    assert mentions.count_for_entity.call_count == 0
    assert svc._last_triggered_at is None


# ===========================================================================
# 用例 2：notify(50) 达阈值 → 触发并清零
# ===========================================================================


def test_notify_at_threshold_triggers_immediate(monkeypatch) -> None:
    """notify(50) 命中阈值 → 触发 send_text 一次；成功后 _pending_count 清零。"""
    _patch_now(
        monkeypatch, datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc)
    )

    db = _make_db_mock()
    # baseline_total=4 → baseline_per_h = 4/167 ≈ 0.024 → max(0.024, 2)=2
    # short=70 → growth = 70/2 = 35 ≥ growth_threshold=30 ✓
    mentions = _make_mentions_mock(baseline_total=4, cross_source=1)
    sc = _make_sliding_counter_mock(candidates=["NEWMEME"], short_count=70)
    tg = _make_telegram_mock(send_ok=True)

    svc = _make_service(
        db=db,
        mentions_repo=mentions,
        sliding_counter=sc,
        telegram_client=tg,
        burst_threshold=50,
        growth_threshold=30.0,
        min_count_short=5,
    )

    svc.notify(50)

    assert tg.send_text.call_count == 1
    # send_text 成功 + 无失败 → 清零
    assert svc._pending_count == 0
    assert svc._last_triggered_at is not None
    text = tg.send_text.call_args.args[0]
    # 实时告警标签必须含 [实时]，首次告警含 [首次]
    assert "[实时]" in text
    assert "[首次]" in text
    assert "NEWMEME" in text


# ===========================================================================
# 用例 3：growth_threshold=30，growth=25 不告警 / growth=35 告警
# ===========================================================================


def test_immediate_uses_realtime_threshold(monkeypatch) -> None:
    """
    验证 growth_threshold 被遵守：
    - short=50（growth=50/2=25 < 30）→ send_text 0 次
    - short=70（growth=70/2=35 ≥ 30）→ send_text 1 次
    """
    _patch_now(
        monkeypatch, datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc)
    )

    # 场景 A：growth=25，不告警
    db_a = _make_db_mock()
    mentions_a = _make_mentions_mock(baseline_total=4, cross_source=1)
    sc_a = _make_sliding_counter_mock(candidates=["NEWMEME"], short_count=50)
    tg_a = _make_telegram_mock(send_ok=True)
    svc_a = _make_service(
        db=db_a,
        mentions_repo=mentions_a,
        sliding_counter=sc_a,
        telegram_client=tg_a,
        growth_threshold=30.0,
    )
    svc_a.notify(50)
    assert tg_a.send_text.call_count == 0, (
        "growth=25 < threshold=30 应被 _is_eligible 过滤掉"
    )

    # 场景 B：growth=35，告警
    db_b = _make_db_mock()
    mentions_b = _make_mentions_mock(baseline_total=4, cross_source=1)
    sc_b = _make_sliding_counter_mock(candidates=["NEWMEME"], short_count=70)
    tg_b = _make_telegram_mock(send_ok=True)
    svc_b = _make_service(
        db=db_b,
        mentions_repo=mentions_b,
        sliding_counter=sc_b,
        telegram_client=tg_b,
        growth_threshold=30.0,
    )
    svc_b.notify(50)
    assert tg_b.send_text.call_count == 1, (
        "growth=35 ≥ threshold=30 应触发告警"
    )


# ===========================================================================
# 用例 4：shared_alert_records 是引用语义（外部 dict 被同步更新）
# ===========================================================================


def test_immediate_shares_alert_records_with_integral(monkeypatch) -> None:
    """
    main.py 传 alert_service._alert_records 同一引用 → RealtimeAlertService
    写入冷却记录时，外部 dict 必须能直接看到（不是 copy 隔离）。
    """
    _patch_now(
        monkeypatch, datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc)
    )

    external_records: dict = {}
    db = _make_db_mock()
    mentions = _make_mentions_mock(baseline_total=4, cross_source=1)
    sc = _make_sliding_counter_mock(candidates=["NEWMEME"], short_count=70)
    tg = _make_telegram_mock(send_ok=True)

    svc = _make_service(
        db=db,
        mentions_repo=mentions,
        sliding_counter=sc,
        telegram_client=tg,
        shared_alert_records=external_records,  # ★ 传入外部 dict 引用
    )

    # 验证构造完之后两边确实是同一对象（不是被 dataclass 默认值替换掉）
    assert svc.shared_alert_records is external_records

    svc.notify(50)

    # ★ 关键断言：外部 dict 被同步写入
    assert "NEWMEME" in external_records
    rec = external_records["NEWMEME"]
    assert rec.last_growth_rate == 35.0
    assert rec.last_cross_source == 1
    # 反向断言：外部 dict 与 service 内部字段是同一对象
    assert svc.shared_alert_records is external_records


# ===========================================================================
# 用例 5：不写 hotness_snapshots —— dataclass 无该字段 + 跑完只读 mentions_repo
# ===========================================================================


def test_immediate_does_not_write_hotness_snapshots(monkeypatch) -> None:
    """
    实时榜不污染整点对齐的 hotness_snapshots 表（design §1.2 第 3 条核心哲学）。

    断言两条：
    1. 结构：RealtimeAlertService dataclass 没有 hotness_repo 字段（编译期保证）
    2. 行为：_trigger_immediate 跑完后只调 mentions_repo 的两个**只读**方法
       （count_for_entity / count_sources_for_entity），无任何写库 / hotness 调用
    """
    # 结构断言：dataclass 字段名清单里不能有 hotness_repo
    field_names = {f.name for f in fields(RealtimeAlertService)}
    assert "hotness_repo" not in field_names, (
        "RealtimeAlertService 不应含 hotness_repo 字段（实时榜禁止落库）"
    )

    _patch_now(
        monkeypatch, datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc)
    )

    db = _make_db_mock()
    mentions = _make_mentions_mock(baseline_total=4, cross_source=1)
    sc = _make_sliding_counter_mock(candidates=["NEWMEME"], short_count=70)
    tg = _make_telegram_mock(send_ok=True)

    svc = _make_service(
        db=db,
        mentions_repo=mentions,
        sliding_counter=sc,
        telegram_client=tg,
    )
    svc.notify(50)

    # 行为断言：mentions_repo 只被读，没有任何写方法被调
    assert mentions.count_for_entity.call_count == 1
    assert mentions.count_sources_for_entity.call_count == 1
    assert mentions.bulk_upsert.call_count == 0
    # 用 method_calls 兜底：mentions_repo 上发生过的所有调用都是这两个只读方法
    called_methods = {c[0] for c in mentions.method_calls}
    assert called_methods <= {"count_for_entity", "count_sources_for_entity"}, (
        f"mentions_repo 出现了非预期的调用：{called_methods}"
    )


# ===========================================================================
# 用例 6：send_text 失败 → _pending_count 保留 + _last_triggered_at 更新
# ===========================================================================


def test_immediate_send_failure_does_not_consume_pending(monkeypatch) -> None:
    """
    send_text 返回 False（Telegram 不可达）：
    - _pending_count 不清零（下一轮 burst 还能重试）
    - _last_triggered_at 仍更新（防 notify 限频失效后反复触发）
    """
    _patch_now(
        monkeypatch, datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc)
    )

    db = _make_db_mock()
    mentions = _make_mentions_mock(baseline_total=4, cross_source=1)
    sc = _make_sliding_counter_mock(candidates=["NEWMEME"], short_count=70)
    tg = _make_telegram_mock(send_ok=False)  # ★ Telegram 不可达

    svc = _make_service(
        db=db,
        mentions_repo=mentions,
        sliding_counter=sc,
        telegram_client=tg,
        burst_threshold=50,
    )

    svc.notify(50)

    # send_text 被调，但返回 False
    assert tg.send_text.call_count == 1
    # ★ pending 保留（不清零，下一轮 burst 再试）
    assert svc._pending_count == 50
    # ★ 触发时间戳更新（防限频失效后反复触发）
    assert svc._last_triggered_at is not None
    # 失败路径下不应写入 shared_alert_records
    assert "NEWMEME" not in svc.shared_alert_records
