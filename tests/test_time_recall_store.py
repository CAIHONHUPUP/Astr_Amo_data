# -*- coding: utf-8 -*-
"""时间回看接口（recall_messages / messages_since）的单测。

这两个方法之前不存在，amo 侧 callable 检查直接 return，导致
「时间回看 / 每日总结 / 里程碑」三个功能静默失效。
"""

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import DataStore  # noqa: E402


def _ts(dt):
    return int(dt.timestamp())


class TimeWindowTests(unittest.TestCase):
    def test_yesterday_whole_day(self):
        yesterday = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        start, end = DataStore._time_window("昨天我们聊了啥")
        self.assertEqual(start, _ts(yesterday))
        self.assertEqual(end, _ts(yesterday + timedelta(days=1)))

    def test_yesterday_afternoon_is_narrowed(self):
        # 「昨天下午」不能把上午的聊天也当成下午塞进提示词
        yesterday = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        start, end = DataStore._time_window("昨天下午")
        self.assertEqual(start, _ts(yesterday + timedelta(hours=12)))
        self.assertEqual(end, _ts(yesterday + timedelta(hours=18)))

    def test_last_night(self):
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        start, end = DataStore._time_window("昨晚我们聊了什么")
        self.assertEqual(start, _ts(today - timedelta(days=1) + timedelta(hours=18)))
        self.assertEqual(end, _ts(today))
        self.assertEqual(DataStore._time_label("昨晚我们聊了什么"), "昨晚")

    def test_last_weekday(self):
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        monday = today - timedelta(days=today.weekday())
        start, end = DataStore._time_window("还记得上周三我说的事吗")
        self.assertEqual(end - start, 86400)
        self.assertEqual(datetime.fromtimestamp(start).weekday(), 2)  # 周三
        self.assertLess(start, _ts(monday))

    def test_recent_days(self):
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        start, end = DataStore._time_window("最近3天我提过什么")
        self.assertEqual(start, _ts(today - timedelta(days=2)))
        self.assertEqual(end, _ts(today + timedelta(days=1)))

    def test_unknown_returns_zero(self):
        self.assertEqual(DataStore._time_window("你在干嘛"), (0, 0))

    def test_label_matches_window(self):
        self.assertEqual(DataStore._time_label("昨天下午我们聊了啥"), "昨天下午")
        self.assertEqual(DataStore._time_label("最近3天我提过什么"), "最近3天")
        self.assertTrue(DataStore._time_label("还记得上周三我说的事吗").startswith("上周三"))


class RecallMessagesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = DataStore(os.path.join(self.tmp, "amo_data.db"))
        self.bot = "bot1"
        self.uid = "10001"
        self.umo = "aiocqhttp:FriendMessage:10001"
        today0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday0 = today0 - timedelta(days=1)
        self._add("昨天的午饭", _ts(yesterday0 + timedelta(hours=11)))
        self._add("昨天下午的班", _ts(yesterday0 + timedelta(hours=14)))
        self._add("今天的消息", _ts(today0 + timedelta(hours=9)))

    def _add(self, text, created_at):
        mid = self.store.add_message(self.bot, self.umo, self.uid, "他", "user", text)
        with self.store.lock:
            self.store.conn.execute("UPDATE messages SET created_at=? WHERE id=?", (created_at, mid))
            self.store.conn.commit()

    def tearDown(self):
        self.store.close()

    def test_recall_only_returns_window_and_ascending(self):
        rows = self.store.recall_messages(self.bot, query="昨天", uid=self.uid)
        self.assertEqual([r["content"] for r in rows], ["昨天的午饭", "昨天下午的班"])

    def test_recall_afternoon_filters_morning(self):
        rows = self.store.recall_messages(self.bot, query="昨天下午", uid=self.uid)
        self.assertEqual([r["content"] for r in rows], ["昨天下午的班"])

    def test_recall_unknown_time_returns_empty(self):
        self.assertEqual(self.store.recall_messages(self.bot, query="你在干嘛", uid=self.uid), [])

    def test_recall_respects_limit(self):
        rows = self.store.recall_messages(self.bot, query="昨天", uid=self.uid, limit=1)
        self.assertEqual([r["content"] for r in rows], ["昨天下午的班"])

    def test_recall_isolated_by_bot(self):
        self._add("别人的消息", int(time.time()) - 86400)
        other = self.store.recall_messages("bot2", query="昨天", uid=self.uid)
        self.assertEqual(other, [])

    def test_messages_since(self):
        since = _ts(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0))
        rows = self.store.messages_since(self.bot, uid=self.uid, since=since)
        self.assertEqual([r["content"] for r in rows], ["今天的消息"])


if __name__ == "__main__":
    unittest.main()
