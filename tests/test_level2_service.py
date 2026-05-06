from __future__ import annotations

"""
Level2Service 的单元测试。

测试策略:
- 用 Mock 替代 DB/Repo/Ollama,验证"阈值触发的二次摘要"流程编排:
  - 未达阈值:不调用 LLM、不写 level2(run_once 返回 False)
  - 达到阈值:拼 prompt -> 调 LLM -> 写 level2 -> 标记 level1(run_once 返回 True)
  - LLM 失败:跳过写库与标记(run_once 返回 False)
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from services.level2_service import Level2Service


@contextmanager
def _session_ctx(session: Mock):
    # Database.get_session() 是 contextmanager,这里用同样的形式模拟。
    yield session


@dataclass(frozen=True)
class L1:
    # 用于模拟 level1_repo.fetch_oldest_unsummarized_l2 的返回对象。
    id: int
    summary: str
    created_at: datetime


def _ts(minute: int) -> datetime:
    """生成一个稳定的、带 tz 的时间戳,便于 min/max 测试。"""
    return datetime(2026, 1, 1, 12, minute, 0, tzinfo=timezone.utc)


def test_level2_skip_when_below_threshold(tmp_path: Path) -> None:
    """未达阈值时应跳过 fetch/LLM/写库。"""
    prompt_path = tmp_path / "p.txt"
    prompt_path.write_text("{items}", encoding="utf-8")

    session = Mock()
    db = Mock()
    db.get_session.return_value = _session_ctx(session)

    level1_repo = Mock()
    level1_repo.count_unsummarized_l2.return_value = 4  # 阈值 5,差一条

    level2_repo = Mock()
    ollama = Mock()

    svc = Level2Service(
        db=db,
        source="twitter",
        threshold=5,
        level1_repo=level1_repo,
        level2_repo=level2_repo,
        ollama=ollama,
        prompt_path=prompt_path,
        timezone=ZoneInfo("UTC"),
    )
    assert svc.run_once() is False

    # 关键断言:计数后立即返回,不进入 fetch / LLM / 写库分支。
    level1_repo.fetch_oldest_unsummarized_l2.assert_not_called()
    ollama.chat.assert_not_called()
    level2_repo.insert.assert_not_called()


def test_level2_happy_path(tmp_path: Path) -> None:
    """达到阈值时:fetch -> LLM -> 写库 -> 标记 全部触发,run_once 返回 True。"""
    prompt_path = tmp_path / "p.txt"
    prompt_path.write_text("X\n{items}", encoding="utf-8")

    # run_once 内部依次获取三次 Session:计数、读取、写入/标记。
    session1, session2, session3 = Mock(), Mock(), Mock()
    db = Mock()
    db.get_session.side_effect = [
        _session_ctx(session1),
        _session_ctx(session2),
        _session_ctx(session3),
    ]

    level1_repo = Mock()
    level1_repo.count_unsummarized_l2.return_value = 5
    rows = [
        L1(1, "a", _ts(1)),
        L1(2, "b", _ts(3)),
        L1(3, "c", _ts(2)),
        L1(4, "d", _ts(5)),
        L1(5, "e", _ts(4)),
    ]
    level1_repo.fetch_oldest_unsummarized_l2.return_value = rows
    level1_repo.mark_summarized_l2.return_value = 5

    level2_repo = Mock()
    level2_repo.insert.return_value = 99

    ollama = Mock()
    ollama.chat.return_value = "l2 summary"

    svc = Level2Service(
        db=db,
        source="twitter",
        threshold=5,
        level1_repo=level1_repo,
        level2_repo=level2_repo,
        ollama=ollama,
        prompt_path=prompt_path,
        timezone=ZoneInfo("UTC"),
    )
    assert svc.run_once() is True

    # 关键断言:LLM 调用、写库、标记都发生一次。
    ollama.chat.assert_called_once()
    level2_repo.insert.assert_called_once()
    level1_repo.mark_summarized_l2.assert_called_once()
    # 写库 Session 上应当 commit 了一次。
    session3.commit.assert_called_once()

    # period_start/end 应来自本批 created_at 的 min/max。
    insert_kwargs = level2_repo.insert.call_args.kwargs
    assert insert_kwargs["period_start"] == _ts(1)
    assert insert_kwargs["period_end"] == _ts(5)
    assert insert_kwargs["level1_count"] == 5


def test_level2_llm_failure_skips_db_writes(tmp_path: Path) -> None:
    """LLM 失败时应跳过写库与标记,run_once 返回 False。"""
    prompt_path = tmp_path / "p.txt"
    prompt_path.write_text("{items}", encoding="utf-8")

    session1, session2 = Mock(), Mock()
    db = Mock()
    db.get_session.side_effect = [_session_ctx(session1), _session_ctx(session2)]

    level1_repo = Mock()
    level1_repo.count_unsummarized_l2.return_value = 5
    level1_repo.fetch_oldest_unsummarized_l2.return_value = [
        L1(i, f"s{i}", _ts(i)) for i in range(1, 6)
    ]

    level2_repo = Mock()
    ollama = Mock()
    ollama.chat.side_effect = RuntimeError("llm down")

    svc = Level2Service(
        db=db,
        source="twitter",
        threshold=5,
        level1_repo=level1_repo,
        level2_repo=level2_repo,
        ollama=ollama,
        prompt_path=prompt_path,
        timezone=ZoneInfo("UTC"),
    )
    assert svc.run_once() is False

    level2_repo.insert.assert_not_called()
    level1_repo.mark_summarized_l2.assert_not_called()
