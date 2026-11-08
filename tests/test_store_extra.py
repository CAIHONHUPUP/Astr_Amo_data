# -*- coding: utf-8 -*-
"""聊天记录与日志：压缩、保留期、删除。"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import DataStore  # noqa: E402


class ChatLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = DataStore(os.path.join(self.tmp, "amo_data.db"))
        self.bot = "bot1"
        self.umo = "test:GroupMessage:1"

    def tearDown(self):
        self.store.close()

    def test_add_and_list_messages(self):
        self.store.add_message(self.bot, self.umo, "u1", "小明", "user", "在吗")
        self.store.add_message(self.bot, self.umo, "", "她", "bot", "在呀")
        items = self.store.list_messages(self.bot, self.umo)
        self.assertEqual(len(items), 2)
        self.assertEqual({item["role"] for item in items}, {"user", "bot"})

    def test_delete_message(self):
        mid = self.store.add_message(self.bot, self.umo, "u1", "小明", "user", "删我")
        self.assertTrue(self.store.delete_message(self.bot, mid))
        self.assertEqual(self.store.list_messages(self.bot, self.umo), [])

    def test_compress_old_messages(self):
        # 造一条 10 天前的原始记录
        self.store.add_message(self.bot, self.umo, "u1", "小明", "user", "很久以前说的话")
        with self.store.lock:
            self.store.conn.execute(
                "UPDATE messages SET created_at=? WHERE bot_id=?",
                (int(time.time()) - 10 * 86400, self.bot),
            )
            self.store.conn.commit()
        result = self.store.compress_messages(self.bot, after_days=3, keep_days=14)
        self.assertEqual(result["compressed"], 1)
        items = self.store.list_messages(self.bot, self.umo)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["compressed"], 1)
        self.assertIn("摘要", items[0]["name"])

    def test_purge_expired(self):
        self.store.add_message(self.bot, self.umo, "u1", "小明", "user", "过期记录")
        with self.store.lock:
            self.store.conn.execute(
                "UPDATE messages SET created_at=? WHERE bot_id=?",
                (int(time.time()) - 30 * 86400, self.bot),
            )
            self.store.conn.commit()
        self.store.compress_messages(self.bot, after_days=3, keep_days=14)
        self.assertEqual(self.store.list_messages(self.bot, self.umo), [])

    def test_logs(self):
        self.store.add_log(self.bot, "info", "schedule", "生成日程")
        self.store.add_log(self.bot, "error", "weather", "天气失败")
        self.assertEqual(len(self.store.list_logs(self.bot)), 2)
        self.assertEqual(len(self.store.list_logs(self.bot, "error")), 1)
        self.assertEqual(self.store.clear_logs(self.bot), 2)
        self.assertEqual(self.store.list_logs(self.bot), [])

    def test_purge_logs(self):
        self.store.add_log(self.bot, "info", "x", "旧日志")
        with self.store.lock:
            self.store.conn.execute(
                "UPDATE logs SET created_at=? WHERE bot_id=?",
                (int(time.time()) - 10 * 86400, self.bot),
            )
            self.store.conn.commit()
        self.assertEqual(self.store.purge_logs(self.bot, 7), 1)
        self.assertEqual(self.store.list_logs(self.bot), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
