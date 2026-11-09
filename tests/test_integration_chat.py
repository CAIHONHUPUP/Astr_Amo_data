# -*- coding: utf-8 -*-
"""主链路集成测试：消息落库 → 按 uid 回看 → 压缩后仍能取到。

这些用例跨 store / time_query / main 三层，模拟 amo 真正会走的调用顺序，
而不是单个函数的边界值。
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import DataStore  # noqa: E402
from time_query import parse_time_span  # noqa: E402

BOT = "bot1"
UMO = "aiocqhttp:private:302578963"
UID = "302578963"


class ChatPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="amo-pipeline-")
        self.store = DataStore(os.path.join(self.tmp, "a.db"))

    def tearDown(self):
        self.store.close()

    def test_message_id_is_the_dedupe_key(self):
        """同一个平台 message_id 只落一条；不同 id 的同文消息是两条。"""
        first = self.store.add_message(BOT, UMO, UID, "他", "user", "在吗",
                                       message_id="qq-1001")
        again = self.store.add_message(BOT, UMO, UID, "他", "user", "在吗",
                                       message_id="qq-1001")
        self.assertEqual(first, again)
        self.assertEqual(len(self.store.list_messages(BOT, UMO, 50)), 1)

        self.store.add_message(BOT, UMO, UID, "他", "user", "在吗",
                               message_id="qq-1002")
        self.assertEqual(len(self.store.list_messages(BOT, UMO, 50)), 2)

    def test_without_message_id_still_uses_10s_dedupe(self):
        """旧调用方不带 message_id 时保持原行为：10 秒内同文只留一条。"""
        self.store.add_message(BOT, UMO, UID, "他", "user", "吃了吗")
        self.store.add_message(BOT, UMO, UID, "他", "user", "吃了吗")
        self.assertEqual(len(self.store.list_messages(BOT, UMO, 50)), 1)

    def test_compressed_summary_keeps_uid_and_stays_recallable(self):
        """3 天前的聊天压成摘要后，按 uid 的【按时间回看】仍然取得到。"""
        old = int(time.time()) - 5 * 86400
        for index in range(30):
            self.store.add_message(
                BOT, UMO, UID, "他", "user", "第%d条：今天在修 bug" % index,
                message_id="m-%d" % index, created_at=old + index,
            )
        counts = self.store.compress_messages(BOT, after_days=3, keep_days=14)
        self.assertGreaterEqual(counts["compressed"], 1)

        rows = self.store.messages_since(BOT, UID, 0)
        summaries = [row for row in rows if row["role"] == "summary"]
        self.assertTrue(summaries, "压缩后应该留下能按 uid 取到的摘要")
        self.assertEqual(summaries[0]["uid"], UID)
        self.assertIn("修 bug", summaries[0]["content"])

        span = parse_time_span("最近7天")
        self.assertTrue(span)
        in_span = self.store.messages_in_span(
            BOT, span["start"], span["end"], uid=UID, limit=50
        )
        self.assertTrue(any(row["role"] == "summary" for row in in_span))

    def test_recall_3char_query_uses_trigram_and_finds_memory(self):
        """>=3 个字的查询走 trigram 粗筛，仍然命中；删除后不再命中（索引同步）。"""
        path = os.path.join(self.tmp, "fts.db")
        store = DataStore(path, settings={"recall_fts_enabled": True})
        try:
            store.remember(BOT, UID, "主人喜欢喝美式咖啡，不加糖", importance=0.8)
            store.remember(BOT, UID, "主人生日是十二月十号", importance=0.8)
            self.assertTrue(store._fts_tri)
            hits = store.recall(BOT, UID, "美式咖啡", limit=3)
            self.assertTrue(hits)
            self.assertIn("美式咖啡", hits[0]["content"])

            memory_id = int(hits[0]["id"])
            store.delete_memory(BOT, memory_id)
            again = store.recall(BOT, UID, "美式咖啡", limit=3)
            self.assertFalse(any("美式咖啡" in row["content"] for row in again))
        finally:
            store.close()

    def test_old_db_gets_trigram_index_rebuilt(self):
        """老库升级：先在没有 trigram 的旧库写记忆，再开启 FTS 重开，索引要自动补全。"""
        path = os.path.join(self.tmp, "old.db")
        old_store = DataStore(path, settings={"recall_fts_enabled": False})
        old_store.remember(BOT, UID, "主人喜欢喝美式咖啡，不加糖", importance=0.8)
        old_store.remember(BOT, UID, "主人生日是十二月十号", importance=0.8)
        old_store.close()

        store = DataStore(path, settings={"recall_fts_enabled": True})
        try:
            self.assertTrue(store._fts_tri)
            marker = store.conn.execute(
                "SELECT value FROM store_meta WHERE key='fts_tri_rows'"
            ).fetchone()["value"]
            total = store.conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]
            self.assertEqual(int(marker), int(total))
            candidates = store._substring_candidates(
                "SELECT id, content, kind, importance, created_at, last_hit_at, hit_count, scope, group_id "
                "FROM memories WHERE bot_id=? AND uid=? AND COALESCE(status,'active')='active'",
                [BOT, UID], "美式咖啡", 10,
            )
            self.assertTrue(candidates)
            self.assertIn("美式咖啡", candidates[0]["content"])
        finally:
            store.close()

    def test_recall_2char_query_falls_back_to_like(self):
        """2 个字的查询 trigram 不支持，要自动退回 LIKE，不能变瞎。"""
        self.store.remember(BOT, UID, "他最近在喝咖啡", importance=0.6)
        hits = self.store.recall(BOT, UID, "咖啡", limit=3)
        self.assertTrue(hits)
        self.assertIn("咖啡", hits[0]["content"])


if __name__ == "__main__":
    unittest.main()
