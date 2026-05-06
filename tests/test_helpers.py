"""
helpers 相关的单元测试

目标：
- 这些测试只覆盖“纯函数/纯逻辑”，不依赖数据库、不依赖 Ollama。
- 这样即使你还没把 Postgres / Ollama 配好，依然可以快速验证基础逻辑没被改坏。

运行方式（标准包安装后）：
- python -m unittest -v tests.test_helpers
"""

import unittest
from datetime import datetime, timezone

import src.poms.main as poms_worker


class TestHelpers(unittest.TestCase):
    """
    这里集中测试 poms.main 里的一些小工具函数：
    - SQL 标识符安全处理
    - INI 配置解析的通用辅助
    - 时间窗口（整点）计算
    """

    def test_quote_ident_accepts_safe(self) -> None:
        # 验证：_quote_ident 只对“安全的 SQL 标识符”加双引号，不改变内容。
        # 典型输入：表名/列名/键名这种只包含字母数字下划线的标识符。
        self.assertEqual(poms_worker._quote_ident("abc"), '"abc"')
        self.assertEqual(poms_worker._quote_ident("_a1"), '"_a1"')

    def test_quote_ident_rejects_unsafe(self) -> None:
        # 验证：_quote_ident 会拒绝可能导致 SQL 注入或不合法的标识符。
        # 这里覆盖三类常见风险：
        # 1) 引号/分号等拼接 SQL 的注入符号
        # 2) 包含连字符等不符合“标识符”约束的字符
        # 3) 以数字开头（不是合法 SQL 标识符）
        with self.assertRaises(ValueError):
            poms_worker._quote_ident('a";drop table x;--')

        with self.assertRaises(ValueError):
            poms_worker._quote_ident("a-b")

        with self.assertRaises(ValueError):
            poms_worker._quote_ident("1abc")

    def test_split_csv(self) -> None:
        # 验证：INI 中逗号分隔配置的解析行为：去空格、过滤空项。
        # enabled=a,b,c -> ["a","b","c"]
        self.assertEqual(poms_worker._split_csv("a,b,c"), ["a", "b", "c"])
        # 允许用户写多余空格和多余逗号，解析结果应当干净。
        self.assertEqual(poms_worker._split_csv(" a,  b , ,c "), ["a", "b", "c"])
        # 空串应当返回空列表。
        self.assertEqual(poms_worker._split_csv(""), [])

    def test_parse_bool(self) -> None:
        # 验证：布尔解析对常见真值/假值的兼容，并在未知输入时回退到 default。
        # 常见 true 表达
        self.assertTrue(poms_worker._parse_bool("true", default=False))
        self.assertTrue(poms_worker._parse_bool("1", default=False))
        # 常见 false 表达
        self.assertFalse(poms_worker._parse_bool("false", default=True))
        self.assertFalse(poms_worker._parse_bool("0", default=True))
        # 任何无法识别的值，都应该回退到 default（保证配置容错）。
        self.assertEqual(poms_worker._parse_bool("???", default=True), True)
        self.assertEqual(poms_worker._parse_bool("???", default=False), False)

    def test_floor_and_ceil_hour(self) -> None:
        # 验证：小时窗口边界计算的正确性（向下取整到整点、向上取整到下一个整点）。
        # 用一个不是整点的时间（12:34:56.000123）来验证：
        # - floor -> 12:00:00
        # - ceil  -> 13:00:00
        ts = datetime(2026, 5, 6, 12, 34, 56, 123, tzinfo=timezone.utc)
        self.assertEqual(
            poms_worker._floor_to_hour(ts), datetime(2026, 5, 6, 12, 0, 0, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(
            poms_worker._ceil_to_next_hour(ts), datetime(2026, 5, 6, 13, 0, 0, 0, tzinfo=timezone.utc)
        )

        # 再用一个“刚好整点”的时间验证：floor 和 ceil 都不应该改变它。
        ts2 = datetime(2026, 5, 6, 12, 0, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(poms_worker._floor_to_hour(ts2), ts2)
        self.assertEqual(poms_worker._ceil_to_next_hour(ts2), ts2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
