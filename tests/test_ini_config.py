"""
INI 配置读取相关的单元测试

目标：
- 验证 config/poms_worker.ini 的解析逻辑：字段名、默认值、过滤规则稳定。
- 不依赖真实数据库、不依赖真实文件路径（使用临时文件）。

运行方式（标准包安装后）：
- python -m unittest -v tests.test_ini_config
"""

import os
import tempfile
import unittest
from unittest import mock

import poms.main as poms_worker


class TestIniConfig(unittest.TestCase):
    """
    这里测试两件事：
    1) _load_ini_config：把 ini 文件读成一个结构化 dict
    2) _resolve_sources：在“环境变量 vs ini vs 默认值”之间如何选择 sources
    """

    def test_load_ini_config_sources_and_worker(self) -> None:
        # 验证：从 INI 文件读取配置时，能正确解析 postgres/ollama/worker 以及 source.* 段落。
        # 注意：这里使用“最小但完整”的 ini，覆盖所有段落。
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
        # 写入临时文件，模拟真实的 config/poms_worker.ini
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as f:
            f.write(ini)
            path = f.name

        try:
            # 调用解析函数，得到结构化结果
            conf = poms_worker._load_ini_config(path)
        finally:
            # 清理临时文件，避免测试污染文件系统
            os.unlink(path)

        # 断言：postgres / ollama 段落解析正确
        self.assertEqual(conf["pg_dsn"], "postgresql://u:p@127.0.0.1:5432/db")
        self.assertEqual(conf["ollama_base_url"], "http://localhost:11434")
        self.assertEqual(conf["ollama_model"], "qwen2.5")
        self.assertEqual(conf["ollama_timeout_s"], 3.0)

        # 断言：worker 段落解析正确（包括布尔 hourly_enabled）
        worker = conf["worker"]
        self.assertEqual(worker["batch_size"], 7)
        self.assertEqual(worker["poll_interval_s"], 1.5)
        self.assertEqual(worker["fetch_limit"], 99)
        self.assertEqual(worker["hourly_enabled"], False)

        # 断言：sources 列表能按 [sources] enabled 过滤，并正确读取每个 source.* 段落
        sources = conf["sources"]
        self.assertEqual([s.name for s in sources], ["a", "b"])
        self.assertEqual(sources[0].table, "t_a")
        self.assertEqual(sources[1].table, "t_b")
        self.assertEqual(sources[1].text_column, "body")

    def test_resolve_sources_priority(self) -> None:
        # 验证：sources 的优先级为 环境变量 POMS_SOURCES_JSON > INI sources > 默认 sources。
        # 给一个 ini_sources 作为“备选”，然后用环境变量覆盖它。
        ini_sources = [poms_worker.SourceConfig(name="x", table="t", id_column="id", text_column="content")]
        with mock.patch.dict(os.environ, {"POMS_SOURCES_JSON": '[{"name":"y","table":"t2"}]'}, clear=False):
            # _resolve_sources 会优先走环境变量（也就是 _load_sources_from_env 解析 JSON）
            resolved = poms_worker._resolve_sources(ini_sources)
            self.assertEqual([s.name for s in resolved], ["y"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
