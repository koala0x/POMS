from __future__ import annotations

"""
数据库连接管理。

本项目是“常驻后台服务”，数据库连接可能遇到网络抖动/连接重置：
- 使用 psycopg2 的连接池减少频繁建连开销
- 捕获 OperationalError 时清空连接池并短暂等待，交由上层任务下一轮重试
"""

import time
from contextlib import contextmanager
from typing import Iterator, Optional

import psycopg2
from loguru import logger
from psycopg2.pool import SimpleConnectionPool

from config.settings import Settings


class Database:
    """
    PostgreSQL 连接池包装。

    - get_conn(): 以 contextmanager 的形式借出连接
      - autocommit=False：由上层显式 commit/rollback，保证批处理一致性
      - 连接使用完会放回池中（或异常时关闭）
    - reconnect(): 清空池，下一次 get_conn 会重新建立连接池
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: Optional[SimpleConnectionPool] = None

    def _ensure_pool(self) -> SimpleConnectionPool:
        """
        延迟初始化连接池。

        服务启动后不立即建连，避免在部署时 DB 还没就绪导致启动失败；
        第一次真正需要访问 DB 时才创建连接池。
        """
        if self._pool is not None:
            return self._pool

        dsn = (
            f"host={self._settings.db_host} "
            f"port={self._settings.db_port} "
            f"dbname={self._settings.db_name} "
            f"user={self._settings.db_user} "
            f"password={self._settings.db_password}"
        )

        self._pool = SimpleConnectionPool(minconn=1, maxconn=10, dsn=dsn)
        return self._pool

    def reconnect(self) -> None:
        """
        主动丢弃当前连接池中的所有连接。

        典型场景：
        - 捕获到 psycopg2.OperationalError（连接中断/重置）
        - 连接池中可能混入不可用连接，直接 closeall() 再重建更可靠
        """
        if self._pool is None:
            return
        try:
            self._pool.closeall()
        finally:
            self._pool = None

    @contextmanager
    def get_conn(self) -> Iterator[psycopg2.extensions.connection]:
        """
        从连接池获取一个连接并以 contextmanager 形式返回。

        注意：
        - 只对 OperationalError 做“重连 + sleep”，其他异常交由上层处理
        - finally 中尽量把连接放回池；如果放回失败则关闭连接
        """
        pool = self._ensure_pool()
        conn = None
        try:
            conn = pool.getconn()
            conn.autocommit = False
            yield conn
        except psycopg2.OperationalError as e:
            logger.error("数据库连接异常：{}", e)
            self.reconnect()
            time.sleep(5)
            raise
        finally:
            if conn is not None and self._pool is not None:
                try:
                    if not conn.closed:
                        pool.putconn(conn)
                except Exception:
                    try:
                        conn.close()
                    finally:
                        pass
