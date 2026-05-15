from __future__ import annotations

"""
DigestPusherService 单元 + 集成测试（Phase 2.8 新增）。

测试矩阵（6 个用例，覆盖核心行为）：
 1. test_alignment_skips_off_quarter
    push_every_quarters=4 时只在每小时 :00 推送，:15/:30/:45 跳过
 2. test_same_window_not_pushed_twice
    同一 window_end 第二次 run_once 直接跳过
 3. test_renders_three_window_sections
    1h/6h/24h 三窗口都有数据时，消息里包含三段 + 各窗口的 entity 名
 4. test_renders_empty_window_gracefully
    某窗口 hotness_snapshots 为空时，渲染"（暂未生成）"占位符不报错
 5. test_send_failure_keeps_state_for_retry
    Telegram 推送失败时不更新 _last_pushed_window_end，下次 run_once 还能再试
 6. test_send_uses_markdown_parse_mode
    推送时 parse_mode='Markdown'，便于 Telegram 渲染加粗 / code 块
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import Base, HotnessSnapshot
from db.repositories.hotness_snapshots_repo import HotnessSnapshotsRepo
from services.l2_digest_pusher import DigestPusherService


# ===========================================================================
# Fixtures
# ===========================================================================


@dataclass
class _SqliteDB:
    factory: sessionmaker[Session]

    @contextmanager
    def get_session(self) -> Iterator[Session]:
        s = self.factory()
        try:
            yield s
        finally:
            s.close()


@pytest.fixture()
def sqlite_db() -> _SqliteDB:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine, tables=[HotnessSnapshot.__table__])
    return _SqliteDB(factory=sessionmaker(bind=engine, expire_on_commit=False, future=True))


def _seed_snapshots(
    db: _SqliteDB,
    *,
    window_end: datetime,
    window_type: str,
    entities: list[tuple[str, float, int, int]],  # (entity, growth, count_short, rank)
    id_offset: int = 0,
) -> None:
    """种 N 条 hotness_snapshots 记录到 SQLite。"""
    with db.get_session() as s:
        for i, (entity, growth, count, rank) in enumerate(entities):
            s.add(
                HotnessSnapshot(
                    id=id_offset + i + 1,
                    window_end=window_end,
                    window_type=window_type,
                    entity=entity,
                    entity_type="ticker",
                    count_short=count,
                    count_baseline=2.0,
                    growth_rate=growth,
                    cross_source=2,
                    final_score=growth,
                    rank=rank,
                    is_new_entity=False,
                )
            )
        s.commit()


def _make_service(
    db: _SqliteDB,
    *,
    push_every_quarters: int = 4,
    top_n: int = 10,
    window_types: tuple[str, ...] = ("1h", "6h", "24h"),
    send_ok: bool = True,
):
    tg = MagicMock()
    tg.send_text.return_value = send_ok
    return DigestPusherService(
        db=db,
        hotness_repo=HotnessSnapshotsRepo(),
        telegram_client=tg,
        window_types=window_types,
        top_n=top_n,
        push_every_quarters=push_every_quarters,
        timezone=ZoneInfo("UTC"),
    )


# ===========================================================================
# 用例 1：对齐
# ===========================================================================


def test_alignment_skips_off_quarter(sqlite_db, monkeypatch) -> None:
    """
    push_every_quarters=4 时：只在 :00 推送，:15/:30/:45 都跳过。
    """
    svc = _make_service(sqlite_db, push_every_quarters=4)

    # 模拟 datetime.now 返回 10:23:00（align 到 10:15）→ 应跳过
    fake_now = datetime(2026, 5, 14, 10, 23, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "services.l2_digest_pusher.datetime",
        _FrozenDatetime(fake_now),
    )
    assert svc.run_once() is False
    svc.telegram_client.send_text.assert_not_called()


# ===========================================================================
# 用例 2：同窗口去重
# ===========================================================================


def test_same_window_not_pushed_twice(sqlite_db, monkeypatch) -> None:
    """
    同一 window_end 第二次 run_once 直接跳过，send_text 只被调用一次。
    """
    svc = _make_service(sqlite_db, push_every_quarters=4)
    fake_now = datetime(2026, 5, 14, 10, 5, 0, tzinfo=timezone.utc)  # align → 10:00
    monkeypatch.setattr(
        "services.l2_digest_pusher.datetime",
        _FrozenDatetime(fake_now),
    )

    # 起码种一条 1h 数据，让 fetch_top_k 不空
    _seed_snapshots(
        sqlite_db,
        window_end=datetime(2026, 5, 14, 10, 0, 0),
        window_type="1h",
        entities=[("BTC", 5.0, 10, 1)],
    )

    assert svc.run_once() is True
    assert svc.run_once() is False
    assert svc.telegram_client.send_text.call_count == 1


# ===========================================================================
# 用例 3：三窗口渲染
# ===========================================================================


def test_renders_three_window_sections(sqlite_db, monkeypatch) -> None:
    """
    1h / 6h / 24h 都有数据时，消息正文应包含三段 + 各窗口的 entity 名。
    """
    we = datetime(2026, 5, 14, 10, 0, 0)
    _seed_snapshots(
        sqlite_db, window_end=we, window_type="1h",
        entities=[("AAA", 5.0, 10, 1)], id_offset=0,
    )
    _seed_snapshots(
        sqlite_db, window_end=we, window_type="6h",
        entities=[("BBB", 4.0, 20, 1)], id_offset=10,
    )
    _seed_snapshots(
        sqlite_db, window_end=we, window_type="24h",
        entities=[("CCC", 3.0, 50, 1)], id_offset=20,
    )

    svc = _make_service(sqlite_db, push_every_quarters=4)
    fake_now = datetime(2026, 5, 14, 10, 5, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "services.l2_digest_pusher.datetime", _FrozenDatetime(fake_now),
    )

    assert svc.run_once() is True
    body = svc.telegram_client.send_text.call_args[0][0]
    # 三个窗口的 header 都在
    assert "1h 榜" in body
    assert "6h 榜" in body
    assert "24h 榜" in body
    # 三个 entity 都在
    assert "`AAA`" in body
    assert "`BBB`" in body
    assert "`CCC`" in body


# ===========================================================================
# 用例 4：空窗口降级
# ===========================================================================


def test_renders_empty_window_gracefully(sqlite_db, monkeypatch) -> None:
    """
    24h 窗口没数据时，对应 section 渲染"（暂未生成）"占位，不报错。
    """
    we = datetime(2026, 5, 14, 10, 0, 0)
    _seed_snapshots(
        sqlite_db, window_end=we, window_type="1h",
        entities=[("AAA", 5.0, 10, 1)],
    )
    # 6h / 24h 都不种 → fetch_latest_window_end 返回 None

    svc = _make_service(sqlite_db, push_every_quarters=4)
    fake_now = datetime(2026, 5, 14, 10, 5, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "services.l2_digest_pusher.datetime", _FrozenDatetime(fake_now),
    )

    assert svc.run_once() is True
    body = svc.telegram_client.send_text.call_args[0][0]
    assert "暂未生成" in body
    assert "`AAA`" in body  # 1h 窗口正常渲染


# ===========================================================================
# 用例 5：send 失败保留状态
# ===========================================================================


def test_send_failure_keeps_state_for_retry(sqlite_db, monkeypatch) -> None:
    """
    Telegram 推送失败时不应更新 _last_pushed_window_end，
    下次 run_once（同一对齐时刻）仍会重试。
    """
    we = datetime(2026, 5, 14, 10, 0, 0)
    _seed_snapshots(
        sqlite_db, window_end=we, window_type="1h",
        entities=[("AAA", 5.0, 10, 1)],
    )

    svc = _make_service(sqlite_db, push_every_quarters=4, send_ok=False)
    fake_now = datetime(2026, 5, 14, 10, 5, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "services.l2_digest_pusher.datetime", _FrozenDatetime(fake_now),
    )

    assert svc.run_once() is False
    assert svc._last_pushed_window_end is None  # 状态未更新

    # 第二次同 window_end 仍触发尝试（因为状态没更新）
    svc.telegram_client.send_text.return_value = True
    assert svc.run_once() is True
    assert svc.telegram_client.send_text.call_count == 2


# ===========================================================================
# 用例 6：parse_mode 是 Markdown
# ===========================================================================


def test_send_uses_markdown_parse_mode(sqlite_db, monkeypatch) -> None:
    """
    推送时 parse_mode='Markdown'，让 Telegram 渲染加粗 / code 块。
    """
    we = datetime(2026, 5, 14, 10, 0, 0)
    _seed_snapshots(
        sqlite_db, window_end=we, window_type="1h",
        entities=[("AAA", 5.0, 10, 1)],
    )

    svc = _make_service(sqlite_db, push_every_quarters=4)
    fake_now = datetime(2026, 5, 14, 10, 5, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "services.l2_digest_pusher.datetime", _FrozenDatetime(fake_now),
    )

    assert svc.run_once() is True
    _, kwargs = svc.telegram_client.send_text.call_args
    assert kwargs.get("parse_mode") == "Markdown"


# ===========================================================================
# 工具：冻结 datetime.now（避免对真实墙钟敏感）
# ===========================================================================


class _FrozenDatetime:
    """让 datetime.now(tz) 返回固定时间；其它 datetime API 透传给真 datetime。"""

    def __init__(self, fixed: datetime) -> None:
        self._fixed = fixed

    def now(self, tz=None):  # noqa: D401
        if tz is None:
            return self._fixed.replace(tzinfo=None)
        return self._fixed.astimezone(tz)

    def __getattr__(self, item):
        from datetime import datetime as _real_dt
        return getattr(_real_dt, item)
