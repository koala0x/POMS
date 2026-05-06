from __future__ import annotations

from db.repositories.raw_posts_base import RawPostsRepoBase


class TwitterRepo(RawPostsRepoBase):
    def __init__(self) -> None:
        super().__init__(table_name="twitter_posts")
