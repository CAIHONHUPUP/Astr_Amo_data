# -*- coding: utf-8 -*-
"""跨平台人物合一的单测：QQ 和飞书上的同一个人要能并到一起。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import DataStore, platform_label, platform_of_umo  # noqa: E402


class PersonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = DataStore(os.path.join(self.tmp, "amo_data.db"))
        self.bot = "bot1"

    def tearDown(self):
        self.store.close()

    def test_platform_label(self):
        self.assertEqual(platform_label("aiocqhttp"), "QQ")
        self.assertEqual(platform_label("lark"), "飞书")
        self.assertEqual(platform_of_umo("aiocqhttp:GroupMessage:123"), "aiocqhttp")
        self.assertEqual(platform_of_umo("lark:FriendMessage:ou_abc"), "lark")

    def test_same_name_auto_merge(self):
        qq_pid = self.store.resolve_person(self.bot, "aiocqhttp", "10001", "墨辞烟")
        lark_pid = self.store.resolve_person(self.bot, "lark", "ou_abc", "墨辞烟")
        self.assertEqual(qq_pid, lark_pid)
        links = self.store.person_links(self.bot, qq_pid)
        self.assertEqual(len(links), 2)
        self.assertEqual({item["label"] for item in links}, {"QQ", "飞书"})

    def test_different_name_not_merged(self):
        a = self.store.resolve_person(self.bot, "aiocqhttp", "10001", "小明")
        b = self.store.resolve_person(self.bot, "lark", "ou_abc", "小红")
        self.assertNotEqual(a, b)

    def test_ambiguous_name_not_merged(self):
        # 两个 QQ 用户都叫「小明」，飞书再出现一个「小明」时不能乱并
        self.store.resolve_person(self.bot, "aiocqhttp", "10001", "小明")
        self.store.resolve_person(self.bot, "aiocqhttp", "10002", "小明")
        before = len(self.store.list_persons(self.bot))
        self.store.resolve_person(self.bot, "lark", "ou_abc", "小明")
        self.assertEqual(len(self.store.list_persons(self.bot)), before + 1)

    def test_messages_carry_platform_and_pid(self):
        self.store.add_message(
            self.bot, "aiocqhttp:FriendMessage:10001", "10001", "墨辞烟", "user", "在吗"
        )
        self.store.add_message(
            self.bot, "lark:FriendMessage:ou_abc", "ou_abc", "墨辞烟", "user", "飞书也来了"
        )
        rows = self.store.list_messages(self.bot, limit=10)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["platform"] for row in rows}, {"aiocqhttp", "lark"})
        self.assertEqual({row["label"] for row in rows}, {"QQ", "飞书"})
        pids = {row["pid"] for row in rows}
        self.assertEqual(len(pids), 1)
        self.assertGreater(list(pids)[0], 0)

    def test_person_context_returns_other_platform(self):
        self.store.add_message(
            self.bot, "aiocqhttp:FriendMessage:10001", "10001", "墨辞烟", "user", "今天加班"
        )
        self.store.add_message(
            self.bot, "lark:FriendMessage:ou_abc", "ou_abc", "墨辞烟", "user", "明天出差"
        )
        detail = self.store.person_context(self.bot, "aiocqhttp", "10001", 10)
        self.assertTrue(detail)
        self.assertEqual(len(detail["links"]), 2)
        self.assertEqual(len(detail["others"]), 1)
        self.assertEqual(detail["others"][0]["label"], "飞书")
        texts = " ".join(item["content"] for item in detail["recent"])
        self.assertIn("明天出差", texts)

    def test_merge_and_unlink(self):
        a = self.store.resolve_person(self.bot, "aiocqhttp", "10001", "阿墨")
        b = self.store.resolve_person(self.bot, "lark", "ou_abc", "阿墨仔")
        self.assertNotEqual(a, b)
        self.assertTrue(self.store.merge_persons(self.bot, a, b))
        detail = self.store.person_detail(self.bot, a)
        self.assertEqual(len(detail["links"]), 2)
        self.assertFalse(self.store.person_detail(self.bot, b))
        # 解绑后单独成一个人
        self.assertTrue(self.store.unlink_identity(self.bot, "lark", "ou_abc"))
        detail = self.store.person_detail(self.bot, a)
        self.assertEqual(len(detail["links"]), 1)

    def test_list_persons_counts_messages(self):
        self.store.add_message(self.bot, "aiocqhttp:FriendMessage:10001", "10001", "阿墨", "user", "1")
        self.store.add_message(self.bot, "aiocqhttp:FriendMessage:10001", "10001", "阿墨", "user", "2")
        items = self.store.list_persons(self.bot)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["msg_count"], 2)
        self.assertEqual(items[0]["platforms"], ["QQ"])

    def test_find_profile_across_bots(self):
        # 面板聚合所有 bot，编辑时要能找到这条属于哪个 bot
        self.store.save_profile("botA", "10001", name="阿墨", relation="相恋")
        found = self.store.find_profile("10001")
        self.assertEqual(found["bot_id"], "botA")
        self.assertEqual(found["name"], "阿墨")
        self.assertEqual(self.store.find_profile("不存在"), {})

    def test_list_messages_filter_by_uid(self):
        self.store.add_message(self.bot, "aiocqhttp:FriendMessage:1", "10001", "甲", "user", "a")
        self.store.add_message(self.bot, "aiocqhttp:FriendMessage:1", "10002", "乙", "user", "b")
        rows = self.store.list_messages(self.bot, uid="10001")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], "a")

    def test_old_db_gets_platform_columns(self):
        # 模拟老库：建表时没有 platform / pid
        import sqlite3
        path = os.path.join(self.tmp, "old.db")
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT NOT NULL, "
            "umo TEXT NOT NULL, uid TEXT DEFAULT '', name TEXT DEFAULT '', "
            "role TEXT DEFAULT 'user', content TEXT NOT NULL, created_at INTEGER NOT NULL, "
            "compressed INTEGER DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO messages(bot_id, umo, uid, name, role, content, created_at) "
            "VALUES('bot1','aiocqhttp:FriendMessage:1','1','阿墨','user','旧消息',1)"
        )
        conn.commit()
        conn.close()
        store = DataStore(path)
        try:
            rows = store.list_messages("bot1", limit=10)
            self.assertEqual(rows[0]["content"], "旧消息")
            self.assertEqual(rows[0]["platform"], "")
            store.add_message("bot1", "aiocqhttp:FriendMessage:1", "1", "阿墨", "user", "新消息")
            self.assertEqual(len(store.list_messages("bot1", limit=10)), 2)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
