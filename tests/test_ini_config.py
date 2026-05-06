import os
import tempfile
import unittest
from unittest import mock

import poms.main as poms_worker


class TestIniConfig(unittest.TestCase):
    def test_load_ini_config_sources_and_worker(self) -> None:
        # 验证：从 INI 文件读取配置时，能正确解析 postgres/ollama/worker 以及 source.* 段落。
        ini = """
[postgres]
dsn = postgresql://u:p@127.0.0.1:5432/db

[ollama]
base_url = http://localhost:11434
model = qwen2.5
timeout_s = 3

[worker]
batch_size = 7
poll_interval_s = 1.5
fetch_limit = 99
hourly_enabled = false

[sources]
enabled = a,b

[source.a]
table = t_a
id_column = id
text_column = content
created_at_column = created_at

[source.b]
table = t_b
text_column = body
"""
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as f:
            f.write(ini)
            path = f.name

        try:
            conf = poms_worker._load_ini_config(path)
        finally:
            os.unlink(path)

        self.assertEqual(conf["pg_dsn"], "postgresql://u:p@127.0.0.1:5432/db")
        self.assertEqual(conf["ollama_base_url"], "http://localhost:11434")
        self.assertEqual(conf["ollama_model"], "qwen2.5")
        self.assertEqual(conf["ollama_timeout_s"], 3.0)

        worker = conf["worker"]
        self.assertEqual(worker["batch_size"], 7)
        self.assertEqual(worker["poll_interval_s"], 1.5)
        self.assertEqual(worker["fetch_limit"], 99)
        self.assertEqual(worker["hourly_enabled"], False)

        sources = conf["sources"]
        self.assertEqual([s.name for s in sources], ["a", "b"])
        self.assertEqual(sources[0].table, "t_a")
        self.assertEqual(sources[1].table, "t_b")
        self.assertEqual(sources[1].text_column, "body")

    def test_resolve_sources_priority(self) -> None:
        # 验证：sources 的优先级为 环境变量 POMS_SOURCES_JSON > INI sources > 默认 sources。
        ini_sources = [poms_worker.SourceConfig(name="x", table="t", id_column="id", text_column="content")]
        with mock.patch.dict(os.environ, {"POMS_SOURCES_JSON": '[{"name":"y","table":"t2"}]'}, clear=False):
            resolved = poms_worker._resolve_sources(ini_sources)
            self.assertEqual([s.name for s in resolved], ["y"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

