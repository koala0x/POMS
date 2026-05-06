from __future__ import annotations

"""binance_square_posts 表的仓储,绑定到 BinanceSquarePost ORM 模型。"""

from db.models import BinanceSquarePost
from db.repositories.raw_posts_base import RawPostsRepoBase


class BinanceRepo(RawPostsRepoBase):
    model = BinanceSquarePost
