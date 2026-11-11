# -*- coding: utf-8 -*-
"""时间检索（年 → 月日 → 小时 → 消息）的单测。"""

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import clock  # noqa: E402

from store import DataStore  # noqa: E402
from time_query import describe_span, parse_time_span  # noqa: E402

BOT = "bot1"
UID = "302578963"
UMO = "lark:FriendMessage:ou_cec66"
NOW = datetime(2026, 11, 9, 15, 30, 0, tzinfo=clock.get_zone("Asia/Shanghai"))


def _tmpdir() -> str:
    base = os.environ.get("AMO_DATA_TEST_TMP") or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(dir=base)


class _ClockZoneCase(unittest.TestCase):
    """每个用例把时钟拨到目标时区，跑完还原，别污染别的测试文件。"""

    def setUp(self):
        self._prev_tz = os.environ.get("TZ")
        if hasattr(time, "tzset"):
            os.environ["TZ"] = "Asia/Shanghai"
            time.tzset()
        clock.configure("Asia/Shanghai")

    def tearDown(self):
        clock.restore()
        if self._prev_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._prev_tz
        if hasattr(time, "tzset"):
            time.tzset()


class ParseTests(_ClockZoneCase):

    def test_empty_means_no_time(self):
        self.assertIsNone(parse_time_span("", NOW))
        self.assertIsNone(parse_time_span("你吃饭了吗", NOW))

    def test_today_yesterday(self):
        span = parse_time_span("今天", NOW)
        self.assertEqual(span["granularity"], "day")
        self.assertEqual(clock.from_timestamp(span["start"]).day, 9)
        span = parse_time_span("昨天", NOW)
        self.assertEqual(clock.from_timestamp(span["start"]).day, 8)
        span = parse_time_span("前天", NOW)
        self.assertEqual(clock.from_timestamp(span["start"]).day, 7)
        span = parse_time_span("大前天", NOW)
        self.assertEqual(clock.from_timestamp(span["start"]).day, 6)

    def test_yesterday_afternoon_is_hour_span(self):
        span = parse_time_span("昨天下午", NOW)
        self.assertEqual(span["granularity"], "hour")
        start = clock.from_timestamp(span["start"])
        end = clock.from_timestamp(span["end"])
        self.assertEqual((start.day, start.hour), (8, 13))
        self.assertEqual(end.hour, 18)

    def test_month_day_and_hour(self):
        span = parse_time_span("11月6号晚上10点", NOW)
        start = clock.from_timestamp(span["start"])
        self.assertEqual((start.month, start.day, start.hour), (11, 6, 22))
        self.assertEqual(span["granularity"], "hour")

    def test_full_year_month_day(self):
        span = parse_time_span("2026年11月6日", NOW)
        start = clock.from_timestamp(span["start"])
        self.assertEqual((start.year, start.month, start.day), (2026, 11, 6))

    def test_month_only(self):
        span = parse_time_span("上个月", NOW)
        start = clock.from_timestamp(span["start"])
        self.assertEqual((start.year, start.month), (2026, 10))

    def test_recent_days(self):
        span = parse_time_span("最近3天", NOW)
        delta = NOW.timestamp() - span["start"]
        self.assertAlmostEqual(delta / 86400.0, 3.0, places=1)

    def test_weekday(self):
        span = parse_time_span("上周三", NOW)
        start = clock.from_timestamp(span["start"])
        self.assertEqual(start.weekday(), 2)  # 周三
        # 上周三应该比"本周三"再早 7 天
        this_span = parse_time_span("这周三", NOW)
        gap = clock.from_timestamp(this_span["start"]) - start
        self.assertEqual(gap.days, 7)

    def test_describe(self):
        self.assertIn("2026-11-08", describe_span(parse_time_span("昨天", NOW)))
        self.assertIn("13:00", describe_span(parse_time_span("昨天下午", NOW)))


class HierarchyTests(_ClockZoneCase):
    def setUp(self):
        # 数据插件默认按 Asia/Shanghai 分天；CI 容器是 UTC，不能靠进程时区。
        # Linux 有 tzset 就把进程时区拨到上海；Windows 没有 tzset（C 运行库会
        # 把 IANA 名字当 UTC），靠 clock 显式换算。
        super().setUp()
        self._zone = clock.get_zone("Asia/Shanghai")
        self.tmp = _tmpdir()
        self.store = DataStore(os.path.join(self.tmp, "chat.db"))
        self.rows = [
            (datetime(2026, 11, 8, 14, 30), "昨天下午在商场逛"),
            (datetime(2026, 11, 8, 21, 10), "昨天晚上看了部电影"),
            (datetime(2026, 11, 7, 10, 5), "前天上午开会"),
            (datetime(2026, 11, 6, 22, 15), "六号晚上吃了牛杂"),
            (datetime(2026, 10, 20, 9, 0), "上个月在公园"),
        ]
        for moment, text in self.rows:
            stamp = moment.replace(tzinfo=self._zone).timestamp() if self._zone else moment.timestamp()
            self.store.add_message(BOT, UMO, UID, "我", "user", text,
                                   created_at=int(stamp))

    def tearDown(self):
        self.store.close()
        super().tearDown()

    def _now(self):
        return datetime(2026, 11, 9, 15, 30, 0, tzinfo=self._zone)

    def test_buckets_go_year_month_day_hour(self):
        self.assertEqual(list(self.store.time_buckets(BOT, umo=UMO)), ["2026"])
        months = self.store.time_buckets(BOT, umo=UMO, year="2026")
        self.assertEqual(sorted(months), ["10", "11"])
        days = self.store.time_buckets(BOT, umo=UMO, year="2026", month="11")
        self.assertEqual(sorted(days), ["06", "07", "08"])
        hours = self.store.time_buckets(BOT, umo=UMO, year="2026", month="11", day="08")
        self.assertEqual(sorted(hours), ["14", "21"])

    def test_yesterday_afternoon_returns_only_that_window(self):
        span = parse_time_span("昨天下午", self._now())
        rows, path = self.store.hierarchical_messages(BOT, span, umo=UMO)
        self.assertEqual([r["content"] for r in rows], ["昨天下午在商场逛"])
        self.assertIn("years", path)
        self.assertIn("months", path)
        self.assertIn("days", path)
        self.assertIn("hours", path)
        self.assertEqual(path["months"], {"11": 1})
        self.assertEqual(path["days"], {"08": 1})
        self.assertEqual(path["hours"], {"14": 1})

    def test_evening_returns_evening_messages(self):
        span = parse_time_span("六号晚上", self._now())
        rows, _ = self.store.hierarchical_messages(BOT, span, umo=UMO)
        self.assertEqual([r["content"] for r in rows], ["六号晚上吃了牛杂"])

    def test_month_span_returns_all_days(self):
        span = parse_time_span("上个月", self._now())
        rows, path = self.store.hierarchical_messages(BOT, span, umo=UMO)
        self.assertEqual([r["content"] for r in rows], ["上个月在公园"])
        self.assertEqual(path["messages"], 1)

    def test_messages_have_no_timestamp_prefix(self):
        span = parse_time_span("昨天", self._now())
        rows, _ = self.store.hierarchical_messages(BOT, span, umo=UMO)
        for row in rows:
            self.assertNotRegex(row["content"], r"^\d{4}-\d{2}-\d{2}")
        self.assertTrue(all(row["created_at"] for row in rows))

    def test_recent_days_label_shows_range(self):
        text = describe_span(parse_time_span("最近3天", NOW))
        self.assertIn("~", text, text)

    def test_month_only_returns_whole_month(self):
        for text in ("11月", "去年11月"):
            span = parse_time_span(text, NOW)
            self.assertEqual(span["granularity"], "month", text)
            self.assertGreaterEqual(span["end"] - span["start"], 28 * 86400, text)

    def test_noon_and_midnight(self):
        noon = parse_time_span("昨天中午12点", NOW)
        self.assertEqual(clock.from_timestamp(noon["start"]).hour, 12)
        midnight = parse_time_span("昨天晚上12点", NOW)
        self.assertEqual(clock.from_timestamp(midnight["start"]).hour, 0)
        self.assertEqual(clock.from_timestamp(midnight["start"]).day, 9)

    def test_whole_year_span_not_blocked_by_empty_january(self):
        # 「今年」从 1 月 1 日算起；1 月没聊天不代表全年没聊天（旧逻辑会在这里直接返回空）
        span = parse_time_span("今年", NOW)
        rows, path = self.store.hierarchical_messages(BOT, span, umo=UMO)
        self.assertEqual(len(rows), 5)
        self.assertEqual(path["years"], {"2026": 5})

    def test_span_across_month_keeps_later_days(self):
        # 「最近N天」跨月时，起始那几天完全可能没聊天；不能因为起始月空着就把后面有消息的日子一起丢掉
        start = int(datetime(2026, 10, 30).timestamp())
        end = int(datetime(2026, 11, 9).timestamp())
        span = {"start": start, "end": end, "label": "最近10天",
                "granularity": "day", "year": 2026, "month": 10, "day": 30}
        rows, path = self.store.hierarchical_messages(BOT, span, umo=UMO)
        self.assertEqual(path["days"], {})
        self.assertEqual(len(rows), 4)
        self.assertTrue(all("2026-11" in clock.from_timestamp(r["created_at"]).strftime("%Y-%m")
                            for r in rows))

    def test_empty_window_returns_empty(self):
        span = parse_time_span("2025年1月1日", NOW)
        rows, path = self.store.hierarchical_messages(BOT, span, umo=UMO)
        self.assertEqual(rows, [])
        self.assertEqual(path.get("years"), {})


if __name__ == "__main__":
    unittest.main()
