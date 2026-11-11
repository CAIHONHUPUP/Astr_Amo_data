# -*- coding: utf-8 -*-
"""时区测试：容器跑 UTC 时，插件必须按配置时区算时间。

线上问题：AstrBot 容器 TZ=UTC，北京时间 10:00 时 ``datetime.now()`` 拿到 02:00，
于是她说「大晚上的」；日志里 20:00 UTC（北京 04:00）也打成了「天气提醒（晚上）」。

修复方式是进程级的：``clock.configure()`` 设置 ``TZ`` 并 ``tzset()``，
所以 ``datetime.now()``、``.timestamp()``、SQLite ``'localtime'`` 会一起对齐。
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import clock  # noqa: E402


@unittest.skipUnless(hasattr(time, "tzset"), "Windows 没有 tzset，进程级时区改不了")
class ClockTests(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"   # 模拟线上 AstrBot 容器
        time.tzset()
        clock.configure("UTC")

    def tearDown(self):
        clock.restore()
        if self._prev is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._prev
        time.tzset()

    def test_configure_moves_process_clock_by_eight_hours(self):
        before = datetime.now()
        self.assertEqual(clock.configure("Asia/Shanghai"), "Asia/Shanghai")
        after = datetime.now()
        self.assertAlmostEqual((after - before).total_seconds(), 8 * 3600, delta=5)

    def test_now_agrees_with_process_clock(self):
        clock.configure("Asia/Shanghai")
        self.assertLess(abs((clock.now() - datetime.now()).total_seconds()), 2)

    def test_timestamp_conversion_is_aligned(self):
        """关键：naive 时间的 .timestamp() 必须按目标时区解释（SQLite/时间检索都靠它）。"""
        clock.configure("Asia/Shanghai")
        local = datetime(2026, 11, 10, 10, 0, 0)
        expected = datetime(2026, 11, 10, 2, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(int(local.timestamp()), int(expected), "北京 10 点应等于 UTC 02 点")

    def test_sql_offset_modifier(self):
        clock.configure("Asia/Shanghai")
        self.assertEqual(clock.utc_offset_minutes(), 480)
        self.assertEqual(clock.sql_offset_modifier(), "+480 minutes")

    def test_from_timestamp_uses_target_zone(self):
        clock.configure("Asia/Shanghai")
        ts = datetime(2026, 11, 10, 2, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(clock.from_timestamp(ts).hour, 10)

    def test_auto_follows_system(self):
        clock.configure("auto")
        self.assertTrue(clock.resolve_name(), "auto 也要解析出具体时区")

    def test_restore_puts_timezone_back(self):
        clock.configure("Asia/Shanghai")
        clock.restore()
        self.assertEqual(os.environ.get("TZ"), "UTC")  # 回到 setUp 里设的 UTC


class SourceGuardTests(unittest.TestCase):
    def test_no_bare_localtime_or_now(self):
        names = ["store.py", "time_query.py", "main.py", "store_util.py", "store_schema.py",
                 "store_people.py", "store_memory.py", "store_messages.py", "store_logs.py"]
        for name in names:
            with open(os.path.join(ROOT, name), encoding="utf-8") as fp:
                body = fp.read()
            self.assertEqual(body.count("'localtime'"), 0, name + " 还在用 SQLite 的 'localtime'")
            self.assertEqual(body.count("datetime.now()"), 0, name + " 还残留裸 datetime.now()")
            self.assertEqual(body.count("date.today()"), 0, name + " 还残留 date.today()")

    def test_schema_default_is_shanghai(self):
        with open(os.path.join(ROOT, "_conf_schema.json"), encoding="utf-8") as fp:
            schema = json.load(fp)
        self.assertEqual(schema["timezone"]["default"], "Asia/Shanghai")


@unittest.skipIf(hasattr(time, "tzset"), "只在没有 tzset 的平台（Windows）验证")
class NoTzsetTests(unittest.TestCase):
    def test_configure_does_not_poison_tz(self):
        """Windows 上写 TZ=Asia/Shanghai 会让 CRT 把本地时间当 UTC，绝不能写。"""
        prev = os.environ.get("TZ")
        try:
            clock.configure("Asia/Shanghai")
            self.assertEqual(os.environ.get("TZ"), prev)
            self.assertFalse(clock.applied())
            expected = datetime.now(clock.get_zone("Asia/Shanghai")).replace(tzinfo=None)
            self.assertLess(abs((clock.now() - expected).total_seconds()), 60)
        finally:
            clock.restore()
            if prev is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = prev


if __name__ == "__main__":
    unittest.main()
