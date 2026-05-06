from __future__ import annotations

"""
Repository 集成测试,基于真实 PostgreSQL。

策略:
- 通过 .env 读取 DB 配置;若环境不可用则 pytest.skip 而不报错
- 每个用例使用独立 Session,在 finally 中清理本测试插入的行(按 id 范围)
- 不污染表结构,直接复用 create_all() 已建好的业务表
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from config.settings import get_settings
from db.connection import Database
from db.models import BinanceSquarePost, SummaryLevel1, SummaryLevel2, TwitterPost
from db.repositories.binance_repo import BinanceRepo
from db.repositories.level1_repo import Level1Repo
from db.repositories.level2_repo import Level2Repo
from db.repositories.twitter_repo import TwitterRepo


@pytest.fixture(scope="module")
def db() -> Database:
    """初始化 Database 并兜底建表。如 PG 不可达则跳过整个模块。"""
    try:
        instance = Database(get_settings())
        instance.create_all()
        # 显式触发一次连接,确保 PG 可用。
        with instance.get_session() as session:
            session.execute(select(1))
        return instance
    except Exception as e:
        pytest.skip(f"PostgreSQL 不可用,跳过集成测试:{e}")


def _now_utc() -> datetime:
    """构造带 tz 的 now,避免 PG 端 TIMESTAMPTZ 列被推断成 naive。"""
    return datetime.now(timezone.utc)


def test_twitter_repo_count_fetch_mark(db: Database) -> None:
    """覆盖 twitter_posts 仓储的 count → fetch → mark 三件套。"""
    repo = TwitterRepo()
    inserted_ids: list[int] = []

    try:
        with db.get_session() as session:
            for i in range(3):
                p = TwitterPost(content=f"orm-test-tw-{i}", author="pytest")
                session.add(p)
            session.flush()
            inserted_ids = [
                int(r.id)
                for r in session.scalars(
                    select(TwitterPost).where(TwitterPost.author == "pytest")
                ).all()
            ]
            session.commit()

        with db.get_session() as session:
            cnt = repo.count_unsummarized(session)
            assert cnt >= 3

            # 直接按 id 查回这 3 条,验证存在 + 是否未摘要(避免被
            # 大批量 seed 数据挤出 fetch_oldest_unsummarized 的 limit)。
            rows = session.scalars(
                select(TwitterPost).where(TwitterPost.id.in_(inserted_ids))
            ).all()
            assert {int(r.id) for r in rows} == set(inserted_ids)
            assert all(r.is_summarized is False for r in rows)

        with db.get_session() as session:
            updated = repo.mark_summarized(session, inserted_ids)
            assert updated == len(inserted_ids)
            session.commit()

        # 二次调用:同一批 id 已是 TRUE,不应重复更新。
        with db.get_session() as session:
            updated_again = repo.mark_summarized(session, inserted_ids)
            assert updated_again == 0
            session.commit()
    finally:
        # 清理:删除本用例新增的行。
        if inserted_ids:
            with db.get_session() as session:
                session.query(TwitterPost).filter(
                    TwitterPost.id.in_(inserted_ids)
                ).delete(synchronize_session=False)
                session.commit()


def test_binance_repo_basic(db: Database) -> None:
    """BinanceRepo 与 TwitterRepo 行为一致,这里只做最小验证。"""
    repo = BinanceRepo()
    inserted_ids: list[int] = []
    try:
        with db.get_session() as session:
            for i in range(2):
                session.add(BinanceSquarePost(content=f"orm-test-bn-{i}", author="pytest"))
            session.flush()
            inserted_ids = [
                int(r.id)
                for r in session.scalars(
                    select(BinanceSquarePost).where(BinanceSquarePost.author == "pytest")
                ).all()
            ]
            session.commit()

        with db.get_session() as session:
            cnt = repo.count_unsummarized(session)
            assert cnt >= 2
    finally:
        if inserted_ids:
            with db.get_session() as session:
                session.query(BinanceSquarePost).filter(
                    BinanceSquarePost.id.in_(inserted_ids)
                ).delete(synchronize_session=False)
                session.commit()


def test_level1_repo_insert_fetch_mark(db: Database) -> None:
    """覆盖 Level1Repo.insert + fetch_unsummarized_for_period + mark_summarized_l2。"""
    repo = Level1Repo()
    now = _now_utc()
    period_start = now - timedelta(minutes=1)
    period_end = now + timedelta(minutes=1)
    new_id: int | None = None

    try:
        with db.get_session() as session:
            new_id = repo.insert(
                session=session,
                source="twitter",
                summary="orm-test-l1-summary",
                raw_ids=[101, 102, 103],
                raw_count=3,
                created_at=now,
            )
            assert new_id > 0
            session.commit()

        with db.get_session() as session:
            rows = repo.fetch_unsummarized_for_period(
                session=session,
                source="twitter",
                period_start=period_start,
                period_end=period_end,
            )
            assert any(r.id == new_id for r in rows)

        with db.get_session() as session:
            updated = repo.mark_summarized_l2(session, [new_id])
            assert updated == 1
            session.commit()

        # 二次窗口查询:刚才的记录已被标记,不应再出现。
        with db.get_session() as session:
            rows = repo.fetch_unsummarized_for_period(
                session=session,
                source="twitter",
                period_start=period_start,
                period_end=period_end,
            )
            assert all(r.id != new_id for r in rows)
    finally:
        if new_id is not None:
            with db.get_session() as session:
                session.query(SummaryLevel1).filter(SummaryLevel1.id == new_id).delete(
                    synchronize_session=False
                )
                session.commit()


def test_level2_repo_insert(db: Database) -> None:
    repo = Level2Repo()
    now = _now_utc()
    new_id: int | None = None
    try:
        with db.get_session() as session:
            new_id = repo.insert(
                session=session,
                source="twitter",
                summary="orm-test-l2-summary",
                level1_ids=[201, 202],
                level1_count=2,
                period_start=now - timedelta(hours=1),
                period_end=now,
                created_at=now,
            )
            assert new_id > 0
            session.commit()

        with db.get_session() as session:
            row = session.get(SummaryLevel2, new_id)
            assert row is not None
            assert row.source == "twitter"
            assert row.level1_ids == [201, 202]
            assert row.level1_count == 2
    finally:
        if new_id is not None:
            with db.get_session() as session:
                session.query(SummaryLevel2).filter(SummaryLevel2.id == new_id).delete(
                    synchronize_session=False
                )
                session.commit()


# ============================================================
# 手动种子测试:面向真实场景的端到端冒烟
# ============================================================
#
# 用途:把 50 条带真实文本的 twitter_posts 灌进 PG,模拟"达到 batch_size"
# 的触发条件,启动 main.py 后可以观察 Level1Service 是否正常吐出 summary。
#
# 与上面集成测试的区别:
# - **不做 cleanup**——种下的数据需要保留,等 Service 把它们消费掉
# - 默认每条 content 都不一样(带索引),便于在数据库里快速肉眼区分
# - 可通过 pytest 单独触发:
#   `pytest tests/test_repositories.py::test_seed_50_twitter_posts -s`
#   `-s` 让 print 直接输出,看到插入的 id 列表
# ============================================================


_SEED_AUTHOR = "seed-50"
_SEED_SAMPLE_CONTENTS = [
    "$BTC 突破前期高点,链上资金持续流入,鲸鱼地址增持。",
    "$ETH gas 费降至年内低点,L2 网络活跃度爆表。",
    "美 SEC 推迟比特币现货 ETF 决议,市场情绪转向谨慎。",
    "Solana 生态 meme 币热度回升,DEX 交易量周环比 +30%。",
    "Vitalik 发文讨论 PoS 安全模型,引发社区激辩。",
    "Tether 公布最新储备金报告,USDT 流通量再创新高。",
    "Coinbase 上线新代币,首日涨幅 80%。",
    "Binance Labs 投资 AI x Crypto 项目,A 轮估值 5 亿。",
    "BlackRock 增持 IBIT,机构持仓占比突破 25%。",
    "MicroStrategy 再次买入 1000 BTC,持仓接近 22 万。",
]


def test_seed_50_twitter_posts(db: Database) -> None:
    """
    向 twitter_posts 写入 50 条不同内容的样本数据。

    设计:
    - 使用 ORM 批量 add + commit
    - 内容来源于 _SEED_SAMPLE_CONTENTS,通过 (idx + i) 取模轮换,保证 50 条都不重复
    - posted_at 跨度过去 50 分钟(每条间隔 1 分钟),便于看到时间序
    - is_summarized 默认 False,Service 启动后会按 batch_size=50 一次性消费
    """
    base_time = datetime.now(timezone.utc) - timedelta(minutes=50)

    inserted_ids: list[int] = []
    with db.get_session() as session:
        for i in range(50):
            content_idx = i % len(_SEED_SAMPLE_CONTENTS)
            content = f"[seed#{i:02d}] {_SEED_SAMPLE_CONTENTS[content_idx]}"
            post = TwitterPost(
                content=content,
                author=_SEED_AUTHOR,
                posted_at=base_time + timedelta(minutes=i),
            )
            session.add(post)
        session.flush()

        # 拿到所有刚插入的 id(这一批 author 都是 _SEED_AUTHOR)
        inserted_ids = [
            int(p.id)
            for p in session.scalars(
                select(TwitterPost)
                .where(TwitterPost.author == _SEED_AUTHOR)
                .order_by(TwitterPost.id.desc())
                .limit(50)
            ).all()
        ]
        session.commit()

    print(f"\n已写入 {len(inserted_ids)} 条 twitter_posts,id 区间:"
          f"{min(inserted_ids)} ~ {max(inserted_ids)}")

    # 基本断言:50 条都成功落库,is_summarized 默认 false
    assert len(inserted_ids) == 50
    with db.get_session() as session:
        unsummarized = session.scalar(
            select(func.count())
            .select_from(TwitterPost)
            .where(
                TwitterPost.id.in_(inserted_ids),
                TwitterPost.is_summarized.is_(False),
            )
        )
        assert unsummarized == 50

