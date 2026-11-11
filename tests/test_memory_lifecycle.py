# -*- coding: utf-8 -*-
"""记忆生命周期（冲突过期 / 归档 / 压缩）的单测。"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import DataStore  # noqa: E402

BOT = "bot1"
UID = "302578963"


def _tmpdir() -> str:
    base = os.environ.get("AMO_DATA_TEST_TMP") or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(dir=base)


class SupersedeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir()
        self.store = DataStore(os.path.join(self.tmp, "amo.db"))

    def tearDown(self):
        self.store.close()

    def _status(self, mid):
        row = self.store.conn.execute(
            "SELECT status, superseded_by FROM memories WHERE id=?", (mid,)
        ).fetchone()
        return row["status"], row["superseded_by"]

    def test_new_home_supersedes_old(self):
        old = self.store.remember(BOT, UID, "主人住在东莞东兴路一带。", importance=0.7)
        new = self.store.remember(BOT, UID, "主人搬到深圳南山住了。", importance=0.7)
        self.assertEqual(new["superseded"], [old["memory_id"]])
        self.assertEqual(self._status(old["memory_id"])[0], "superseded")
        rows = self.store.recall(BOT, UID, "主人住在哪")
        self.assertTrue(rows)
        self.assertIn("深圳", rows[0]["content"])
        self.assertFalse(any("东兴路" in r["content"] for r in rows))

    def test_unrelated_fact_does_not_supersede(self):
        old = self.store.remember(BOT, UID, "主人住在东莞东兴路。", importance=0.7)
        new = self.store.remember(BOT, UID, "主人喜欢喝美式咖啡。", importance=0.5)
        self.assertEqual(new["superseded"], [])
        self.assertEqual(self._status(old["memory_id"])[0], "active")

    def test_same_value_not_superseded(self):
        a = self.store.remember(BOT, UID, "主人住在东莞东兴路。")
        b = self.store.remember(BOT, UID, "他住在东莞东兴路那边。")
        self.assertEqual(b["superseded"], [])
        self.assertEqual(self._status(a["memory_id"])[0], "active")

    def test_manual_supersede(self):
        a = self.store.remember(BOT, UID, "主人养了一只猫。")
        b = self.store.remember(BOT, UID, "主人养了一只狗。")
        self.assertTrue(self.store.mark_superseded(a["memory_id"], b["memory_id"]))
        self.assertEqual(self._status(a["memory_id"])[0], "superseded")


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir()
        self.store = DataStore(os.path.join(self.tmp, "amo.db"))

    def tearDown(self):
        self.store.close()

    def test_archive_stale_only(self):
        old = int(time.time()) - 200 * 86400
        stale = self.store.remember(BOT, UID, "很久以前的一件小事", importance=0.2,
                                    created_at=old, last_hit_at=old)
        self.store.remember(BOT, UID, "最近的重要事情，生日是十二月十号", importance=0.9)
        result = self.store.archive_stale(BOT, days=90, max_importance=0.3)
        self.assertEqual(result["archived"], 1)
        status = self.store.conn.execute(
            "SELECT status FROM memories WHERE id=?", (stale["memory_id"],)
        ).fetchone()["status"]
        self.assertEqual(status, "archived")
        rows = self.store.recall(BOT, UID, "很久以前的小事")
        self.assertFalse(any("很久以前的一件小事" in r["content"] for r in rows))
        self.assertTrue(self.store.recall(BOT, UID, "生日"))

    def test_archive_dry_run_changes_nothing(self):
        old = int(time.time()) - 200 * 86400
        item = self.store.remember(BOT, UID, "很旧的小事", importance=0.2,
                                   created_at=old, last_hit_at=old)
        result = self.store.archive_stale(BOT, days=90, max_importance=0.3, apply=False)
        self.assertEqual(result["candidate"], 1)
        self.assertEqual(result["archived"], 0)
        row = self.store.conn.execute(
            "SELECT status FROM memories WHERE id=?", (item["memory_id"],)
        ).fetchone()
        self.assertEqual(row["status"], "active")


class CompressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir()
        # 关掉写入时的自动去重，专门测"周度整理"这一步
        self.store = DataStore(os.path.join(self.tmp, "amo.db"),
                               settings={"auto_dedupe": False})

    def tearDown(self):
        self.store.close()

    def test_same_day_summaries_merge(self):
        a = self.store.remember(BOT, UID, "2026-11-08 下午和主人聊了工作。", kind="conversation_summary")
        b = self.store.remember(BOT, UID, "2026-11-08 晚上和主人聊了天气和晚饭。", kind="conversation_summary")
        result = self.store.compress_memories(BOT, apply=True)
        self.assertEqual(result["merged_summaries"], 1)
        statuses = [
            self.store.conn.execute("SELECT status FROM memories WHERE id=?", (item["memory_id"],))
            .fetchone()["status"]
            for item in (a, b)
        ]
        self.assertIn("merged", statuses)
        self.assertIn("active", statuses)

    def test_near_duplicate_merge(self):
        a = self.store.remember(BOT, UID, "主人的工作作息是中午有休息时间，下午五点半下班。")
        b = self.store.remember(BOT, UID, "主人的工作作息是中午有休息时间，下午五点半下班啦。")
        result = self.store.compress_memories(BOT, apply=True)
        self.assertGreaterEqual(result["merged_near_duplicates"], 1)
        statuses = [
            self.store.conn.execute("SELECT status FROM memories WHERE id=?", (item["memory_id"],))
            .fetchone()["status"]
            for item in (a, b)
        ]
        self.assertIn("merged", statuses)

    def test_information_rich_pair_not_merged(self):
        """一条是另一条的子集时不能合并，否则会丢信息。"""
        a = self.store.remember(BOT, UID, "主人的工作作息是中午有休息时间，下午五点半下班。")
        b = self.store.remember(BOT, UID, "主人的作息是中午休息一会儿，下午五点半就下班回家。")
        result = self.store.compress_memories(BOT, apply=True)
        self.assertEqual(result["merged_near_duplicates"], 0)
        statuses = {
            self.store.conn.execute("SELECT status FROM memories WHERE id=?", (item["memory_id"],))
            .fetchone()["status"]
            for item in (a, b)
        }
        self.assertEqual(statuses, {"active"})

    def test_stats_include_status(self):
        self.store.remember(BOT, UID, "一条普通记忆")
        stats = self.store.stats(BOT)
        self.assertGreaterEqual(stats.get("memories_active", 0), 1)
        self.assertIn("memories_archived", stats)


if __name__ == "__main__":
    unittest.main()
