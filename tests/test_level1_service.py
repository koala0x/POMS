from __future__ import annotations

"""
Level1Service 的单元测试。

测试策略:
- 不依赖真实 DB / Ollama,通过 Mock 依赖验证"流程编排"是否正确:
  - 未达到 batch_size 时应直接跳过,不拉取数据、不调用 LLM、不写库
  - 达到 batch_size 且预过滤后有保留:按顺序拉取 -> 调 LLM -> 写 level1 -> 标记原始表
  - 达到 batch_size 但整批都是噪音:跳过 LLM,但仍标记全部 raw 为已处理(避免无限重拉)
  - LLM 失败:跳过写库与标记,等待下一轮重试

预过滤接入后,fixture 内容必须能通过 services.prefilter.classify(否则会走"整批被过滤"分支)。
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from services.level1_service import Level1Service


@contextmanager
def _session_ctx(session: Mock):
    # Database.get_session() 是 contextmanager,这里用同样的形式模拟。
    yield session


@dataclass(frozen=True)
class Post:
    # 用于模拟 raw_repo.fetch_oldest_unsummarized 返回的对象。
    # 服务层用 getattr 鸭子类型读取,无需是真实 ORM 实例。
    id: int
    content: str
    author: str | None = None
    posted_at: datetime | None = None
    created_at: datetime | None = None


def _signal_post(i: int) -> Post:
    """生成一条能通过 prefilter A 规则的真实风格 fixture。"""
    return Post(id=i, content=f"$BTC 突破 ${70000 + i},机构 ETF 持仓 +{i}%", author="a")


def _noise_post(i: int) -> Post:
    """生成一条会被 prefilter D 规则丢弃的纯噪音 fixture(< 20 字、无 $ 无币名)。"""
    return Post(id=i, content="梭哈了", author="b")


def test_level1_skip_when_not_enough(tmp_path: Path) -> None:
    # 准备最小 prompt 模板。
    prompt_path = tmp_path / "p.txt"
    prompt_path.write_text("{items}", encoding="utf-8")

    # db.get_session 返回一个可用的"会话上下文"。
    session = Mock()
    db = Mock()
    db.get_session.return_value = _session_ctx(session)

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

    # 关键断言:不会进入后续步骤。
    raw_repo.fetch_oldest_unsummarized.assert_not_called()
    ollama.chat.assert_not_called()
    level1_repo.insert.assert_not_called()


def test_level1_happy_path(tmp_path: Path) -> None:
    """全部能通过 prefilter:LLM 调用、写库、标记都应发生。"""
    prompt_path = tmp_path / "p.txt"
    prompt_path.write_text("X\n{items}", encoding="utf-8")

    # run_once 内部多次获取 Session:统计、拉取、写入/标记。
    session1, session2, session3 = Mock(), Mock(), Mock()
    db = Mock()
    db.get_session.side_effect = [
        _session_ctx(session1),
        _session_ctx(session2),
        _session_ctx(session3),
    ]

    raw_repo = Mock()
    raw_repo.count_unsummarized.return_value = 50
    raw_repo.fetch_oldest_unsummarized.return_value = [_signal_post(i) for i in range(1, 51)]
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
    assert svc.run_once() is True

    # 关键断言:LLM 调用、写库、标记都发生一次。
    ollama.chat.assert_called_once()
    level1_repo.insert.assert_called_once()
    raw_repo.mark_summarized.assert_called_once()
    # 写库 Session 上应当 commit 了一次。
    session3.commit.assert_called_once()
    # raw_count 应等于 kept 数量(此处全部通过)。
    insert_kwargs = level1_repo.insert.call_args.kwargs
    assert insert_kwargs["raw_count"] == 50
    assert len(insert_kwargs["raw_ids"]) == 50


def test_level1_all_filtered_marks_but_skips_llm(tmp_path: Path) -> None:
    """整批都被 prefilter 丢弃:不调 LLM,不写 level1,但标记全部 raw 已处理,返回 True。"""
    prompt_path = tmp_path / "p.txt"
    prompt_path.write_text("{items}", encoding="utf-8")

    session1, session2, session3 = Mock(), Mock(), Mock()
    db = Mock()
    db.get_session.side_effect = [
        _session_ctx(session1),
        _session_ctx(session2),
        _session_ctx(session3),
    ]

    raw_repo = Mock()
    raw_repo.count_unsummarized.return_value = 50
    raw_repo.fetch_oldest_unsummarized.return_value = [_noise_post(i) for i in range(1, 51)]
    raw_repo.mark_summarized.return_value = 50

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
    assert svc.run_once() is True

    # 不调 LLM、不写 level1
    ollama.chat.assert_not_called()
    level1_repo.insert.assert_not_called()
    # 但全部 raw 应被标记为已处理(否则下次会无限重拉)
    raw_repo.mark_summarized.assert_called_once()
    marked_ids = raw_repo.mark_summarized.call_args.args[1]
    assert sorted(marked_ids) == list(range(1, 51))
    session3.commit.assert_called_once()


def test_level1_llm_failure_skips_db_writes(tmp_path: Path) -> None:
    """LLM 失败时应跳过写库与标记,保证下次轮询可重试。"""
    prompt_path = tmp_path / "p.txt"
    prompt_path.write_text("{items}", encoding="utf-8")

    session1, session2 = Mock(), Mock()
    db = Mock()
    db.get_session.side_effect = [_session_ctx(session1), _session_ctx(session2)]

    raw_repo = Mock()
    raw_repo.count_unsummarized.return_value = 50
    raw_repo.fetch_oldest_unsummarized.return_value = [_signal_post(i) for i in range(1, 51)]
    level1_repo = Mock()

    ollama = Mock()
    ollama.chat.side_effect = RuntimeError("llm down")

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
    assert svc.run_once() is False

    level1_repo.insert.assert_not_called()
    raw_repo.mark_summarized.assert_not_called()
