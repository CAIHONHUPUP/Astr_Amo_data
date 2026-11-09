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

    def _legacy(self, content, scope="", created=0, importance=0.5, uid=None):
        with self.store.lock:
            self.store.conn.execute(
                "INSERT INTO memories(bot_id, uid, scope, group_id, kind, content, importance, "
                "hit_count, created_at, last_hit_at, idem_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (self.bot, uid or self.uid, scope, "", "fact", content, importance, 0, created, created, ""),
            )
            self.store.conn.commit()

    def test_legacy_empty_scope_visible_in_private(self):
        # 老数据的 scope 是空串/global，私聊召回也必须看得见
        self._legacy("在体检中心上夜班", scope="")
        self._legacy("以前在老家养过猫", scope="global")
        self.assertTrue(any("上夜班" in item["content"]
                            for item in self.store.recall(self.bot, self.uid, "上班", scope="private")))
        self.assertTrue(any("养过猫" in item["content"]
                            for item in self.store.recall(self.bot, self.uid, "猫", scope="private")))

    def test_timeless_legacy_not_pushed_out_of_pool(self):
        # 先塞 450 条无时间戳的老数据，再把目标放最后（id 最大）
        rows = [(self.bot, self.uid, "private", "", "fact", "旧记忆 %d" % i, 0.5, 0, 0, 0, "")
                for i in range(450)]
        with self.store.lock:
            self.store.conn.executemany(
                "INSERT INTO memories(bot_id, uid, scope, group_id, kind, content, importance, "
                "hit_count, created_at, last_hit_at, idem_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            self.store.conn.commit()
        self._legacy("在体检中心上夜班")
        self.assertTrue(any("上夜班" in item["content"]
                            for item in self.store.recall(self.bot, self.uid, "上班")))

    def test_duplicate_content_deduped(self):
        self.store.remember(self.bot, self.uid, "他喜欢喝美式")
        self.store.remember(self.bot, self.uid, "他喜欢喝美式")
        items = self.store.recall(self.bot, self.uid, "")
        self.assertEqual(len([i for i in items if "美式" in i["content"]]), 1)

    def test_timeless_legacy_not_treated_as_fresh(self):
        self._legacy("旧数据的一条", created=0, importance=0.75)
        self.store.remember(self.bot, self.uid, "刚刚说的新事", importance=0.75)
        ranked = self.store.recall(self.bot, self.uid, "")
        self.assertIn("新事", ranked[0]["content"])


if __name__ == "__main__":
    unittest.main()
