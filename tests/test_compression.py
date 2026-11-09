# -*- coding: utf-8 -*-
"""压缩/槽位相关的回归测试：丢内容、丢 uid、把「记住」当住址。"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import DataStore, _slot_facts  # noqa: E402

BOT = "bot1"
UID = "302578963"
UMO = "lark:FriendMessage:ou_cec66"


class SlotTests(unittest.TestCase):
    def test_remember_is_not_home(self):
        # 「记住北京的咖啡」里的「住」不是住址；否则会抽出假 home 并参与冲突判定
        self.assertEqual(_slot_facts("我记住北京的咖啡很好喝"), {})
        self.assertEqual(_slot_facts("我记住了他说的那家店"), {})

    def test_real_home_still_recognized(self):
        self.assertEqual(_slot_facts("我住在东莞东城").get("home"), "东莞东城")
        self.assertEqual(_slot_facts("他搬到深圳南山了").get("home"), "深圳南山")


class CompressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = DataStore(os.path.join(self.tmp, "c.db"))

    def tearDown(self):
        self.store.close()

    def test_message_summary_keeps_uid(self):
        # 3 天前的聊天会被压成 summary；摘要必须保留 uid，否则按 uid 的【按时间回看】取不到
        old = int(time.time()) - 5 * 86400
        for i in range(3):
            self.store.add_message(BOT, UMO, UID, "family", "user", "旧消息%d" % i, created_at=old + i)
        self.store.compress_messages(BOT, after_days=1, keep_days=30)
        rows = self.store.messages_in_span(BOT, old - 1, old + 10, umo=UMO, uid=UID)
        summaries = [row for row in rows if row["role"] == "summary"]
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["uid"], UID)
        self.assertIn("旧消息", summaries[0]["content"])

    def test_memory_merge_over_budget_keeps_rows_active(self):
        # 两段 3000 字摘要塞不进 4000 字：装不下的那几条不能标 merged（否则正文永远召不回来）
        day = "2026-11-01"
        self.store.remember(BOT, UID, "%s %s" % (day, "甲" * 3000), kind="conversation_summary")
        self.store.remember(BOT, UID, "%s %s" % (day, "乙" * 3000), kind="conversation_summary")
        self.store.compress_memories(BOT, apply=True)
        rows = self.store.list_memories(BOT, UID, limit=50)
        active = [row for row in rows if (row.get("status") or "active") == "active"]
        self.assertEqual(len(active), 2)


if __name__ == "__main__":
    unittest.main()
