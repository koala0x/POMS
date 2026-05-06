from __future__ import annotations

"""
原始帖子表(twitter_posts / binance_square_posts)仓储基类。

基于 SQLAlchemy 2.x ORM:
- 子类只需要把 `model` 类属性指向 TwitterPost / BinanceSquarePost
- 三类操作:
  - count_unsummarized:统计 is_summarized=FALSE 的条数
  - fetch_oldest_unsummarized:按 created_at 升序取最早 N 条(同 created_at 时再按 id 排,保证稳定)
  - mark_summarized:把指定 id 的记录标记为已处理(只更新仍为 FALSE 的行,幂等)
"""

from typing import Sequence, Type

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from db.models import Base


class RawPostsRepoBase:
    """原始帖子仓储基类,子类通过 `model` 类属性绑定具体 ORM 模型。"""

    # 子类填入:TwitterPost / BinanceSquarePost
    model: Type[Base]

    def count_unsummarized(self, session: Session) -> int:
        """统计未处理条数,用于决定是否触发一次摘要(>= batch_size)。"""
        stmt = (
            select(func.count())
            .select_from(self.model)
            .where(self.model.is_summarized.is_(False))
        )
        return int(session.scalar(stmt) or 0)

    def fetch_oldest_unsummarized(
        self, session: Session, limit: int
    ) -> list:
        """
        拉取最早入库的若干条未处理数据。

        - 按 created_at 升序(满足"按入库时间最早")
        - 同 created_at 时再按 id 升序,使排序结果在批次间稳定
        """
        stmt = (
            select(self.model)
            .where(self.model.is_summarized.is_(False))
            .order_by(self.model.created_at.asc(), self.model.id.asc())
            .limit(limit)
        )
        return list(session.scalars(stmt).all())

    def mark_summarized(self, session: Session, ids: Sequence[int]) -> int:
        """
        幂等标记为已处理。

        - 只更新仍为 FALSE 的行,避免重复执行造成 rowcount 失真
        - 返回实际被翻成 TRUE 的行数,业务层用它做一致性校验
        """
        if not ids:
            return 0
        stmt = (
            update(self.model)
            .where(
                self.model.id.in_(list(ids)),
                self.model.is_summarized.is_(False),
            )
            .values(is_summarized=True)
        )
        result = session.execute(stmt)
        return int(result.rowcount or 0)
