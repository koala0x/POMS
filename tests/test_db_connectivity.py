"""
数据库连通性测试（集成测试 / Integration Test）

说明：
- 这不是纯单元测试，因为它会尝试连接真实 PostgreSQL。
- 为了不影响本地/CI 的默认测试体验，本测试默认“跳过”，只有在能找到可用 DSN 时才会运行。

提供 DSN 的方式（按优先级）：
1) 环境变量：POMS_PG_DSN 或 DATABASE_URL
2) 配置文件：config/poms_worker.ini（或通过 POMS_CONFIG 指定路径）

运行方式（示例）：
- POMS_PG_DSN=... python -m unittest -v tests.test_db_connectivity
- 或者：把 DSN 写进 config/poms_worker.ini，然后运行本测试
"""

import os
import unittest
from typing import Optional

import src.poms.main as poms_worker


def _looks_like_placeholder_dsn(dsn: str) -> bool:
    value = (dsn or "").strip()
    if not value:
        return True
    if "user:pass@" in value:
        return True
    if value.endswith("/your_db"):
        return True
    return False


def _load_dsn_for_test() -> Optional[str]:
    dsn = os.getenv("POMS_PG_DSN") or os.getenv("DATABASE_URL")
    if isinstance(dsn, str) and dsn.strip() and not _looks_like_placeholder_dsn(dsn):
        return dsn.strip()

    config_path = (os.getenv("POMS_CONFIG") or "").strip() or None
    if not config_path:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config_path = os.path.join(repo_root, "config", "poms_worker.ini")

    if os.path.exists(config_path):
        conf = poms_worker._load_ini_config(config_path)
        dsn = conf.get("pg_dsn")
        if isinstance(dsn, str) and dsn.strip() and not _looks_like_placeholder_dsn(dsn):
            return dsn.strip()

    return None


class TestDbConnectivity(unittest.TestCase):
    def test_postgres_connect_and_select_1(self) -> None:
        # 目的：快速验证“能连上库 + 能执行最小查询”，用于排查环境/DSN/网络/权限问题。
        dsn = _load_dsn_for_test()
        if not dsn:
            self.skipTest("未找到可用 DSN（环境变量或 config/poms_worker.ini），跳过数据库连通性测试")

        # 驱动缺失时直接跳过（避免把“环境没装依赖”误报成业务错误）。
        try:
            import psycopg  # noqa: F401
        except Exception:
            try:
                import psycopg2  # noqa: F401
            except Exception:
                self.skipTest("未安装 psycopg 或 psycopg2，跳过数据库连通性测试")

        db = poms_worker.Db(dsn=dsn)
        db.connect()
        try:
            cur = db.cursor()
            cur.execute("SELECT 1")
            row = cur.fetchone()
            self.assertEqual(row[0], 1)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
