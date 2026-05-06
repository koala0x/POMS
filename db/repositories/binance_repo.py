from __future__ import annotations

from db.repositories.raw_posts_base import RawPostsRepoBase


class BinanceRepo(RawPostsRepoBase):
    def __init__(self) -> None:
        super().__init__(table_name="binance_square_posts")
