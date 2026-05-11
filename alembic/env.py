from __future__ import annotations

"""
Alembic 环境配置。

与 Alembic 默认模板的差异:
1. 从 config/settings.py 的 get_settings() 动态读取 DB URL,不再依赖 alembic.ini
   里的 sqlalchemy.url 配置项。这样生产/开发/测试的数据库配置只有一份 source of truth。
2. target_metadata 绑定到 db.models.Base.metadata,支持未来如果要用 autogenerate
   (Phase 1 不用,但留好)。
3. 把项目根目录加入 sys.path,保证 `from db.models import Base` / `from config.settings import get_settings`
   在 `alembic` CLI 工作目录下可 import。
"""

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 项目根目录(alembic/ 的父目录)加入 sys.path,
# 这样 alembic CLI 不论从哪启动都能 import 到 db / config 顶级包
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config.settings import get_settings  # noqa: E402
from db.models import Base  # noqa: E402

# Alembic Config 对象,提供对 alembic.ini 内容的访问
config = context.config

# 日志配置:按 alembic.ini 里的 [loggers] / [handlers] / [formatters] 段初始化
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 动态覆盖 sqlalchemy.url:从 Settings 构造真实的 DSN,保证
# alembic.ini 里无需保存明文密码,也避免开发/生产配置漂移
_settings = get_settings()
_db_url = (
    f"postgresql+psycopg2://"
    f"{_settings.db_user}:{_settings.db_password}"
    f"@{_settings.db_host}:{_settings.db_port}"
    f"/{_settings.db_name}"
)
config.set_main_option("sqlalchemy.url", _db_url)

# 绑定元数据,支持 autogenerate(Phase 1 手写迁移时其实用不到,但留好接口)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """
    Offline 模式:只输出 SQL 脚本,不连 DB。

    用于代码审查或把迁移 SQL 导出到 CI/CD 管道外部执行。
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Online 模式:直连 PostgreSQL 执行迁移。

    NullPool 避免 Alembic 自己带一个长连接,保证命令结束立即释放资源。
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
