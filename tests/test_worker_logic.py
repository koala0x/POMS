"""
Worker 核心逻辑的单元测试（使用 mock）

目标：
- 不连真实 PostgreSQL：用 mock 替代 _fetch_new_rows / _state_get / _state_set 等函数
- 不连真实 Ollama：用 mock 替代 chat()
- 只验证 Worker 的控制流和“缓冲/批处理/状态推进”的行为是否符合预期

运行方式（标准包安装后）：
- python -m unittest -v tests.test_worker_logic
"""

import unittest
from datetime import datetime, timezone
from unittest import mock

import poms.main as poms_worker


class TestWorkerLogic(unittest.TestCase):
    """
    这里的测试关注“流程正确性”，而不是外部系统是否可用。
    外部系统（DB / Ollama）是否能连通，应当由 check-db 或集成测试来覆盖。
    """

    def test_run_sources_batches_and_keeps_remainder(self) -> None:
        # 验证：源表拉取到 4 条、batch_size=3 时，只会处理 1 个 batch，剩余 1 条保留在缓冲区。
        # 这里用 object() 占位 db/ollama，因为我们会 mock 掉所有会触达外部的函数。
        db = object()
        ollama = object()

        # 构造一个最小的数据源描述（表名/列名只是形式正确即可）。
        source = poms_worker.SourceConfig(name="s", table="t", id_column="id", text_column="content")

        # batch_size=3：意味着缓冲里凑够 3 条才会触发一次归纳。
        worker = poms_worker.Worker(
            db=db,  # type: ignore[arg-type]
            ollama=ollama,  # type: ignore[arg-type]
            sources=[source],
            batch_size=3,
            poll_interval_s=0.0,
            fetch_limit=10,
            hourly_enabled=False,
        )

        # 模拟一次拉取到 4 条新数据：应该处理 3 条，剩余 1 条留在缓冲区等待下一轮。
        items = [
            {"id": 1, "created_at": None, "text": "a"},
            {"id": 2, "created_at": None, "text": "b"},
            {"id": 3, "created_at": None, "text": "c"},
            {"id": 4, "created_at": None, "text": "d"},
        ]

        # 用来捕获 Worker 实际送去归纳的 batch 内容。
        calls: list[list[dict]] = []

        def _capture(_source, batch):
            calls.append(list(batch))

        # mock 掉外部依赖：
        # - _state_get/_state_set：避免真实写库，且让流程能更新 last_id
        # - _fetch_new_rows：返回我们构造的 items
        # - Worker._summarize_and_store：不调用模型/不写归纳表，只记录 batch
        with mock.patch.object(poms_worker, "_state_get", return_value={}), mock.patch.object(
            poms_worker, "_state_set"
        ) as _state_set, mock.patch.object(poms_worker, "_fetch_new_rows", return_value=items), mock.patch.object(
            poms_worker.Worker, "_summarize_and_store", side_effect=_capture
        ):
            worker._run_sources_once()

        # 断言：只处理了 1 个 batch
        self.assertEqual(len(calls), 1)
        # 断言：batch 恰好是前 3 条
        self.assertEqual([i["id"] for i in calls[0]], [1, 2, 3])
        # 断言：剩余第 4 条还留在缓冲里
        self.assertEqual([i["id"] for i in worker._buffers["s"]], [4])
        # 断言：状态写入被调用过（证明 last_id 推进）
        _state_set.assert_called()

    def test_run_hourly_writes_state(self) -> None:
        # 验证：小时级二次提炼会写入 poms_state 的窗口进度（即使具体 DB/模型都被 mock 掉）。
        # db 依旧用占位对象；ollama 用 mock，提供 chat() 返回值。
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

        # 这里模拟“当前要处理到 13:00 整点”，并假设上次处理到 12:00。
        window_end = datetime(2026, 5, 6, 13, 0, tzinfo=timezone.utc)
        # 模拟该小时内已有 1 条批次归纳，小时归纳会把它再压缩。
        summaries = [{"id": 1, "summary_text": "x", "created_at": window_end, "source": "s"}]

        # mock 掉外部依赖：
        # - _state_get：返回上次窗口结束时间
        # - _select_summaries_for_window：返回 summaries（否则会是空）
        # - _insert_hourly_summary：避免写库，直接返回一个假 id
        # - _state_set：我们要验证它一定会被调用（推进窗口）
        with mock.patch.object(
            poms_worker, "_state_get", return_value={"window_end": "2026-05-06T12:00:00+00:00"}
        ), mock.patch.object(poms_worker, "_state_set") as state_set, mock.patch.object(
            poms_worker, "_select_summaries_for_window", return_value=summaries
        ), mock.patch.object(
            poms_worker, "_insert_hourly_summary", return_value=123
        ):
            worker._run_hourly_until(window_end)

        # 断言：窗口进度一定会被写回 state（否则重启后会重复做同一小时）
        state_set.assert_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
