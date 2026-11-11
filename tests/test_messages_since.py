# -*- coding: utf-8 -*-
"""增量聊天接口（messages_since）：里程碑归档依赖它。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import DataStore  # noqa: E402

BOT = "b"
UID = "302578963"


def _tmpdir() -> str:
    base = os.environ.get("AMO_DATA_TEST_TMP") or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(dir=base)


class MessagesSinceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir()
        self.store = DataStore(os.path.join(self.tmp, "a.db"))

    def tearDown(self):
        self.store.close()

    def _add(self, role, text, ts):
        self.store.add_message(BOT, "aiocqhttp:FriendMessage:" + UID, UID, "family",
                               "user" if role == "user" else "bot", text, created_at=ts)

    def test_filters_by_time_and_orders_ascending(self):
        base = 1_800_000_000
        self._add("user", "旧消息", base)
        self._add("bot", "第二条", base + 60)
        self._add("user", "新消息", base + 120)
        rows = self.store.messages_since(BOT, UID, since_ts=base + 30)
        self.assertEqual([r["content"] for r in rows], ["第二条", "新消息"])
        self.assertEqual([r["role"] for r in rows], ["bot", "user"])

    def test_zero_since_returns_from_start(self):
        base = 1_800_000_000
        self._add("user", "一", base)
        self._add("user", "二", base + 10)
        rows = self.store.messages_since(BOT, UID, since_ts=0)
        self.assertEqual(len(rows), 2)

    def test_isolated_by_uid(self):
        base = 1_800_000_000
        self._add("user", "我的", base)
        self.store.add_message(BOT, "aiocqhttp:FriendMessage:other", "other", "他", "user",
                               "别人的", created_at=base + 10)
        rows = self.store.messages_since(BOT, UID)
        self.assertEqual([r["content"] for r in rows], ["我的"])

    def test_limit(self):
        base = 1_800_000_000
        for i in range(5):
            self._add("user", "第%d条" % i, base + i)
        rows = self.store.messages_since(BOT, UID, limit=3)
        self.assertEqual(len(rows), 3)


if __name__ == "__main__":
    unittest.main()
