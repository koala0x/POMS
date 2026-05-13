from __future__ import annotations

"""
AlertTriggerService 单元 + 集成测试（Phase 2 Task 3.2，对应 requirements.md
Req 2 / 6.2 / 7）。

测试策略：
- SQLite in-memory + 子类化 HotnessSnapshotsRepo（绕开 PG ON CONFLICT）
  种数据走真 SQLAlchemy ORM 路径，验证 repo 接口能正常被 Service 调用
- TelegramClient 用 MagicMock 替换，断言 send_text 被调用次数 / 文本内容
- monkeypatch `services.l2_alert_trigger.datetime` 让 datetime.now(tz) 返回
  固定值，控制冷却 / 心跳判断的相对时间

覆盖 11 个用例（对应 design.md §5 测试矩阵 alert_trigger 部分）：
- test_first_alert_when_all_conditions_met         [首次]
- test_alert_skipped_when_growth_below_threshold
- test_alert_skipped_when_count_short_below_threshold
- test_alert_skipped_when_cross_source_below_threshold
- test_no_alert_within_cooldown_without_escalation （冷却内无质变）
- test_growth_doubled_triggers_escalation          [升级]
- test_cross_source_increase_triggers_escalation   [跨源升级]
- test_heartbeat_after_6h_without_escalation       [持续]
- test_alert_after_cooldown_with_no_change         [重新触发]
- test_skips_same_window_on_repeat_run
- test_send_failure_does_not_update_alert_record
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import Base, HotnessSnapshot
from db.repositories.hotness_snapshots_repo import HotnessSnapshotsRepo
from services import l2_alert_trigger as alert_module
from services.l2_alert_trigger import AlertRecord, AlertTriggerService


# ===========================================================================
# 辅助：SQLite-friendly Hotness repo（绕开 PG on_conflict_do_update）
# ===========================================================================


class _SqliteHotnessSnapshotsRepo(HotnessSnapshotsRepo):
    """
    SQLite 版 repo：覆盖 upsert_batch 用 SELECT-then-UPDATE/INSERT 等价实现。
    本测试主要走 fetch_*（读路径），upsert 仅用于 fixture 种数据。

    SQLite BigInteger 主键不自增，手动维护 _id_counter。
    """

    _id_counter: int = 0

    def upsert_batch(
        self,
        session: Session,
        *,
        window_end: datetime,
        window_type: str,
        records: list[dict],
    ) -> int:
        if not records:
            return 0

        entities = [r["entity"] for r in records]
        existing_stmt = select(HotnessSnapshot).where(
            HotnessSnapshot.window_end == window_end,
            HotnessSnapshot.window_type == window_type,
            HotnessSnapshot.entity.in_(entities),
        )
        existing = {r.entity: r for r in session.scalars(existing_stmt).all()}

        for r in records:
            if r["entity"] in existing:
                row = existing[r["entity"]]
                for k, v in r.items():
                    if k == "entity":
                        continue
                    setattr(row, k, v)
            else:
                type(self)._id_counter += 1
                session.add(
                    HotnessSnapshot(
                        id=type(self)._id_counter,
                        window_end=window_end,
                        window_type=window_type,
                        **r,
                    )
                )

        session.flush()
        return len(records)


@dataclass
class _SqliteDatabase:
    session_factory: sessionmaker[Session]

    @contextmanager
    def get_session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


@pytest.fixture()
def sqlite_db() -> _SqliteDatabase:
    """SQLite in-memory 数据库 + 只建 hotness_snapshots 一张表。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine, tables=[HotnessSnapshot.__table__])
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return _SqliteDatabase(session_factory=factory)


@pytest.fixture()
def hotness_repo() -> _SqliteHotnessSnapshotsRepo:
    """每个测试用全新的 repo + 重置 id 计数器。"""
    _SqliteHotnessSnapshotsRepo._id_counter = 0
    return _SqliteHotnessSnapshotsRepo()


# ===========================================================================
# 辅助：种数据 / Mock 时间 / 构造 Service
# ===========================================================================


# Phase 1 已踩过的坑：SQLite 的 DateTime(timezone=True) 丢 tzinfo，
# 测试时统一用 naive datetime 避免类型错位
_BASE_WINDOW_END = datetime(2026, 5, 14, 10, 0, 0)


def _seed_records(
    sqlite_db: _SqliteDatabase,
    repo: _SqliteHotnessSnapshotsRepo,
    *,
    window_end: datetime = _BASE_WINDOW_END,
    records: list[dict],
) -> None:
    """种一批 hotness_snapshots 记录（同一 window_end）。"""
    with sqlite_db.get_session() as s:
        repo.upsert_batch(
            s,
            window_end=window_end,
            window_type="1h",
            records=records,
        )
        s.commit()


def _make_record(
    entity: str,
    *,
    growth_rate: float = 25.0,
    count_short: int = 10,
    cross_source: int = 1,
    is_new_entity: bool = False,
    rank: int = 1,
    entity_type: str = "ticker",
    final_score: float | None = None,
    count_baseline: float = 0.5,
) -> dict:
    """
    生成一条 hotness 记录的字典（用于 upsert_batch 的 records 参数）。

    默认值：满足三道门槛（growth=25 ≥ 20, count=10 ≥ 3, cross=1 ≥ 1）。
    """
    return {
        "entity": entity,
        "entity_type": entity_type,
        "count_short": count_short,
        "count_baseline": count_baseline,
        "growth_rate": growth_rate,
        "cross_source": cross_source,
        "engagement_sum": 0,
        "is_new_entity": is_new_entity,
        "final_score": final_score if final_score is not None else growth_rate,
        "rank": rank,
    }


def _patch_now(monkeypatch, fake_now: datetime) -> None:
    """
    替换 services.l2_alert_trigger 模块里的 datetime，让 datetime.now(tz)
    返回 fake_now（必须是 aware datetime）。

    必须替换整个 datetime 类（而不是只 patch now）才能让模块里的
    timedelta 比较仍正常工作（timedelta 不动，只动 now）。
    """
    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return fake_now.astimezone(tz)
            return fake_now

    monkeypatch.setattr(alert_module, "datetime", _FakeDateTime)


def _make_service(
    sqlite_db: _SqliteDatabase,
    hotness_repo: _SqliteHotnessSnapshotsRepo,
    *,
    telegram_client: MagicMock | None = None,
    growth_threshold: float = 20.0,
    min_count_short: int = 3,
    min_cross_source: int = 1,
    cooldown_minutes: int = 60,
    escalation_growth_multiplier: float = 1.5,
    heartbeat_hours: int = 6,
) -> AlertTriggerService:
    if telegram_client is None:
        telegram_client = MagicMock()
        telegram_client.send_text.return_value = True
    return AlertTriggerService(
        db=sqlite_db,
        hotness_repo=hotness_repo,
        telegram_client=telegram_client,
        growth_threshold=growth_threshold,
        min_count_short=min_count_short,
        min_cross_source=min_cross_source,
        cooldown_minutes=cooldown_minutes,
        escalation_growth_multiplier=escalation_growth_multiplier,
        heartbeat_hours=heartbeat_hours,
    )


# ===========================================================================
# 用例 1：首次告警 —— 触发条件全满足
# ===========================================================================


def test_first_alert_when_all_conditions_met(
    sqlite_db, hotness_repo, monkeypatch
) -> None:
    """
    Req 2.3 + 2.5：所有门槛满足 + 无历史告警记录 → 推送 [首次]。
    """
    _seed_records(
        sqlite_db,
        hotness_repo,
        records=[
            _make_record("BTC", growth_rate=25.3, count_short=10, cross_source=2)
        ],
    )
    _patch_now(monkeypatch, datetime(2026, 5, 14, 10, 5, 0, tzinfo=timezone.utc))

    svc = _make_service(sqlite_db, hotness_repo)
    assert svc.run_once() is True

    svc.telegram_client.send_text.assert_called_once()
    text = svc.telegram_client.send_text.call_args.args[0]
    assert "[首次]" in text
    assert "BTC" in text
    assert "25.3" in text

    # 推送成功后 _alert_records 已记录
    assert "BTC" in svc._alert_records
    rec = svc._alert_records["BTC"]
    assert rec.last_growth_rate == 25.3
    assert rec.last_cross_source == 2


# ===========================================================================
# 用例 2~4：三道门槛任一不满足 → 不告警
# ===========================================================================


def test_alert_skipped_when_growth_below_threshold(
    sqlite_db, hotness_repo, monkeypatch
) -> None:
    """growth_rate=15 < threshold=20 → 不告警。"""
    _seed_records(
        sqlite_db,
        hotness_repo,
        records=[_make_record("BTC", growth_rate=15.0)],
    )
    _patch_now(monkeypatch, datetime(2026, 5, 14, 10, 5, 0, tzinfo=timezone.utc))

    svc = _make_service(sqlite_db, hotness_repo)
    assert svc.run_once() is False
    svc.telegram_client.send_text.assert_not_called()


def test_alert_skipped_when_count_short_below_threshold(
    sqlite_db, hotness_repo, monkeypatch
) -> None:
    """count_short=2 < min=3 → 不告警（避免 1 次提及就告警的噪音）。"""
    _seed_records(
        sqlite_db,
        hotness_repo,
        records=[_make_record("BTC", count_short=2)],
    )
    _patch_now(monkeypatch, datetime(2026, 5, 14, 10, 5, 0, tzinfo=timezone.utc))

    svc = _make_service(sqlite_db, hotness_repo)
    assert svc.run_once() is False
    svc.telegram_client.send_text.assert_not_called()


def test_alert_skipped_when_cross_source_below_threshold(
    sqlite_db, hotness_repo, monkeypatch
) -> None:
    """min_cross_source=2 但记录 cross_source=1 → 不告警。"""
    _seed_records(
        sqlite_db,
        hotness_repo,
        records=[_make_record("BTC", cross_source=1)],
    )
    _patch_now(monkeypatch, datetime(2026, 5, 14, 10, 5, 0, tzinfo=timezone.utc))

    svc = _make_service(sqlite_db, hotness_repo, min_cross_source=2)
    assert svc.run_once() is False
    svc.telegram_client.send_text.assert_not_called()


# ===========================================================================
# 用例 5：常规冷却 —— 60min 内无质变不重发
# ===========================================================================


def test_no_alert_within_cooldown_without_escalation(
    sqlite_db, hotness_repo, monkeypatch
) -> None:
    """
    Req 2.4：60min 内 + growth/cross 都没变 → 不重发。
    """
    # 第一轮窗口
    _seed_records(
        sqlite_db,
        hotness_repo,
        window_end=datetime(2026, 5, 14, 10, 0, 0),
        records=[_make_record("BTC", growth_rate=25.0, cross_source=1)],
    )
    _patch_now(monkeypatch, datetime(2026, 5, 14, 10, 5, 0, tzinfo=timezone.utc))

    svc = _make_service(sqlite_db, hotness_repo)
    assert svc.run_once() is True
    assert svc.telegram_client.send_text.call_count == 1

    # 第二轮窗口（30 分钟后），growth/cross 没变
    _seed_records(
        sqlite_db,
        hotness_repo,
        window_end=datetime(2026, 5, 14, 10, 15, 0),
        records=[_make_record("BTC", growth_rate=25.0, cross_source=1)],
    )
    _patch_now(monkeypatch, datetime(2026, 5, 14, 10, 35, 0, tzinfo=timezone.utc))

    assert svc.run_once() is False
    # send_text 仍只调过 1 次（首轮的）
    assert svc.telegram_client.send_text.call_count == 1


# ===========================================================================
# 用例 6：growth 翻倍 → 升级告警（即便在冷却内）
# ===========================================================================


def test_growth_doubled_triggers_escalation(
    sqlite_db, hotness_repo, monkeypatch
) -> None:
    """
    Req 2.4 路径 3：growth ≥ 上次 × 1.5 → 立刻升级告警，标签含 "[升级]"。
    """
    # 第一轮：growth=20
    _seed_records(
        sqlite_db,
        hotness_repo,
        window_end=datetime(2026, 5, 14, 10, 0, 0),
        records=[_make_record("BTC", growth_rate=20.0, cross_source=1)],
    )
    _patch_now(monkeypatch, datetime(2026, 5, 14, 10, 5, 0, tzinfo=timezone.utc))

    svc = _make_service(sqlite_db, hotness_repo)
    assert svc.run_once() is True

    # 第二轮：30 分钟后（仍在 60min 冷却内），growth=40（×2.0）
    _seed_records(
        sqlite_db,
        hotness_repo,
        window_end=datetime(2026, 5, 14, 10, 15, 0),
        records=[_make_record("BTC", growth_rate=40.0, cross_source=1)],
    )
    _patch_now(monkeypatch, datetime(2026, 5, 14, 10, 35, 0, tzinfo=timezone.utc))

    assert svc.run_once() is True
    assert svc.telegram_client.send_text.call_count == 2

    # 第二条消息含 [升级]
    second_text = svc.telegram_client.send_text.call_args_list[1].args[0]
    assert "[升级" in second_text
    assert "×2.0" in second_text


# ===========================================================================
# 用例 7：cross_source 增加 → 升级告警（即便在冷却内）
# ===========================================================================


def test_cross_source_increase_triggers_escalation(
    sqlite_db, hotness_repo, monkeypatch
) -> None:
    """
    Req 2.4 路径 4：cross_source 增加 → 立刻升级告警，标签含 "[跨源升级]"。
    """
    # 第一轮：cross=1
    _seed_records(
        sqlite_db,
        hotness_repo,
        window_end=datetime(2026, 5, 14, 10, 0, 0),
        records=[_make_record("BTC", growth_rate=22.0, cross_source=1)],
    )
    _patch_now(monkeypatch, datetime(2026, 5, 14, 10, 5, 0, tzinfo=timezone.utc))

    svc = _make_service(sqlite_db, hotness_repo)
    assert svc.run_once() is True

    # 第二轮：30 分钟后（冷却内），cross_source=2，growth 未达升级倍数
    _seed_records(
        sqlite_db,
        hotness_repo,
        window_end=datetime(2026, 5, 14, 10, 15, 0),
        records=[_make_record("BTC", growth_rate=23.0, cross_source=2)],
    )
    _patch_now(monkeypatch, datetime(2026, 5, 14, 10, 35, 0, tzinfo=timezone.utc))

    assert svc.run_once() is True
    second_text = svc.telegram_client.send_text.call_args_list[1].args[0]
    assert "[跨源升级" in second_text
    assert "+1" in second_text


# ===========================================================================
# 用例 8：心跳 —— 6h 后无质变也告警
# ===========================================================================


def test_heartbeat_after_6h_without_escalation(
    sqlite_db, hotness_repo, monkeypatch
) -> None:
    """
    Req 2.4 路径 2：距上次告警 ≥ heartbeat_hours，即便 growth/cross 都没变
    也告警一次（标签 "[持续 Nh]"）。

    关键测试点：心跳判断必须在 growth 升级判断之前——否则 6h 后 growth 持平
    会落到"60min 内无质变"分支被错过（handoff §4.1）。
    """
    # 第一轮
    _seed_records(
        sqlite_db,
        hotness_repo,
        window_end=datetime(2026, 5, 14, 10, 0, 0),
        records=[_make_record("BTC", growth_rate=25.0, cross_source=1)],
    )
    _patch_now(monkeypatch, datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc))

    svc = _make_service(sqlite_db, hotness_repo)
    assert svc.run_once() is True

    # 第二轮：6 小时 5 分钟后，growth/cross 都没变
    _seed_records(
        sqlite_db,
        hotness_repo,
        window_end=datetime(2026, 5, 14, 16, 0, 0),
        records=[_make_record("BTC", growth_rate=25.0, cross_source=1)],
    )
    _patch_now(monkeypatch, datetime(2026, 5, 14, 16, 5, 0, tzinfo=timezone.utc))

    assert svc.run_once() is True
    assert svc.telegram_client.send_text.call_count == 2
    second_text = svc.telegram_client.send_text.call_args_list[1].args[0]
    assert "[持续" in second_text
    # 6h 心跳标签
    assert "6h" in second_text


# ===========================================================================
# 用例 9：cooldown 外 + 仍达阈值 → 重新触发
# ===========================================================================


def test_alert_after_cooldown_with_no_change(
    sqlite_db, hotness_repo, monkeypatch
) -> None:
    """
    Req 2.4 路径 6：60min 之外 + growth/cross 都没变（不构成升级）
    → 视为重新触发，标签 "[重新触发]"。
    """
    # 第一轮
    _seed_records(
        sqlite_db,
        hotness_repo,
        window_end=datetime(2026, 5, 14, 10, 0, 0),
        records=[_make_record("BTC", growth_rate=25.0, cross_source=1)],
    )
    _patch_now(monkeypatch, datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc))

    svc = _make_service(sqlite_db, hotness_repo)
    assert svc.run_once() is True

    # 第二轮：61 分钟后（出冷却但未到 6h 心跳），growth/cross 都没变
    _seed_records(
        sqlite_db,
        hotness_repo,
        window_end=datetime(2026, 5, 14, 11, 0, 0),
        records=[_make_record("BTC", growth_rate=25.0, cross_source=1)],
    )
    _patch_now(monkeypatch, datetime(2026, 5, 14, 11, 5, 0, tzinfo=timezone.utc))

    assert svc.run_once() is True
    second_text = svc.telegram_client.send_text.call_args_list[1].args[0]
    assert "[重新触发]" in second_text


# ===========================================================================
# 用例 10：同窗口重复 run_once → 跳过（不重复扫描）
# ===========================================================================


def test_skips_same_window_on_repeat_run(
    sqlite_db, hotness_repo, monkeypatch
) -> None:
    """
    Req 2.3：同一 window_end 在第二次 run_once 时直接跳过，
    send_text 不会被多次调用，repo.fetch_top_k 也不会被多调一次。
    """
    _seed_records(
        sqlite_db,
        hotness_repo,
        records=[_make_record("BTC", growth_rate=25.0, cross_source=1)],
    )
    _patch_now(monkeypatch, datetime(2026, 5, 14, 10, 5, 0, tzinfo=timezone.utc))

    svc = _make_service(sqlite_db, hotness_repo)
    assert svc.run_once() is True
    assert svc.telegram_client.send_text.call_count == 1

    # 第二次 run_once：window_end 没变 → 直接跳过
    assert svc.run_once() is False
    # send_text 仍只调用 1 次
    assert svc.telegram_client.send_text.call_count == 1


# ===========================================================================
# 用例 11：send_text 失败 → 不进冷却（下一轮可重试）
# ===========================================================================


def test_send_failure_does_not_update_alert_record(
    sqlite_db, hotness_repo, monkeypatch
) -> None:
    """
    Req 2.4 + 6.2：send_text 返回 False（Telegram 不可达）时，
    该 entity 不进入 _alert_records，下一轮新窗口下仍按"首次告警"处理。
    """
    # 第一轮：send_text 失败
    failing_client = MagicMock()
    failing_client.send_text.return_value = False

    _seed_records(
        sqlite_db,
        hotness_repo,
        window_end=datetime(2026, 5, 14, 10, 0, 0),
        records=[_make_record("BTC", growth_rate=25.0, cross_source=1)],
    )
    _patch_now(monkeypatch, datetime(2026, 5, 14, 10, 5, 0, tzinfo=timezone.utc))

    svc = _make_service(sqlite_db, hotness_repo, telegram_client=failing_client)
    # 推送失败 → run_once 返回 False（无 sent>0）
    assert svc.run_once() is False
    # send_text 被调用，但 _alert_records 未更新
    failing_client.send_text.assert_called_once()
    assert "BTC" not in svc._alert_records

    # 第二轮新窗口：Telegram 恢复，send_text 成功
    failing_client.send_text.return_value = True
    _seed_records(
        sqlite_db,
        hotness_repo,
        window_end=datetime(2026, 5, 14, 10, 15, 0),
        records=[_make_record("BTC", growth_rate=25.0, cross_source=1)],
    )
    _patch_now(monkeypatch, datetime(2026, 5, 14, 10, 20, 0, tzinfo=timezone.utc))

    assert svc.run_once() is True
    # 这次 _alert_records 应该有 BTC
    assert "BTC" in svc._alert_records
    # 第二条消息仍是 [首次]（因为第一次失败没记录）
    second_text = failing_client.send_text.call_args_list[1].args[0]
    assert "[首次]" in second_text
