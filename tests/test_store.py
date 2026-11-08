# -*- coding: utf-8 -*-
"""DataStore 的单测：画像可编辑、记忆读写、关系网、缓存。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import DataStore  # noqa: E402


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = DataStore(os.path.join(self.tmp, "amo_data.db"))
        self.bot = "bot1"
        self.uid = "10001"

    def tearDown(self):
        self.store.close()

    def test_profile_editable(self):
        first = self.store.save_profile(self.bot, self.uid, name="小明", relation="普通")
        self.assertEqual(first["name"], "小明")
        self.assertEqual(first["relation"], "普通")
        # 只改关系，名字不能被清空
        second = self.store.save_profile(self.bot, self.uid, relation="相恋")
        self.assertEqual(second["name"], "小明")
        self.assertEqual(second["relation"], "相恋")
        # 改成别名，也不影响其它字段
        third = self.store.save_profile(self.bot, self.uid, alias="老公,哥哥")
        self.assertEqual(third["name"], "小明")
        self.assertEqual(third["alias"], "老公,哥哥")

    def test_profile_isolated_by_bot(self):
        self.store.save_profile("botA", self.uid, name="A")
        self.store.save_profile("botB", self.uid, name="B")
        self.assertEqual(self.store.get_profile("botA", self.uid)["name"], "A")
        self.assertEqual(self.store.get_profile("botB", self.uid)["name"], "B")

    def test_remember_and_recall(self):
        self.store.remember(self.bot, self.uid, "他养了一只叫团子的猫", importance=0.9)
        self.store.remember(self.bot, self.uid, "他喜欢喝美式", importance=0.5)
        items = self.store.recall(self.bot, self.uid, "团子")
        self.assertTrue(any("团子" in item["content"] for item in items))
        # 空 query 按重要度排序
        ranked = self.store.recall(self.bot, self.uid, "")
        self.assertEqual(len(ranked), 2)
        self.assertIn("团子", ranked[0]["content"])

    def test_remember_idempotent(self):
        a = self.store.remember(self.bot, self.uid, "重复的一条", idem_key="k1")
        b = self.store.remember(self.bot, self.uid, "重复的一条", idem_key="k1")
        self.assertTrue(a["ok"])
        self.assertTrue(b.get("duplicate"))
        self.assertEqual(self.store.stats(self.bot)["memories"], 1)

    def test_scope_group(self):
        self.store.remember(self.bot, self.uid, "群里说的", scope="group", group_id="g1")
        self.store.remember(self.bot, self.uid, "私聊说的", scope="private")
        self.assertEqual(len(self.store.recall(self.bot, self.uid, "", scope="group")), 1)
        self.assertEqual(len(self.store.recall(self.bot, self.uid, "", scope="private")), 1)

    def test_delete_profile_cascades(self):
        self.store.save_profile(self.bot, self.uid, name="小明")
        self.store.remember(self.bot, self.uid, "记住的一条")
        self.store.save_relation(self.bot, self.uid, "20002", "朋友")
        counts = self.store.delete_profile(self.bot, self.uid)
        self.assertEqual(counts["profiles"], 1)
        self.assertEqual(counts["memories"], 1)
        self.assertEqual(counts["relations"], 1)
        self.assertEqual(self.store.stats(self.bot)["memories"], 0)

    def test_relations(self):
        self.assertTrue(self.store.save_relation(self.bot, "a", "b", "恋人", "在一起两年"))
        rows = self.store.list_relations(self.bot)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["relation"], "恋人")
        # 同一对再存就是更新
        self.store.save_relation(self.bot, "a", "b", "朋友")
        self.assertEqual(self.store.list_relations(self.bot)[0]["relation"], "朋友")
        self.assertTrue(self.store.delete_relation(self.bot, "a", "b"))
        self.assertEqual(self.store.list_relations(self.bot), [])

    def test_cache_roundtrip(self):
        self.store.cache_set(self.bot, "schedule:2026-11-09", {"items": [1, 2, 3]})
        self.assertEqual(self.store.cache_get(self.bot, "schedule:2026-11-09"), {"items": [1, 2, 3]})
        self.store.cache_delete(self.bot, "schedule:2026-11-09")
        self.assertIsNone(self.store.cache_get(self.bot, "schedule:2026-11-09"))

    def test_cache_expired(self):
        self.store.cache_set(self.bot, "k", "v", ttl=-1)
        self.assertIsNone(self.store.cache_get(self.bot, "k"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
