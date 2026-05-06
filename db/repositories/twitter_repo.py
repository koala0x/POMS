from __future__ import annotations

"""twitter_posts 表的仓储,绑定到 TwitterPost ORM 模型。"""

from db.models import TwitterPost
from db.repositories.raw_posts_base import RawPostsRepoBase


class TwitterRepo(RawPostsRepoBase):
    model = TwitterPost
