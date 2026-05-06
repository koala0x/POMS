import unittest
from datetime import datetime, timezone

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
        self.assertEqual(
            poms_worker._floor_to_hour(ts), datetime(2026, 5, 6, 12, 0, 0, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(
            poms_worker._ceil_to_next_hour(ts), datetime(2026, 5, 6, 13, 0, 0, 0, tzinfo=timezone.utc)
        )

        ts2 = datetime(2026, 5, 6, 12, 0, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(poms_worker._floor_to_hour(ts2), ts2)
        self.assertEqual(poms_worker._ceil_to_next_hour(ts2), ts2)


if __name__ == "__main__":
    unittest.main(verbosity=2)

