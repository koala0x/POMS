from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from services.level2_service import Level2Service


@contextmanager
def _conn_ctx(conn: Mock):
    yield conn


@dataclass(frozen=True)
class L1:
    id: int
    summary: str


def test_level2_skip_when_no_data(tmp_path: Path) -> None:
    prompt_path = tmp_path / "p.txt"
    prompt_path.write_text("{items}", encoding="utf-8")

    conn = Mock()
    db = Mock()
    db.get_conn.return_value = _conn_ctx(conn)

    level1_repo = Mock()
    level1_repo.fetch_unsummarized_for_period.return_value = []

    level2_repo = Mock()
    ollama = Mock()

    svc = Level2Service(
        db=db,
        source="twitter",
        level1_repo=level1_repo,
        level2_repo=level2_repo,
        ollama=ollama,
        prompt_path=prompt_path,
        timezone=ZoneInfo("UTC"),
    )
    svc.run_hourly()

    ollama.chat.assert_not_called()
    level2_repo.insert.assert_not_called()


def test_level2_happy_path(tmp_path: Path) -> None:
    prompt_path = tmp_path / "p.txt"
    prompt_path.write_text("X\n{items}", encoding="utf-8")

    conn1 = Mock()
    conn2 = Mock()
    db = Mock()
    db.get_conn.side_effect = [_conn_ctx(conn1), _conn_ctx(conn2)]

    level1_repo = Mock()
    level1_repo.fetch_unsummarized_for_period.return_value = [L1(1, "a"), L1(2, "b")]
    level1_repo.mark_summarized_l2.return_value = 2

    level2_repo = Mock()
    level2_repo.insert.return_value = 99

    ollama = Mock()
    ollama.chat.return_value = "l2"

    svc = Level2Service(
        db=db,
        source="twitter",
        level1_repo=level1_repo,
        level2_repo=level2_repo,
        ollama=ollama,
        prompt_path=prompt_path,
        timezone=ZoneInfo("UTC"),
    )
    svc.run_hourly()

    ollama.chat.assert_called_once()
    level2_repo.insert.assert_called_once()
    level1_repo.mark_summarized_l2.assert_called_once()
