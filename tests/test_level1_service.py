from __future__ import annotations

"""
Level1Service 的单元测试。

测试策略：
- 不依赖真实 DB / Ollama，通过 Mock 依赖验证“流程编排”是否正确：
  - 未达到 batch_size 时应直接跳过，不拉取数据、不调用 LLM、不写库
  - 达到 batch_size 时应按顺序拉取 -> 调 LLM -> 写 level1 -> 标记原始表
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from services.level1_service import Level1Service


@contextmanager
def _conn_ctx(conn: Mock):
    # Database.get_conn() 是 contextmanager，这里用同样的形式模拟。
    yield conn


@dataclass(frozen=True)
class Post:
    # 用于模拟 raw_repo.fetch_oldest_unsummarized 返回的对象。
    id: int
    content: str
    author: str | None = None
    posted_at: datetime | None = None
    created_at: datetime | None = None


def test_level1_skip_when_not_enough(tmp_path: Path) -> None:
    # 准备最小 prompt 模板。
    prompt_path = tmp_path / "p.txt"
    prompt_path.write_text("{items}", encoding="utf-8")

    # db.get_conn 返回一个可用的“连接上下文”。
    conn = Mock()
    db = Mock()
    db.get_conn.return_value = _conn_ctx(conn)

    # 未处理数量不足 batch_size 时应直接返回。
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

    # 关键断言：不会进入后续步骤。
    raw_repo.fetch_oldest_unsummarized.assert_not_called()
    ollama.chat.assert_not_called()
    level1_repo.insert.assert_not_called()


def test_level1_happy_path(tmp_path: Path) -> None:
    # 准备带前缀的模板，确保 format 占位符正常工作。
    prompt_path = tmp_path / "p.txt"
    prompt_path.write_text("X\n{items}", encoding="utf-8")

    # run_once 内部会多次获取连接：统计、拉取、写入/标记。
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

    # 关键断言：LLM 调用、写库、标记都发生一次。
    ollama.chat.assert_called_once()
    level1_repo.insert.assert_called_once()
    raw_repo.mark_summarized.assert_called_once()
