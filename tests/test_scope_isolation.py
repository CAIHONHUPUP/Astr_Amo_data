# -*- coding: utf-8 -*-
"""群聊 / 私聊记忆隔离：写入侧的重复合并、压缩整理都不能把两个会话混成一条。

召回侧的隔离已有 test_recall_bm25.test_scope_still_isolated 等用例覆盖，
这里只盯「写进去的时候」会不会串味：
1. remember 的 idem_key 去重按 (bot_id, uid, scope, group_id) 比较；
2. compress_memories 的「同日摘要合并」按 (uid, 日期, scope, group_id) 分组；
3. compress_memories 的近重复合并只在同一 (scope, group_id) 内进行。
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import DataStore  # noqa: E402


class ScopeWriteIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = DataStore(os.path.join(self.tmp, "amo_data.db"))
        self.bot = "bot1"
        self.uid = "10001"

    def tearDown(self):
        self.store.close()

    def _active(self, scope="", group_id=""):
        return self.store.recall(self.bot, self.uid, "", limit=50,
                                 scope=scope, group_id=group_id)

    def _count(self):
        return self.store.stats(self.bot)["memories"]

    # ① 显式 idem_key：两个会话各说一次同一句话，不能合并成一条
    def test_idem_key_not_shared_between_scopes(self):
        first = self.store.remember(self.bot, self.uid, "他说明天要出差",
                                    idem_key="trip", scope="private")
        second = self.store.remember(self.bot, self.uid, "他说明天要出差",
                                     idem_key="trip", scope="group", group_id="g1")
        self.assertFalse(first.get("duplicate"))
        self.assertFalse(second.get("duplicate"))
        self.assertNotEqual(first["memory_id"], second["memory_id"])
        self.assertEqual(self._count(), 2)
        self.assertEqual(len(self._active(scope="private")), 1)
        self.assertEqual(len(self._active(scope="group", group_id="g1")), 1)

    # ② 自动 idem_key（同一句话的指纹）：同理不能跨会话合并
    def test_auto_idem_not_shared_between_scopes(self):
        self.store.remember(self.bot, self.uid, "他养了一只叫团子的猫", scope="private")
        self.store.remember(self.bot, self.uid, "他养了一只叫团子的猫",
                            scope="group", group_id="g1")
        self.assertEqual(self._count(), 2)
        self.assertEqual(len(self._active(scope="private")), 1)
        self.assertEqual(len(self._active(scope="group", group_id="g1")), 1)

    # ③ 同一会话里再说一遍，仍然要合并（别把去重修坏了）
    def test_same_scope_still_dedupes(self):
        a = self.store.remember(self.bot, self.uid, "他说明天要出差",
                                idem_key="trip", scope="group", group_id="g1")
        b = self.store.remember(self.bot, self.uid, "他说明天要出差",
                                idem_key="trip", scope="group", group_id="g1")
        self.assertTrue(b.get("duplicate"))
        self.assertEqual(a["memory_id"], b["memory_id"])
        self.assertEqual(self._count(), 1)

    # ④ 同一天、同 kind 的会话摘要：私聊一条、群聊一条，压缩整理不能拼在一起
    def test_compress_keeps_group_and_private_summary_apart(self):
        private_id = self.store.remember(
            self.bot, self.uid, "2026-11-08 私聊里他讲了换工作的事，还没定下来",
            kind="conversation_summary", importance=0.6, scope="private",
        )["memory_id"]
        group_id = self.store.remember(
            self.bot, self.uid, "2026-11-08 群里在约周末爬山，他说想去又怕累",
            kind="conversation_summary", importance=0.6, scope="group", group_id="g1",
        )["memory_id"]
        result = self.store.compress_memories(self.bot, apply=True)
        self.assertEqual(result["merged_summaries"], 0)
        private_rows = {int(r["id"]): r for r in self._active(scope="private")}
        group_rows = {int(r["id"]): r for r in self._active(scope="group", group_id="g1")}
        self.assertIn(private_id, private_rows)
        self.assertIn(group_id, group_rows)
        # 群聊内容一个字都不许出现在私聊那条里
        self.assertNotIn("爬山", private_rows[private_id]["content"])
        self.assertNotIn("换工作", group_rows[group_id]["content"])

    # ⑤ 近重复合并只在同一会话内进行
    def test_compress_near_duplicate_across_scopes_kept(self):
        long_text = "他今天在公司加班到很晚才回家路上还下着雨衣服都湿透了"
        a = self.store.remember(self.bot, self.uid, long_text,
                                kind="fact", scope="private")["memory_id"]
        b = self.store.remember(self.bot, self.uid, long_text + "，还买了烤鱼",
                                kind="fact", scope="group", group_id="g1")["memory_id"]
        self.assertNotEqual(a, b)
        result = self.store.compress_memories(self.bot, apply=True)
        self.assertEqual(result["merged_near_duplicates"], 0)
        self.assertEqual(self._count(), 2)

    # ⑥ 不同的人的「同一天摘要」也不能并成一条（改前只按日期分组，跨人也会拼）
    def test_compress_keeps_different_people_apart(self):
        uid_b = "10002"
        a = self.store.remember(
            self.bot, self.uid, "2026-11-08 他讲了换工作的事，还没定下来",
            kind="conversation_summary", importance=0.6,
        )["memory_id"]
        b = self.store.remember(
            self.bot, uid_b, "2026-11-08 另一个人问的是装机配置",
            kind="conversation_summary", importance=0.6,
        )["memory_id"]
        result = self.store.compress_memories(self.bot, apply=True)
        self.assertEqual(result["merged_summaries"], 0)
        mine = {int(r["id"]): r for r in self.store.recall(self.bot, self.uid, "", limit=50)}
        other = {int(r["id"]): r for r in self.store.recall(self.bot, uid_b, "", limit=50)}
        self.assertIn(a, mine)
        self.assertIn(b, other)
        self.assertNotIn("装机", mine[a]["content"])
        self.assertNotIn("换工作", other[b]["content"])

    # ⑦ 老库里的去重索引是 (bot_id, uid, idem_key)，打开时会被换成带 scope 的版本
    def test_legacy_unique_index_migrated(self):
        import sqlite3

        path = os.path.join(self.tmp, "legacy.db")
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT NOT NULL,"
            " uid TEXT NOT NULL, scope TEXT NOT NULL DEFAULT 'private', group_id TEXT DEFAULT '',"
            " kind TEXT DEFAULT 'fact', content TEXT NOT NULL, importance REAL DEFAULT 0.5,"
            " hit_count INTEGER DEFAULT 0, created_at INTEGER NOT NULL,"
            " last_hit_at INTEGER NOT NULL, idem_key TEXT DEFAULT '')"
        )
        conn.execute(
            "CREATE UNIQUE INDEX idx_mem_idem ON memories(bot_id, uid, idem_key)"
            " WHERE idem_key != ''"
        )
        conn.commit()
        conn.close()

        store = DataStore(path)
        try:
            sql = store.conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_mem_idem'"
            ).fetchone()["sql"]
            self.assertIn("scope", sql)
            store.remember(self.bot, self.uid, "同一句话", idem_key="k1", scope="private")
            second = store.remember(self.bot, self.uid, "同一句话", idem_key="k1",
                                    scope="group", group_id="g1")
            self.assertFalse(second.get("duplicate"))
            self.assertEqual(store.stats(self.bot)["memories"], 2)
        finally:
            store.close()

    # ⑧ 群里写的记忆，私聊召回时一条都不该出现（空池也不许回退到全库）
    def test_group_write_invisible_in_private_recall(self):
        self.store.remember(self.bot, self.uid, "他在群里说周末去爬山",
                            scope="group", group_id="g1")
        self.assertEqual(self.store.recall(self.bot, self.uid, "爬山", scope="private"), [])
        self.assertEqual(self.store.recall(self.bot, self.uid, "爬山",
                                           scope="group", group_id="g2"), [])
        self.assertEqual(len(self.store.recall(self.bot, self.uid, "爬山",
                                               scope="group", group_id="g1")), 1)


if __name__ == "__main__":
    unittest.main()
