# -*- coding: utf-8 -*-
"""存储层收尾修复的回归测试：重复消息、向量清理。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import DataStore  # noqa: E402

BOT = "bot1"
UID = "302578963"
UMO = "aiocqhttp:private:302578963"


class StoreFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = DataStore(os.path.join(self.tmp, "s.db"))

    def tearDown(self):
        self.store.close()

    def test_duplicate_message_within_10s_is_ignored(self):
        first = self.store.add_message(BOT, UMO, UID, "family", "user", "在吗")
        second = self.store.add_message(BOT, UMO, UID, "family", "user", "在吗")
        self.assertTrue(first)
        self.assertEqual(second, first)
        rows = self.store.messages_in_span(BOT, 0, 2 ** 31, umo=UMO, uid=UID)
        self.assertEqual(len(rows), 1)

    def test_duplicate_after_ten_seconds_is_kept(self):
        first = self.store.add_message(BOT, UMO, UID, "family", "user", "晚安", created_at=1000)
        second = self.store.add_message(BOT, UMO, UID, "family", "user", "晚安", created_at=1100)
        self.assertNotEqual(first, second)

    def test_delete_memory_removes_vector_row(self):
        result = self.store.remember(BOT, UID, "他喜欢喝冰美式", kind="fact")
        memory_id = int(result["memory_id"])
        with self.store.lock:
            self.store.conn.execute(
                "INSERT INTO memory_vec(memory_id, model, dim, vec, updated_at) VALUES(?,?,?,?,?)",
                (memory_id, "test", 3, "[0.1,0.2,0.3]", 1),
            )
            self.store.conn.commit()
        self.assertTrue(self.store.delete_memory(BOT, memory_id))
        with self.store.lock:
            left = self.store.conn.execute(
                "SELECT COUNT(*) AS n FROM memory_vec WHERE memory_id=?", (memory_id,)
            ).fetchone()["n"]
        self.assertEqual(left, 0)


if __name__ == "__main__":
    unittest.main()
