from __future__ import annotations

"""discord_messages 表的仓储,绑定到 DiscordMessage ORM 模型。"""

from db.models import DiscordMessage
from db.repositories.raw_posts_base import RawPostsRepoBase


class DiscordRepo(RawPostsRepoBase):
    model = DiscordMessage
