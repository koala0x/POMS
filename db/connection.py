from __future__ import annotations

"""
数据库连接 + 会话管理。

基于 SQLAlchemy 2.x:
- create_engine 创建带连接池的引擎(pool_pre_ping 自动剔除已断开连接,
  替代了原先手写的 reconnect 逻辑)
- sessionmaker 工厂生成 Session;通过 get_session() contextmanager 借出
- 不在 contextmanager 里自动 commit,由 Service 层显式控制事务边界
  (保持原有"失败时不更新标记"的语义)
"""

from contextlib import contextmanager
from typing import Iterator, Optional

from loguru import logger
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from config.settings import Settings
from db.models import Base


class Database:
    """
    PostgreSQL ORM 包装。

    用法:
        db = Database(settings)
        db.create_all()                  # 建表(幂等)
        with db.get_session() as session:
            ...                          # 使用 ORM 操作
            session.commit()             # 显式提交
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine: Optional[Engine] = None
        self._session_factory: Optional[sessionmaker[Session]] = None

    def _ensure(self) -> sessionmaker[Session]:
        """
        延迟初始化 engine + session 工厂。

        服务启动时不立即建连(避免 DB 未就绪导致进程退出);
        第一次访问时才创建,并复用给后续所有调用。
        """
        if self._session_factory is not None:
            return self._session_factory

        url = (
            f"postgresql+psycopg2://"
            f"{self._settings.db_user}:{self._settings.db_password}"
            f"@{self._settings.db_host}:{self._settings.db_port}"
            f"/{self._settings.db_name}"
        )

        self._engine = create_engine(
            url,
            pool_size=5,
            max_overflow=5,
            pool_pre_ping=True,
            pool_recycle=1800,
            future=True,
        )
        self._session_factory = sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            future=True,
        )
        return self._session_factory

    @property
    def engine(self) -> Engine:
        """暴露底层 Engine,主要供 create_all/测试 fixture 使用。"""
        self._ensure()
        assert self._engine is not None
        return self._engine

    def create_all(self) -> None:
        """
        幂等建表:基于 ORM 模型 metadata 创建所有表与索引。

        启动时调用,确保新部署或新环境无需手动跑迁移脚本。
        """
        Base.metadata.create_all(self.engine)

    def reconnect(self) -> None:
        """
        主动释放当前连接池中的所有连接。

        典型场景:外部 DB 主备切换后,池中的连接全部失效。
        通常 pool_pre_ping=True 已经能覆盖大多数场景,这里保留为兜底接口。
        """
        if self._engine is None:
            return
        try:
            self._engine.dispose()
        finally:
            self._engine = None
            self._session_factory = None

    @contextmanager
    def get_session(self) -> Iterator[Session]:
        """
        借出一个 Session 并在结束时关闭。

        - 异常时自动 rollback(防止半提交事务残留)
        - 不自动 commit:由调用方在业务逻辑成功后显式 session.commit()
        - OperationalError(DB 连接异常)单独打日志,便于运维定位
        """
        factory = self._ensure()
        session = factory()
        try:
            yield session
        except OperationalError as e:
            logger.error("数据库连接异常:{}", e)
            session.rollback()
            raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
