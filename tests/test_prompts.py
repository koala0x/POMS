import unittest
from datetime import datetime, timezone

import poms.main as poms_worker


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


if __name__ == "__main__":
    unittest.main(verbosity=2)

