# -*- coding: utf-8 -*-
"""回归守卫：2.5.2 / 2.5.6 / 2.5.8 的三处修复不能回退。"""

import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class PortFixesTests(unittest.TestCase):
    def setUp(self):
        def read(name):
            with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
                return handle.read()

        self.main = read("main.py")
        self.messages = read("store_messages.py")
        self.util = read("store_util.py")

    def test_time_window_uses_clock(self):
        self.assertNotIn("_dt.now()", self.messages)
        self.assertIn("now = _clock.now()", self.messages)
        self.assertIn("cls._epoch(", self.messages)

    def test_logger_does_not_fallback_to_logging(self):
        self.assertNotIn("import logging", self.util)
        self.assertIn("_NullLogger", self.util)

    def test_recall_runs_in_thread(self):
        self.assertIn("await asyncio.to_thread(self.store.recall", self.main)

    def test_relation_overlay(self):
        self.assertIn("profile_relation", self.main)
        self.assertIn('"相恋": "恋人"', self.main)


if __name__ == "__main__":
    unittest.main()
