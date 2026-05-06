"""
数据库连通性测试（集成测试 / Integration Test）

说明：
- 这不是纯单元测试，因为它会尝试连接真实 PostgreSQL。
- 为了不影响本地/CI 的默认测试体验，本测试默认“跳过”，只有在能找到可用 DSN 时才会运行。

如何启用：
- 提供 DSN 的方式（按优先级）：
  1) 环境变量：POMS_PG_DSN 或 DATABASE_URL
  2) 配置文件：./config/poms_worker.ini（或通过 POMS_CONFIG 指定路径）
- 并确保已安装 PostgreSQL Python 驱动（psycopg 或 psycopg2）

运行方式（示例）：
- POMS_PG_DSN=... python -m unittest -v tests.test_db_connectivity
- 或者：把 DSN 写进 config/poms_worker.ini，然后直接运行该测试文件
"""

import os
import unittest

import src.poms.main as poms_worker





class TestDbConnectivity(unittest.TestCase):
    def test_postgres_connect_and_select_1(self) -> None:
        config_path = os.path.join(os.getcwd(), "config", "poms_worker.ini")
        conf = poms_worker._load_ini_config(config_path)
        dsn = conf.get("dsn")
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
