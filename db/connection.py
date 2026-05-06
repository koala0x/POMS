from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator, Optional

import psycopg2
from loguru import logger
from psycopg2.pool import SimpleConnectionPool

from config.settings import Settings


class Database:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: Optional[SimpleConnectionPool] = None

    def _ensure_pool(self) -> SimpleConnectionPool:
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
        if self._pool is None:
            return
        try:
            self._pool.closeall()
        finally:
            self._pool = None

    @contextmanager
    def get_conn(self) -> Iterator[psycopg2.extensions.connection]:
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
