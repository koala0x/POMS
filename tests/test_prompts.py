"""
提示词（Prompt）拼装相关的单元测试

目标：
- 验证“喂给大模型的输入”拼装逻辑稳定：字段齐全、关键结构不被无意修改。
- 这些测试不调用 Ollama，仅对字符串内容做断言，因此运行非常快。

运行方式（标准包安装后）：
- python -m unittest -v tests.test_prompts
"""

import unittest
from datetime import datetime, timezone

import poms.main as poms_worker


class TestPrompts(unittest.TestCase):
    """
    这里测试两类提示词：
    1) 批次归纳提示词（每 batch_size 条原始数据一次）
    2) 小时级二次归纳提示词（把一小时内的归纳再压缩）
    """

    def test_build_batch_prompt_contains_items(self) -> None:
        # 验证：批量提示词会包含数据源信息、条数、编号列表与“请完成”指令。
        # 这里构造两条模拟数据：一条有 created_at，一条没有 created_at。
        items = [
            {"id": 1, "created_at": "2026-05-06T00:00:00Z", "text": "foo"},
            {"id": 2, "created_at": None, "text": "bar"},
        ]
        # 调用拼装函数生成 prompt（这是实际发给大模型的 user prompt）。
        prompt = poms_worker._build_batch_prompt("gmgn_twitter", items)

        # 断言：基础元信息必须包含
        self.assertIn("数据源：gmgn_twitter", prompt)
        self.assertIn("条数：2", prompt)

        # 断言：编号列表和正文必须出现（防止把循环或格式改坏）
        self.assertIn("1.", prompt)
        self.assertIn("foo", prompt)
        self.assertIn("2.", prompt)
        self.assertIn("bar", prompt)

        # 断言：任务指令区存在（否则大模型输出可能发散）
        self.assertIn("请完成：", prompt)

    def test_build_hourly_prompt_contains_summaries(self) -> None:
        # 验证：小时级提示词会包含时间窗口、条数、摘要内容与固定输出结构要求。
        # 这里用固定 UTC 时间窗口，保证测试结果可重复。
        window_start = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)
        window_end = datetime(2026, 5, 6, 13, 0, tzinfo=timezone.utc)

        # 构造两条归纳数据：一条带 created_at，一条不带，用来覆盖格式分支。
        summaries = [
            {"id": 10, "summary_text": "s1", "created_at": "t1", "source": "a"},
            {"id": 11, "summary_text": "s2", "created_at": None, "source": "b"},
        ]
        prompt = poms_worker._build_hourly_prompt(window_start, window_end, summaries)

        # 断言：必须包含窗口信息与条数
        self.assertIn("时间窗口：", prompt)
        self.assertIn("归纳条目数：2", prompt)

        # 断言：归纳内容必须被拼进 prompt
        self.assertIn("s1", prompt)
        self.assertIn("s2", prompt)

        # 断言：输出结构要求必须存在（保证二次归纳格式稳定）
        self.assertIn("主线观点", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
