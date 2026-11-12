# -*- coding: utf-8 -*-
"""补覆盖率：时间解析分支、时钟兜底、快照导入失败路径。"""

import io
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import clock  # noqa: E402
import time_query as tq  # noqa: E402
from store import DataStore  # noqa: E402

BOT = "bot1"
UID = "302578963"
NOW = datetime(2026, 11, 12, 15, 30)  # 周四


class ParseSpanTests(unittest.TestCase):
    def parse(self, text):
        return tq.parse_time_span(text, now=NOW)

    def test_recent_units(self):
        span = self.parse("最近3小时")
        self.assertEqual(span["granularity"], "hour")
        span = self.parse("最近30分钟")
        self.assertEqual(span["granularity"], "hour")
        span = self.parse("最近2天")
        self.assertEqual(span["granularity"], "day")

    def test_weekday_future_rolls_back(self):
        span = self.parse("周日")
        self.assertEqual(span["day"], 8)  # 11-15 是未来，回退到 11-08

    def test_weekday_prefixes(self):
        self.assertEqual(self.parse("上周三")["day"], 4)
        self.assertEqual(self.parse("上上周三")["day"], 28)

    def test_year_tokens(self):
        span = self.parse("去年")
        self.assertEqual(span["granularity"], "year")
        self.assertEqual(clock.from_timestamp(span["start"]).year, 2025)
        self.assertEqual(clock.from_timestamp(self.parse("前年")["start"]).year, 2024)
        self.assertEqual(clock.from_timestamp(self.parse("今年")["start"]).year, 2026)

    def test_month_and_day(self):
        span = self.parse("11月")
        self.assertEqual(span["granularity"], "month")
        self.assertEqual(clock.from_timestamp(span["start"]).month, 11)
        span = self.parse("11月6号")
        self.assertEqual(span["granularity"], "day")
        self.assertEqual(span["day"], 6)
        span = self.parse("6号")
        self.assertEqual(span["day"], 6)

    def test_month_aliases(self):
        span = self.parse("上个月")
        self.assertEqual(clock.from_timestamp(span["start"]).month, 10)
        span = self.parse("这个月")
        self.assertEqual(clock.from_timestamp(span["start"]).month, 11)

    def test_hour_periods(self):
        self.assertEqual(self.parse("中午12点")["hour"], 12)
        self.assertEqual(self.parse("晚上12点")["hour"], 0)
        self.assertEqual(self.parse("深夜1点")["hour"], 1)
        self.assertEqual(self.parse("深夜11点")["hour"], 23)
        self.assertEqual(self.parse("下午3点")["hour"], 15)
        self.assertEqual(self.parse("凌晨12点")["hour"], 0)

    def test_period_only_when_current(self):
        span = self.parse("下午")
        self.assertEqual(span["granularity"], "hour")
        self.assertIsNone(self.parse("凌晨"))

    def test_invalid_date_returns_none(self):
        self.assertIsNone(self.parse("2月30号"))

    def test_no_time_returns_none(self):
        self.assertIsNone(self.parse("随便聊聊"))
        self.assertIsNone(self.parse(""))

    def test_describe(self):
        self.assertEqual(tq.describe_span(None), "没提时间")
        self.assertIn("年", tq.describe_span(self.parse("去年")))
        self.assertIn("月", tq.describe_span(self.parse("11月")))
        self.assertIn(":", tq.describe_span(self.parse("11月6号下午3点")))
        self.assertIn("~", tq.describe_span(self.parse("最近3天")))
        self.assertIn("-", tq.describe_span(self.parse("11月6号")))

    def test_cn_value(self):
        self.assertEqual(tq._cn_value("六"), 6)
        self.assertEqual(tq._cn_value("十一"), 11)
        self.assertEqual(tq._cn_value("二十三"), 23)
        self.assertEqual(tq._cn_value(""), -1)
        self.assertEqual(tq._cn_value("甲"), -1)
        self.assertEqual(tq._cn_value("十十"), -1)
        self.assertEqual(tq._cn_value("十甲"), -1)

    def test_normalize_numbers(self):
        self.assertEqual(tq.normalize_numbers("六号"), "6号")
        self.assertEqual(tq.normalize_numbers("没数字"), "没数字")


class ClockExtraTests(unittest.TestCase):
    def test_zone_fallback_without_zoneinfo(self):
        original = clock.ZoneInfo
        clock.ZoneInfo = None
        try:
            zone = clock.get_zone("Asia/Shanghai")
            self.assertIsNotNone(zone)
            self.assertIsNone(clock.get_zone("Not/AZone"))
        finally:
            clock.ZoneInfo = original

    def test_now_and_offset_with_unknown_zone(self):
        self.assertIsNotNone(clock.now("Not/AZone"))
        self.assertIsInstance(clock.utc_offset_minutes("Not/AZone"), int)
        self.assertIsNotNone(clock.from_timestamp(0, "Not/AZone"))

    def test_etc_timezone_read(self):
        with mock.patch("builtins.open", mock.mock_open(read_data="Asia/Tokyo\n")):
            self.assertEqual(clock._read_etc_timezone(), "Asia/Tokyo")

    def test_restore_with_previous_tz(self):
        clock._applied.update({"ok": True, "name": "Asia/Shanghai", "prev": "UTC"})
        original = clock._time
        clock._time = mock.Mock()
        try:
            clock.restore()
        finally:
            clock._time = original
        self.assertEqual(os.environ.get("TZ"), "UTC")
        os.environ.pop("TZ", None)

    def test_applied_false_is_safe(self):
        self.assertFalse(clock.applied())


class ImportFailureTests(unittest.TestCase):
    def setUp(self):
        self.store = DataStore(os.path.join(os.path.dirname(__file__), "..",
                                            ".test-tmp", "import-edge.db"))

    def tearDown(self):
        self.store.close()

    def test_import_failures_are_counted(self):
        def boom(*args, **kwargs):
            raise RuntimeError("boom")

        original = (self.store.remember, self.store.save_profile, self.store.save_relation,
                    self.store.resolve_person, self.store.set_identity_alias)
        self.store.remember = boom
        self.store.save_profile = boom
        self.store.save_relation = boom
        self.store.resolve_person = boom
        self.store.set_identity_alias = boom
        try:
            counts = self.store.import_snapshot({
                "bot_id": BOT,
                "memories": [{"uid": UID, "content": "一句话"}],
                "profiles": [{"uid": UID, "name": "阿明"}],
                "relations": [{"a_uid": UID, "b_uid": "u2"}],
                "person_links": [{"platform": "lark", "uid": UID}],
                "identity_alias": [{"platform": "lark", "uid": UID}],
            })
        finally:
            (self.store.remember, self.store.save_profile, self.store.save_relation,
             self.store.resolve_person, self.store.set_identity_alias) = original
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["profiles"], 0)

    def test_stats_without_bot_filter(self):
        self.store.remember(BOT, UID, "他喜欢茶")
        stats = self.store.stats("")
        self.assertGreaterEqual(stats["memories"], 1)


if __name__ == "__main__":
    unittest.main()
