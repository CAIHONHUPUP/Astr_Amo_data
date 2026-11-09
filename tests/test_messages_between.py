# -*- coding: utf-8 -*-
"""messages_between：按时间段取聊天（日记用）。"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import DataStore  # noqa: E402

BOT = "大肥鱼"
UID = "302578963"


class MessagesBetweenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="amo-msgs-")
        self.store = DataStore(os.path.join(self.tmp, "a.db"))
        for i in range(5):
            self.store.add_message(BOT, "u", UID, "他", "user", "第%d条" % i,
                                   created_at=1000 + i * 10)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_range_filters(self):
        rows = self.store.messages_between(BOT, 1010, 1030)
        self.assertEqual([r["content"] for r in rows], ["第1条", "第2条"])

    def test_uid_filter(self):
        self.store.add_message(BOT, "b", "470488586", "她", "bot", "她的回复", created_at=1015)
        rows = self.store.messages_between(BOT, 1000, 1100, uid=UID)
        self.assertEqual(len(rows), 5)
        rows_all = self.store.messages_between(BOT, 1000, 1100)
        self.assertEqual(len(rows_all), 6)


if __name__ == "__main__":
    unittest.main()
