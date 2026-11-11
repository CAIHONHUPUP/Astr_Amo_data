# -*- coding: utf-8 -*-
"""聊天列表顺序：按「真实聊天时间」倒序，不是按插入顺序。

线上 2026-11-11 的面板里，8 号的摘要排在 11 号聊天上面：摘要行是当天事后补写的
（id 更大），而它的 created_at 是 8 号。只按 id 排就会把老摘要顶到最前面。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import DataStore  # noqa: E402

BOT = "bot1"
UMO = "lark:FriendMessage:ou_test"
UID = "302578963"


class MessageOrderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = DataStore(os.path.join(self.tmp, "m.db"))

    def tearDown(self):
        self.store.close()

    def test_list_messages_sorted_by_chat_time_desc(self):
        # 先写今天的对话
        self.store.add_message(BOT, UMO, UID, "他", "user", "今天的消息", created_at=2000)
        self.store.add_message(BOT, UMO, "bot", "她", "bot", "今天的回复", created_at=2001)
        # 再补写一条「8 号的摘要」（id 更大，但时间更早）；摘要由 compress_messages 直接写库
        with self.store.lock:
            self.store.conn.execute(
                "INSERT INTO messages(bot_id, umo, uid, name, role, content, created_at, platform, pid) "
                "VALUES(?,?,?,?,?,?,?,?,0)",
                (BOT, UMO, UID, "摘要", "summary", "【摘要】老对话", 1000, "lark"),
            )
            self.store.conn.commit()
        rows = self.store.list_messages(BOT, UMO, limit=10)
        self.assertEqual([r["created_at"] for r in rows], [2001, 2000, 1000])
        self.assertEqual(rows[-1]["role"], "summary")

    def test_same_timestamp_falls_back_to_id_desc(self):
        self.store.add_message(BOT, UMO, UID, "他", "user", "同一秒 A", created_at=3000)
        self.store.add_message(BOT, UMO, UID, "他", "user", "同一秒 B", created_at=3000)
        rows = self.store.list_messages(BOT, UMO, limit=10)
        self.assertEqual([r["content"] for r in rows], ["同一秒 B", "同一秒 A"])

    def test_recall_messages_still_ascending(self):
        for i, text in enumerate(("昨天说的第一句", "昨天说的第二句")):
            self.store.add_message(BOT, UMO, UID, "他", "user", text,
                                   created_at=int(__import__("time").time()) - 86400 + i)
        now = int(__import__("time").time())
        rows = self.store.recall_messages(BOT, "昨天", umo=UMO, limit=10)
        self.assertEqual([r["content"] for r in rows], ["昨天说的第一句", "昨天说的第二句"])
        self.assertTrue(all(r["created_at"] <= now for r in rows))


if __name__ == "__main__":
    unittest.main()
