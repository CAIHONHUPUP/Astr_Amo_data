# -*- coding: utf-8 -*-
"""面板「聊天」页排序：聊天按时间倒序在前，摘要按时间倒序统一放最后。"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import DataStore  # noqa: E402

BOT = "大肥鱼"
UMO = "lark:FriendMessage:chat1"
UID = "ou_xxx"


class MessageGroupingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="amo-group-")
        self.store = DataStore(os.path.join(self.tmp, "a.db"))
        # 3 条聊天（新）
        self.store.add_message(BOT, UMO, UID, "他", "user", "聊天1", created_at=3000)
        self.store.add_message(BOT, UMO, UID, "她", "bot", "聊天2", created_at=2900)
        self.store.add_message(BOT, UMO, UID, "他", "user", "聊天3", created_at=2800)
        # 2 条摘要（旧，比所有聊天都早）——add_message 会把未知 role 归成 user，
        # 摘要行只能像压缩逻辑那样直接写 SQL
        with self.store.lock:
            for ts, text in ((2000, "[11-08]摘要1"), (1900, "[11-08]摘要2")):
                self.store.conn.execute(
                    "INSERT INTO messages(bot_id, umo, uid, name, role, content, created_at, "
                    "compressed, platform, pid) VALUES(?,?,?,?,?,?,?,1,?,0)",
                    (BOT, UMO, UID, "摘要", "summary", text, ts, "lark"),
                )
            self.store.conn.commit()

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_old_list_hides_summaries(self):
        rows = self.store.list_messages(BOT, UMO, limit=3)
        self.assertEqual([r["content"] for r in rows], ["聊天1", "聊天2", "聊天3"])

    def test_grouped_puts_summaries_at_bottom(self):
        rows = self.store.list_messages_grouped(
            BOT, UMO, limit=3, summary_limit=10,
        )
        self.assertEqual(
            [r["content"] for r in rows],
            ["聊天1", "聊天2", "聊天3", "[11-08]摘要1", "[11-08]摘要2"],
        )

    def test_summaries_sorted_by_time_desc(self):
        rows = self.store.list_messages_grouped(BOT, UMO, limit=10, summary_limit=1)
        self.assertEqual(rows[-1]["content"], "[11-08]摘要1")
        self.assertEqual(sum(1 for r in rows if r["role"] == "summary"), 1)

    def test_other_conversation_isolated(self):
        rows = self.store.list_messages_grouped(BOT, "qq:GroupMessage:g1", limit=50, summary_limit=50)
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
