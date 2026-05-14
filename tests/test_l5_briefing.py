from __future__ import annotations

"""
BriefingService 单元 + 集成测试（Phase 2.7 Task 3.6）。

测试矩阵（10 个用例，对应 spec Task 3.6）：
 1. test_select_evidence_top_n
 2. test_select_evidence_falls_back_to_random_when_no_engagement
 3. test_render_prompt_replaces_placeholders
 4. test_parse_json_valid
 5. test_parse_json_invalid_raises
 6. test_skips_when_no_top_entities
 7. test_skips_already_briefed_entities
 8. test_per_entity_failure_isolated（一个 entity 失败不影响其它）
 9. test_skips_when_window_unchanged
10. test_low_growth_filtered_out

★ Req 9.4 硬要求：所有测试必须 mock LLM，禁止真的调 Ollama。
★ 测试统一用 SQLite in-memory + 真实 NormalizedMessagesRepo / EntityMentionsRepo
  的少量直接 ORM insert，避免上重 fixture 的同时保留真实查询路径。
"""

import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from db.models import (
    Base,
    EntityBriefing,
    EntityCooccurrence,
    EntityMention,
    HotnessSnapshot,
    NormalizedMessage,
)
from services.l5_briefing import BriefingService


# ===========================================================================
# Fixtures：SQLite in-memory + helper inject
# ===========================================================================


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
    """SQLite in-memory + 全建表 + 不带连接池冲突。"""
    from sqlalchemy import text
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    # 除 entity_briefings 外的表用 ORM metadata 自动建（PG ARRAY/JSONB 在
    # SQLite 不支持，下面单独用 DDL 建 entity_briefings）
    Base.metadata.create_all(
        engine,
        tables=[
            NormalizedMessage.__table__,
            EntityMention.__table__,
            HotnessSnapshot.__table__,
            EntityCooccurrence.__table__,
        ],
    )
    # 手动建 entity_briefings：ARRAY → TEXT（存 JSON 字符串）/ JSONB → TEXT
    # 测试只验证 ORM 字段读写一致性，类型用 TEXT 兜底就够；ORM 在 SQLite 下
    # 把 list/dict 当 Python 对象塞进 TEXT 列，sqlite 驱动会做 str() 转换，
    # 我们读出来时 evidence_msg_ids 会是 str，但本测试不验证它的内容（只验 rowcount/存在性）
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE entity_briefings (
                    id INTEGER PRIMARY KEY,
                    entity VARCHAR(128) NOT NULL,
                    window_end DATETIME NOT NULL,
                    narrative TEXT,
                    catalyst TEXT,
                    fund_logic TEXT,
                    sentiment VARCHAR(16),
                    confidence FLOAT,
                    evidence_msg_ids TEXT NOT NULL,
                    raw_response TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (entity, window_end)
                )
                """
            )
        )
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return _SqliteDatabase(session_factory=factory)


def _insert_msg_with_mention(
    sqlite_db: _SqliteDatabase,
    *,
    msg_id: int,
    entity: str,
    text: str,
    ts: datetime,
    engagement: int = 0,
    raw_source: str = "twitter",
) -> None:
    """简单封装：插一条 normalized_messages + 一条 entity_mentions（同 ts）。"""
    with sqlite_db.get_session() as s:
        s.add(
            NormalizedMessage(
                id=msg_id,
                raw_source=raw_source,
                raw_id=msg_id,
                text=text,
                author=f"user{msg_id}",
                ts=ts,
                engagement=engagement,
                is_duplicate=False,
            )
        )
        s.add(
            EntityMention(
                id=msg_id,  # 与 msg_id 同步避免 SQLite autoincrement 问题
                msg_id=msg_id,
                entity=entity,
                entity_type="ticker",
                raw_source=raw_source,
                ts=ts,
                engagement=engagement,
            )
        )
        s.commit()


def _insert_hotness_top(
    sqlite_db: _SqliteDatabase,
    *,
    window_end: datetime,
    entries: list[tuple[str, float, int]],  # (entity, growth_rate, rank)
) -> None:
    """插入 1h 榜的 Top-N 记录。"""
    # SQLite BigInteger 主键不会自增（PG BIGSERIAL 在 SQLite 下不生效），
    # 必须显式分配 id 才能 INSERT。用 enumerate + 偏移避免与其它 fixture 冲突
    with sqlite_db.get_session() as s:
        for i, (entity, growth, rank) in enumerate(entries, start=1):
            s.add(
                HotnessSnapshot(
                    id=10000 + i,  # 偏移避免与 entity_briefings.id 冲突
                    window_end=window_end,
                    window_type="1h",
                    entity=entity,
                    count_short=10,
                    count_baseline=1.0,
                    growth_rate=growth,
                    cross_source=2,
                    final_score=growth,
                    rank=rank,
                    is_new_entity=False,
                )
            )
        s.commit()


@pytest.fixture()
def prompt_path(tmp_path: Path) -> Path:
    """临时 prompt 模板，带四个占位符。"""
    p = tmp_path / "level5_briefing.txt"
    p.write_text(
        "实体: {entity}\n"
        "条数: {n_msgs}\n"
        "共现: {cooccur_hint}\n"
        "消息:\n{messages}\n",
        encoding="utf-8",
    )
    return p


def _make_service(
    sqlite_db: _SqliteDatabase,
    prompt_path: Path,
    *,
    ollama_response: str | Exception | None = None,
    cooccur_repo=None,
    **overrides,
):
    """构造 BriefingService（注入真实 SQLite + mock OllamaClient）。"""
    from db.repositories.briefings_repo import BriefingsRepo
    from db.repositories.entity_mentions_repo import EntityMentionsRepo
    from db.repositories.hotness_snapshots_repo import HotnessSnapshotsRepo
    from db.repositories.normalized_messages_repo import NormalizedMessagesRepo

    ollama = MagicMock()
    if isinstance(ollama_response, Exception):
        ollama.chat.side_effect = ollama_response
    elif ollama_response is not None:
        ollama.chat.return_value = ollama_response

    return BriefingService(
        db=sqlite_db,
        hotness_repo=_SqliteFriendlyHotnessRepo(),  # 真实 repo（SQLite 兼容）
        mentions_repo=EntityMentionsRepo(),
        normalized_repo=NormalizedMessagesRepo(),
        briefing_repo=_SqliteFriendlyBriefingRepo(),  # 改 ON CONFLICT 为先 SELECT
        ollama=ollama,
        prompt_path=prompt_path,
        cooccur_repo=cooccur_repo,
        top_n=overrides.get("top_n", 5),
        min_growth=overrides.get("min_growth", 30.0),
        evidence_count=overrides.get("evidence_count", 10),
        timezone=overrides.get("timezone", ZoneInfo("UTC")),
    )


# ===========================================================================
# SQLite 兼容版 repo（沿用 hotness/cooccur 测试同款做法）
# ===========================================================================


class _SqliteFriendlyHotnessRepo:
    """SQLite 不支持 PG 的 ON CONFLICT；本测试只用 fetch 接口，不写。"""

    def fetch_latest_window_end(self, session, window_type: str = "1h"):
        from sqlalchemy import select

        stmt = (
            select(HotnessSnapshot.window_end)
            .where(HotnessSnapshot.window_type == window_type)
            .order_by(HotnessSnapshot.window_end.desc())
            .limit(1)
        )
        return session.scalar(stmt)

    def fetch_top_k(self, session, *, window_end, window_type, k=20):
        from sqlalchemy import select

        stmt = (
            select(HotnessSnapshot)
            .where(
                HotnessSnapshot.window_end == window_end,
                HotnessSnapshot.window_type == window_type,
            )
            .order_by(HotnessSnapshot.rank.asc())
            .limit(k)
        )
        return list(session.scalars(stmt).all())


class _SqliteFriendlyBriefingRepo:
    """SQLite 改 ON CONFLICT DO NOTHING 为"先 SELECT 已存在再 INSERT"。

    SQLite 不支持 PG ARRAY / JSONB，所以本 repo 不走 ORM，直接用 raw SQL；
    ARRAY 列用 JSON 字符串存，JSONB 列也用 JSON 字符串存。
    fetch_for_entity 只关心"是否存在"，返回 bool 等价物即可（用 dict 包一层
    模拟 ORM 接口足够测试 svc.run_once 的判断）。
    """

    _id_counter: int = 0

    def upsert_one(
        self,
        session,
        *,
        entity: str,
        window_end,
        fields: dict,
    ) -> int:
        from sqlalchemy import text

        existing = session.execute(
            text(
                "SELECT id FROM entity_briefings WHERE entity = :e AND window_end = :w"
            ),
            {"e": entity, "w": window_end},
        ).scalar()
        if existing is not None:
            return 0
        type(self)._id_counter += 1
        session.execute(
            text(
                """
                INSERT INTO entity_briefings (
                    id, entity, window_end, narrative, catalyst, fund_logic,
                    sentiment, confidence, evidence_msg_ids, raw_response
                ) VALUES (
                    :id, :entity, :window_end, :narrative, :catalyst, :fund_logic,
                    :sentiment, :confidence, :evidence_msg_ids, :raw_response
                )
                """
            ),
            {
                "id": type(self)._id_counter,
                "entity": entity,
                "window_end": window_end,
                "narrative": fields.get("narrative"),
                "catalyst": fields.get("catalyst"),
                "fund_logic": fields.get("fund_logic"),
                "sentiment": fields.get("sentiment"),
                "confidence": fields.get("confidence"),
                "evidence_msg_ids": json.dumps(fields["evidence_msg_ids"]),
                "raw_response": json.dumps(fields.get("raw_response")),
            },
        )
        session.flush()
        return 1

    def fetch_for_entity(self, session, *, entity, window_end):
        """返回简化记录（dict-like），仅用于"是否存在"判断。None = 不存在。"""
        from sqlalchemy import text

        row = session.execute(
            text(
                """
                SELECT id, entity, narrative
                FROM entity_briefings
                WHERE entity = :e AND window_end = :w
                """
            ),
            {"e": entity, "w": window_end},
        ).first()
        return row  # 非 None 即视为存在，svc 只判断 `is not None`

    def fetch_recent(self, session, *, window_end, limit=20):
        from sqlalchemy import text

        return list(
            session.execute(
                text(
                    """
                    SELECT id, entity, narrative
                    FROM entity_briefings
                    WHERE window_end = :w
                    ORDER BY created_at DESC
                    LIMIT :lim
                    """
                ),
                {"w": window_end, "lim": limit},
            ).all()
        )


# ===========================================================================
# 用例 1-2：_select_evidence
# ===========================================================================


def test_select_evidence_top_n(sqlite_db, prompt_path) -> None:
    """
    Req 4：evidence 选择按 engagement DESC 取 Top-N。
    高 engagement 的应排在前面。
    """
    window_end = datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc)
    base = window_end - timedelta(minutes=30)
    # 插 5 条 BTC mention，engagement 各异
    for i, eng in enumerate([1, 100, 50, 200, 5], start=1):
        _insert_msg_with_mention(
            sqlite_db,
            msg_id=i,
            entity="BTC",
            text=f"msg-{i} engagement={eng}",
            ts=base + timedelta(minutes=i),
            engagement=eng,
        )

    svc = _make_service(sqlite_db, prompt_path, evidence_count=3)
    with sqlite_db.get_session() as s:
        evid = svc._select_evidence(s, entity="BTC", window_end=window_end)

    assert len(evid) == 3
    # 高 engagement 应在前列
    engagements = [m.engagement for m in evid]
    assert engagements == sorted(engagements, reverse=True), (
        f"应按 engagement 降序，实际 {engagements}"
    )
    assert engagements[0] == 200


def test_select_evidence_falls_back_to_random_when_no_engagement(
    sqlite_db, prompt_path
) -> None:
    """
    Req 4.2：engagement 全 0 时，evidence 退化为随机抽样
    （至少能拉到对应数量的消息，不报错）。
    """
    window_end = datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc)
    base = window_end - timedelta(minutes=30)
    for i in range(1, 6):
        _insert_msg_with_mention(
            sqlite_db,
            msg_id=i,
            entity="ETH",
            text=f"msg-{i}",
            ts=base + timedelta(minutes=i),
            engagement=0,
        )

    svc = _make_service(sqlite_db, prompt_path, evidence_count=3)
    with sqlite_db.get_session() as s:
        evid = svc._select_evidence(s, entity="ETH", window_end=window_end)

    assert len(evid) == 3
    # engagement 全 0 时，DB 内部按 random() 排，能拉到任意 3 条即合格
    assert all(m.engagement == 0 for m in evid)


# ===========================================================================
# 用例 3：_render_prompt
# ===========================================================================


def test_render_prompt_replaces_placeholders(sqlite_db, prompt_path) -> None:
    """
    Req 3：prompt 模板正确替换 {entity} / {n_msgs} / {messages} / {cooccur_hint}。
    """
    window_end = datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc)
    base = window_end - timedelta(minutes=30)
    for i in range(1, 4):
        _insert_msg_with_mention(
            sqlite_db,
            msg_id=i,
            entity="EIGEN",
            text=f"hello world {i}",
            ts=base + timedelta(minutes=i),
        )

    svc = _make_service(sqlite_db, prompt_path)
    with sqlite_db.get_session() as s:
        evid = svc._select_evidence(s, entity="EIGEN", window_end=window_end)

    rendered = svc._render_prompt(
        entity="EIGEN", evidence=evid, cooccur_hint="hint-X"
    )
    assert "实体: EIGEN" in rendered
    assert "条数: 3" in rendered
    assert "共现: hint-X" in rendered
    assert "hello world 1" in rendered
    assert "hello world 2" in rendered
    assert "hello world 3" in rendered


# ===========================================================================
# 用例 4-5：_parse_json
# ===========================================================================


def test_parse_json_valid(sqlite_db, prompt_path) -> None:
    """
    Req 3.3：解析合法 JSON，5 个字段全部正确归一化。
    包括 confidence 字符串自动转 float、sentiment 合法值保留。
    """
    svc = _make_service(sqlite_db, prompt_path)
    raw = json.dumps(
        {
            "narrative": "Restaking 复苏",
            "catalyst": "EigenLayer v2.0 上线",
            "fund_logic": "TVL 反弹至 200 亿",
            "sentiment": "bullish",
            "confidence": "0.85",  # 字符串
        }
    )
    out = svc._parse_json(raw)
    assert out["narrative"] == "Restaking 复苏"
    assert out["catalyst"] == "EigenLayer v2.0 上线"
    assert out["fund_logic"] == "TVL 反弹至 200 亿"
    assert out["sentiment"] == "bullish"
    assert isinstance(out["confidence"], float)
    assert abs(out["confidence"] - 0.85) < 1e-9


def test_parse_json_strips_markdown_code_block(sqlite_db, prompt_path) -> None:
    """
    LLM 偶尔会加 ```json ... ``` 包裹，应能剥掉再解析。
    """
    svc = _make_service(sqlite_db, prompt_path)
    raw = '```json\n{"narrative": "AI", "sentiment": "neutral", "confidence": 0.5}\n```'
    out = svc._parse_json(raw)
    assert out["narrative"] == "AI"
    assert out["sentiment"] == "neutral"


def test_parse_json_repairs_unquoted_sentiment_enum(sqlite_db, prompt_path) -> None:
    """
    本地小模型偶尔输出 "sentiment": neutral（裸枚举值漏引号），
    解析器应能用兜底正则修复一次再 json.loads。
    对应线上报错：JSON 解析失败: Expecting value: line 5 column 16
    """
    svc = _make_service(sqlite_db, prompt_path)
    raw = (
        '{\n'
        '  "narrative": null,\n'
        '  "catalyst": null,\n'
        '  "fund_logic": null,\n'
        '  "sentiment": neutral,\n'
        '  "confidence": 0.0\n'
        '}'
    )
    out = svc._parse_json(raw)
    assert out["sentiment"] == "neutral"
    assert out["confidence"] == 0.0
    assert out["narrative"] is None


def test_parse_json_invalid_raises(sqlite_db, prompt_path) -> None:
    """
    Req 3.3 / 5.3：非法 JSON 应 raise ValueError。
    """
    svc = _make_service(sqlite_db, prompt_path)
    with pytest.raises(ValueError):
        svc._parse_json("not a json at all")
    with pytest.raises(ValueError):
        svc._parse_json("")
    with pytest.raises(ValueError):
        svc._parse_json("[1, 2, 3]")  # 不是 dict 也 raise


# ===========================================================================
# 用例 6：跳过——榜单为空
# ===========================================================================


def test_skips_when_no_top_entities(sqlite_db, prompt_path) -> None:
    """
    Req 2.3：hotness_snapshots 表为空 → run_once 直接返回 False，不调 LLM。
    """
    svc = _make_service(sqlite_db, prompt_path, ollama_response='{}')
    assert svc.run_once() is False
    svc.ollama.chat.assert_not_called()


# ===========================================================================
# 用例 7：跳过——同 entity 已有 briefing
# ===========================================================================


def test_skips_already_briefed_entities(sqlite_db, prompt_path) -> None:
    """
    Req 2.3：fetch_for_entity 命中已存在 briefing → 跳过，不再调 LLM。
    """
    window_end = datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc)
    base = window_end - timedelta(minutes=30)

    # 1) 准备数据：BTC 上榜（growth 50） + 1 条 evidence
    _insert_hotness_top(
        sqlite_db, window_end=window_end, entries=[("BTC", 50.0, 1)]
    )
    _insert_msg_with_mention(
        sqlite_db, msg_id=1, entity="BTC", text="hello", ts=base
    )

    # 2) 预先插一条同 (entity, window_end) 的 briefing
    # SQLite 不保留 tz，HotnessSnapshot.fetch_latest_window_end 返回的是 naive
    # datetime；所以这里用 naive 形式插入，保证 fetch_for_entity 能匹配上
    naive_we = window_end.replace(tzinfo=None)
    with sqlite_db.get_session() as s:
        from sqlalchemy import text

        s.execute(
            text(
                """
                INSERT INTO entity_briefings (
                    id, entity, window_end, narrative, evidence_msg_ids
                ) VALUES (999, 'BTC', :w, '预先存在', '[1]')
                """
            ),
            {"w": naive_we},
        )
        s.commit()

    svc = _make_service(
        sqlite_db,
        prompt_path,
        ollama_response='{"narrative":"new","sentiment":"bullish","confidence":0.9}',
    )
    # 整轮没有需要新生成的 → run_once 返回 False
    assert svc.run_once() is False
    # 关键断言：LLM 完全没被调用
    svc.ollama.chat.assert_not_called()


# ===========================================================================
# 用例 8：单 entity 失败不影响其它（per-entity 异常隔离）
# ===========================================================================


def test_per_entity_failure_isolated(sqlite_db, prompt_path) -> None:
    """
    Req 2.3 / 5.4：entity A 的 LLM 抛异常时，entity B 仍能正常生成 briefing。

    构造：
    - BTC（rank 1）：ollama.chat 第一次调用 → raise RuntimeError
    - ETH（rank 2）：ollama.chat 第二次调用 → 返回合法 JSON

    断言：run_once True（至少 1 个成功）；ETH 的 briefing 已落库。
    """
    window_end = datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc)
    base = window_end - timedelta(minutes=30)

    _insert_hotness_top(
        sqlite_db,
        window_end=window_end,
        entries=[("BTC", 50.0, 1), ("ETH", 40.0, 2)],
    )
    for i, ent in enumerate(["BTC", "ETH"], start=1):
        _insert_msg_with_mention(
            sqlite_db,
            msg_id=i,
            entity=ent,
            text=f"msg-{ent}",
            ts=base + timedelta(minutes=i),
        )

    # mock：第一次调用抛异常，第二次返回合法 JSON
    ollama = MagicMock()
    ollama.chat.side_effect = [
        RuntimeError("simulated LLM timeout"),
        '{"narrative":"ETH narrative","sentiment":"neutral","confidence":0.7}',
    ]

    from db.repositories.entity_mentions_repo import EntityMentionsRepo
    from db.repositories.normalized_messages_repo import NormalizedMessagesRepo

    svc = BriefingService(
        db=sqlite_db,
        hotness_repo=_SqliteFriendlyHotnessRepo(),
        mentions_repo=EntityMentionsRepo(),
        normalized_repo=NormalizedMessagesRepo(),
        briefing_repo=_SqliteFriendlyBriefingRepo(),
        ollama=ollama,
        prompt_path=prompt_path,
        top_n=5,
        min_growth=30.0,
        evidence_count=10,
        timezone=ZoneInfo("UTC"),
    )

    assert svc.run_once() is True

    with sqlite_db.get_session() as s:
        from sqlalchemy import text

        rows = list(
            s.execute(text("SELECT entity FROM entity_briefings")).all()
        )

    # ETH 应该写入；BTC 因为 LLM 失败不写
    entities = {r[0] for r in rows}
    assert "ETH" in entities, f"ETH 应已写入，实际：{entities}"
    assert "BTC" not in entities, f"BTC LLM 失败不应写入，实际：{entities}"


# ===========================================================================
# 用例 9：跳过——同窗口已扫
# ===========================================================================


def test_skips_when_window_unchanged(sqlite_db, prompt_path) -> None:
    """
    Req 2.3 / 5.3：第二次 run_once 同 window_end → 直接 False，不调 LLM。
    """
    window_end = datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc)
    base = window_end - timedelta(minutes=30)
    _insert_hotness_top(
        sqlite_db, window_end=window_end, entries=[("BTC", 50.0, 1)]
    )
    _insert_msg_with_mention(
        sqlite_db, msg_id=1, entity="BTC", text="hello", ts=base
    )

    svc = _make_service(
        sqlite_db,
        prompt_path,
        ollama_response='{"narrative":"x","sentiment":"bullish","confidence":0.8}',
    )

    assert svc.run_once() is True
    # 第二次：同窗口已处理 → 跳过
    svc.ollama.chat.reset_mock()
    assert svc.run_once() is False
    svc.ollama.chat.assert_not_called()


# ===========================================================================
# 用例 10：跳过——growth < min_growth
# ===========================================================================


def test_low_growth_filtered_out(sqlite_db, prompt_path) -> None:
    """
    Req 2.3：Top-N 全部 growth < min_growth → 跳过，不调 LLM。
    """
    window_end = datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc)
    # min_growth=30，所有 entity 都低于阈值
    _insert_hotness_top(
        sqlite_db,
        window_end=window_end,
        entries=[
            ("LOW1", 5.0, 1),
            ("LOW2", 3.0, 2),
            ("LOW3", 1.0, 3),
        ],
    )

    svc = _make_service(
        sqlite_db,
        prompt_path,
        ollama_response='{}',
        min_growth=30.0,
    )
    assert svc.run_once() is False
    svc.ollama.chat.assert_not_called()
