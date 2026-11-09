# -*- coding: utf-8 -*-
"""检索打分（BM25 + 遗忘曲线）的单测。"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import DataStore  # noqa: E402


class RecallScoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = DataStore(os.path.join(self.tmp, "amo_data.db"))
        self.bot = "bot1"
        self.uid = "10001"

    def tearDown(self):
        self.store.close()

    def test_two_char_query_hits_substring(self):
        # 旧实现只认连续子串，「上班」查不到「上夜班」
        self.store.remember(self.bot, self.uid, "在体检中心上夜班", importance=0.75)
        items = self.store.recall(self.bot, self.uid, "上班")
        self.assertTrue(any("上夜班" in item["content"] for item in items))

    def test_two_char_query_hits_other_memory(self):
        self.store.remember(self.bot, self.uid, "我养了一只叫豆豆的猫")
        self.store.remember(self.bot, self.uid, "他喜欢喝美式")
        self.assertTrue(any("豆豆" in item["content"]
                            for item in self.store.recall(self.bot, self.uid, "猫")))
        self.assertTrue(any("豆豆" in item["content"]
                            for item in self.store.recall(self.bot, self.uid, "豆豆")))

    def test_empty_query_prefers_important(self):
        self.store.remember(self.bot, self.uid, "他喜欢喝美式", importance=0.35)
        self.store.remember(self.bot, self.uid, "他生日是五月一号", importance=0.75)
        ranked = self.store.recall(self.bot, self.uid, "")
        self.assertIn("生日", ranked[0]["content"])

    def test_forgetting_curve_prefers_recent(self):
        self.store.remember(self.bot, self.uid, "六十天前的旧事一件", importance=0.5)
        self.store.remember(self.bot, self.uid, "刚刚说的新鲜事一件", importance=0.5)
        old = int(time.time()) - 60 * 86400
        with self.store.lock:
            self.store.conn.execute(
                "UPDATE memories SET last_hit_at=? WHERE content LIKE ?",
                (old, "%旧事一件%"),
            )
            self.store.conn.commit()
        ranked = self.store.recall(self.bot, self.uid, "")
        self.assertIn("新鲜事", ranked[0]["content"])

    def test_scope_still_isolated(self):
        self.store.remember(self.bot, self.uid, "群里说的上夜班", scope="group", group_id="g1")
        self.store.remember(self.bot, self.uid, "私聊说的上白班", scope="private")
        group_items = self.store.recall(self.bot, self.uid, "上班", scope="group", group_id="g1")
        self.assertTrue(group_items)
        self.assertTrue(all("群里" in item["content"] for item in group_items))


if __name__ == "__main__":
    unittest.main()
