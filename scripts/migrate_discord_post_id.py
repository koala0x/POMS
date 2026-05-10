from __future__ import annotations

"""
一次性迁移脚本:给 discord_messages 加 post_id 列 + UNIQUE 约束。

与 migrate_binance_post_id.py 对称:
- post_id 用来存 Discord 侧复合原生 ID(例如 "<channel_id>-<message_id>"),
  抓取侧后续走 INSERT ... ON CONFLICT (post_id) DO NOTHING 做去重。
- 列可空,历史数据无需回填;多个 NULL 在 PG 默认行为下不冲突。

幂等:已经存在的列/约束会被跳过,脚本可以重复跑。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loguru import logger
from sqlalchemy import text

from config.settings import get_settings
from db.connection import Database


# IF NOT EXISTS 在 ADD COLUMN 从 PG 9.6 起支持;ADD CONSTRAINT 没有 IF NOT EXISTS,
# 所以约束部分先查 pg_constraint 再决定是否执行。
_ADD_COLUMN_SQL = """
ALTER TABLE discord_messages
ADD COLUMN IF NOT EXISTS post_id VARCHAR(64)
"""

_HAS_CONSTRAINT_SQL = """
SELECT 1
FROM pg_constraint
WHERE conname = 'uq_discord_messages_post_id'
"""

_ADD_CONSTRAINT_SQL = """
ALTER TABLE discord_messages
ADD CONSTRAINT uq_discord_messages_post_id UNIQUE (post_id)
"""


def main() -> int:
    settings = get_settings()
    db = Database(settings)

    with db.get_session() as session:
        logger.info("开始迁移 discord_messages.post_id")
        session.execute(text(_ADD_COLUMN_SQL))

        has_constraint = session.execute(text(_HAS_CONSTRAINT_SQL)).scalar()
        if has_constraint:
            logger.info("UNIQUE 约束 uq_discord_messages_post_id 已存在,跳过")
        else:
            session.execute(text(_ADD_CONSTRAINT_SQL))
            logger.info("已添加 UNIQUE 约束 uq_discord_messages_post_id")

        session.commit()

    logger.info("迁移完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
