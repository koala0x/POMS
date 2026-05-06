from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from services.level1_service import Level1Service


@contextmanager
def _conn_ctx(conn: Mock):
    yield conn


@dataclass(frozen=True)
class Post:
    id: int
    content: str
    author: str | None = None
    posted_at: datetime | None = None
    created_at: datetime | None = None


def test_level1_skip_when_not_enough(tmp_path: Path) -> None:
    prompt_path = tmp_path / "p.txt"
    prompt_path.write_text("{items}", encoding="utf-8")

    conn = Mock()
    db = Mock()
    db.get_conn.return_value = _conn_ctx(conn)

    raw_repo = Mock()
    raw_repo.count_unsummarized.return_value = 10

    level1_repo = Mock()
    ollama = Mock()

    svc = Level1Service(
        db=db,
        source="twitter",
        batch_size=50,
        raw_repo=raw_repo,
        level1_repo=level1_repo,
        ollama=ollama,
        prompt_path=prompt_path,
        timezone=ZoneInfo("UTC"),
    )
    svc.run_once()

    raw_repo.fetch_oldest_unsummarized.assert_not_called()
    ollama.chat.assert_not_called()
    level1_repo.insert.assert_not_called()


def test_level1_happy_path(tmp_path: Path) -> None:
    prompt_path = tmp_path / "p.txt"
    prompt_path.write_text("X\n{items}", encoding="utf-8")

    conn1 = Mock()
    conn2 = Mock()
    db = Mock()
    db.get_conn.side_effect = [_conn_ctx(conn1), _conn_ctx(conn1), _conn_ctx(conn2)]

    raw_repo = Mock()
    raw_repo.count_unsummarized.return_value = 50
    raw_repo.fetch_oldest_unsummarized.return_value = [
        Post(id=i, content=f"c{i}", author="a") for i in range(1, 51)
    ]
    raw_repo.mark_summarized.return_value = 50

    level1_repo = Mock()
    level1_repo.insert.return_value = 123

    ollama = Mock()
    ollama.chat.return_value = "summary"

    svc = Level1Service(
        db=db,
        source="twitter",
        batch_size=50,
        raw_repo=raw_repo,
        level1_repo=level1_repo,
        ollama=ollama,
        prompt_path=prompt_path,
        timezone=ZoneInfo("UTC"),
    )
    svc.run_once()

    ollama.chat.assert_called_once()
    level1_repo.insert.assert_called_once()
    raw_repo.mark_summarized.assert_called_once()
