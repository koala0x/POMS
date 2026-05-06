import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import poms.main as poms_worker


class TestHelpers(unittest.TestCase):
    def test_quote_ident_accepts_safe(self) -> None:
        # 验证：_quote_ident 只对“安全的 SQL 标识符”加双引号，不改变内容。
        self.assertEqual(poms_worker._quote_ident("abc"), '"abc"')
        self.assertEqual(poms_worker._quote_ident("_a1"), '"_a1"')

    def test_quote_ident_rejects_unsafe(self) -> None:
        # 验证：_quote_ident 会拒绝可能导致 SQL 注入或不合法的标识符。
        with self.assertRaises(ValueError):
            poms_worker._quote_ident('a";drop table x;--')

        with self.assertRaises(ValueError):
            poms_worker._quote_ident("a-b")

        with self.assertRaises(ValueError):
            poms_worker._quote_ident("1abc")

    def test_split_csv(self) -> None:
        # 验证：INI 中逗号分隔配置的解析行为：去空格、过滤空项。
        self.assertEqual(poms_worker._split_csv("a,b,c"), ["a", "b", "c"])
        self.assertEqual(poms_worker._split_csv(" a,  b , ,c "), ["a", "b", "c"])
        self.assertEqual(poms_worker._split_csv(""), [])

    def test_parse_bool(self) -> None:
        # 验证：布尔解析对常见真值/假值的兼容，并在未知输入时回退到 default。
        self.assertTrue(poms_worker._parse_bool("true", default=False))
        self.assertTrue(poms_worker._parse_bool("1", default=False))
        self.assertFalse(poms_worker._parse_bool("false", default=True))
        self.assertFalse(poms_worker._parse_bool("0", default=True))
        self.assertEqual(poms_worker._parse_bool("???", default=True), True)
        self.assertEqual(poms_worker._parse_bool("???", default=False), False)

    def test_floor_and_ceil_hour(self) -> None:
        # 验证：小时窗口边界计算的正确性（向下取整到整点、向上取整到下一个整点）。
        ts = datetime(2026, 5, 6, 12, 34, 56, 123, tzinfo=timezone.utc)
        self.assertEqual(poms_worker._floor_to_hour(ts), datetime(2026, 5, 6, 12, 0, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(poms_worker._ceil_to_next_hour(ts), datetime(2026, 5, 6, 13, 0, 0, 0, tzinfo=timezone.utc))

        ts2 = datetime(2026, 5, 6, 12, 0, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(poms_worker._floor_to_hour(ts2), ts2)
        self.assertEqual(poms_worker._ceil_to_next_hour(ts2), ts2)


class TestPrompts(unittest.TestCase):
    def test_build_batch_prompt_contains_items(self) -> None:
        # 验证：批量提示词会包含数据源信息、条数、编号列表与“请完成”指令。
        items = [
            {"id": 1, "created_at": "2026-05-06T00:00:00Z", "text": "foo"},
            {"id": 2, "created_at": None, "text": "bar"},
        ]
        prompt = poms_worker._build_batch_prompt("gmgn_twitter", items)
        self.assertIn("数据源：gmgn_twitter", prompt)
        self.assertIn("条数：2", prompt)
        self.assertIn("1.", prompt)
        self.assertIn("foo", prompt)
        self.assertIn("2.", prompt)
        self.assertIn("bar", prompt)
        self.assertIn("请完成：", prompt)

    def test_build_hourly_prompt_contains_summaries(self) -> None:
        # 验证：小时级提示词会包含时间窗口、条数、摘要内容与固定输出结构要求。
        window_start = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)
        window_end = datetime(2026, 5, 6, 13, 0, tzinfo=timezone.utc)
        summaries = [
            {"id": 10, "summary_text": "s1", "created_at": "t1", "source": "a"},
            {"id": 11, "summary_text": "s2", "created_at": None, "source": "b"},
        ]
        prompt = poms_worker._build_hourly_prompt(window_start, window_end, summaries)
        self.assertIn("时间窗口：", prompt)
        self.assertIn("归纳条目数：2", prompt)
        self.assertIn("s1", prompt)
        self.assertIn("s2", prompt)
        self.assertIn("主线观点", prompt)


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


class TestWorkerLogic(unittest.TestCase):
    def test_run_sources_batches_and_keeps_remainder(self) -> None:
        # 验证：源表拉取到 4 条、batch_size=3 时，只会处理 1 个 batch，剩余 1 条保留在缓冲区。
        db = object()
        ollama = object()
        source = poms_worker.SourceConfig(name="s", table="t", id_column="id", text_column="content")
        worker = poms_worker.Worker(
            db=db,  # type: ignore[arg-type]
            ollama=ollama,  # type: ignore[arg-type]
            sources=[source],
            batch_size=3,
            poll_interval_s=0.0,
            fetch_limit=10,
            hourly_enabled=False,
        )

        items = [
            {"id": 1, "created_at": None, "text": "a"},
            {"id": 2, "created_at": None, "text": "b"},
            {"id": 3, "created_at": None, "text": "c"},
            {"id": 4, "created_at": None, "text": "d"},
        ]

        calls: list[list[dict]] = []

        def _capture(_source, batch):
            calls.append(list(batch))

        with mock.patch.object(poms_worker, "_state_get", return_value={}), mock.patch.object(
            poms_worker, "_state_set"
        ) as _state_set, mock.patch.object(poms_worker, "_fetch_new_rows", return_value=items), mock.patch.object(
            poms_worker.Worker, "_summarize_and_store", side_effect=_capture
        ):
            worker._run_sources_once()

        self.assertEqual(len(calls), 1)
        self.assertEqual([i["id"] for i in calls[0]], [1, 2, 3])
        self.assertEqual([i["id"] for i in worker._buffers["s"]], [4])
        _state_set.assert_called()

    def test_run_hourly_writes_state(self) -> None:
        # 验证：小时级二次提炼会写入 poms_state 的窗口进度（即使具体 DB/模型都被 mock 掉）。
        db = object()
        ollama = mock.Mock()
        ollama.chat.return_value = "hourly"
        source = poms_worker.SourceConfig(name="s", table="t", id_column="id", text_column="content")
        worker = poms_worker.Worker(
            db=db,  # type: ignore[arg-type]
            ollama=ollama,  # type: ignore[arg-type]
            sources=[source],
            batch_size=50,
            poll_interval_s=0.0,
            fetch_limit=200,
            hourly_enabled=True,
        )

        window_end = datetime(2026, 5, 6, 13, 0, tzinfo=timezone.utc)
        summaries = [{"id": 1, "summary_text": "x", "created_at": window_end, "source": "s"}]

        with mock.patch.object(poms_worker, "_state_get", return_value={"window_end": "2026-05-06T12:00:00+00:00"}), mock.patch.object(
            poms_worker, "_state_set"
        ) as state_set, mock.patch.object(
            poms_worker, "_select_summaries_for_window", return_value=summaries
        ), mock.patch.object(
            poms_worker, "_insert_hourly_summary", return_value=123
        ):
            worker._run_hourly_until(window_end)

        state_set.assert_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
