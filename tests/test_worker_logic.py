import unittest
from datetime import datetime, timezone
from unittest import mock

import poms.main as poms_worker


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

        with mock.patch.object(
            poms_worker, "_state_get", return_value={"window_end": "2026-05-06T12:00:00+00:00"}
        ), mock.patch.object(poms_worker, "_state_set") as state_set, mock.patch.object(
            poms_worker, "_select_summaries_for_window", return_value=summaries
        ), mock.patch.object(
            poms_worker, "_insert_hourly_summary", return_value=123
        ):
            worker._run_hourly_until(window_end)

        state_set.assert_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)

