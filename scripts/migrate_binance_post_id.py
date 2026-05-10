from __future__ import annotations

"""
一次性迁移脚本:给 binance_square_posts 加 post_id 列 + UNIQUE 约束。

幂等:已经存在的列/约束会被跳过(IF NOT EXISTS),脚本可以重复跑。
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
ALTER TABLE binance_square_posts
ADD COLUMN IF NOT EXISTS post_id VARCHAR(64)
"""

_HAS_CONSTRAINT_SQL = """
SELECT 1
FROM pg_constraint
WHERE conname = 'uq_binance_square_posts_post_id'
"""

_ADD_CONSTRAINT_SQL = """
ALTER TABLE binance_square_posts
ADD CONSTRAINT uq_binance_square_posts_post_id UNIQUE (post_id)
"""


def main() -> int:
    settings = get_settings()
    db = Database(settings)

    with db.get_session() as session:
        logger.info("开始迁移 binance_square_posts.post_id")
        session.execute(text(_ADD_COLUMN_SQL))

        has_constraint = session.execute(text(_HAS_CONSTRAINT_SQL)).scalar()
        if has_constraint:
            logger.info("UNIQUE 约束 uq_binance_square_posts_post_id 已存在,跳过")
        else:
            session.execute(text(_ADD_CONSTRAINT_SQL))
            logger.info("已添加 UNIQUE 约束 uq_binance_square_posts_post_id")

        session.commit()

    logger.info("迁移完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
