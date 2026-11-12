# -*- coding: utf-8 -*-
"""补覆盖率：时间窗解析、导入快照、备份列表、时钟兜底、向量后台任务。"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import clock  # noqa: E402
from store import DataStore  # noqa: E402

BOT = "bot1"
UID = "302578963"


def _store(settings=None):
    tmp = tempfile.mkdtemp()
    return DataStore(os.path.join(tmp, "s.db"), settings or {})


class TimeWindowTests(unittest.TestCase):
    CASES = [
        "大前天", "前天", "昨天", "昨天下午", "昨晚", "今天", "今天下午",
        "上周三", "上上周一", "这周五", "礼拜天", "周天",
        "11月6号", "11月6号晚上", "2026年11月6号", "6号",
        "上个月", "上月", "最近3天", "最近7天",
    ]

    def test_known_windows_are_ordered(self):
        for query in self.CASES:
            start, end = DataStore._time_window(query)
            self.assertGreater(start, 0, query)
            self.assertGreaterEqual(end, start, query)

    def test_unknown_window_is_empty(self):
        self.assertEqual(DataStore._time_window("随便聊聊"), (0, 0))
        self.assertEqual(DataStore._time_window(""), (0, 0))

    def test_invalid_date_falls_back_or_empty(self):
        for query in ("2月30号", "31号", "13月1号"):
            start, end = DataStore._time_window(query)
            self.assertGreaterEqual(start, 0, query)
            self.assertGreaterEqual(end, start, query)

    def test_cn_int(self):
        self.assertEqual(DataStore._cn_int("6"), 6)
        self.assertEqual(DataStore._cn_int("十一"), 11)
        self.assertEqual(DataStore._cn_int("二十三"), 23)
        self.assertEqual(DataStore._cn_int("十"), 10)
        self.assertEqual(DataStore._cn_int("瞎写"), 0)

    def test_day_window_periods(self):
        from datetime import datetime

        day = datetime(2026, 11, 12)
        for word in ("凌晨", "早上", "中午", "下午", "晚上", "深夜"):
            start, end = DataStore._day_window(day, word)
            self.assertLess(start, end)
        start, end = DataStore._day_window(day, "没有时段词")
        self.assertEqual((end - start).days, 1)


class ClockFallbackTests(unittest.TestCase):
    def test_fixed_offset(self):
        zone = clock._FixedOffset(8)
        self.assertEqual(zone.utcoffset(None).total_seconds(), 8 * 3600)
        self.assertEqual(zone.dst(None).total_seconds(), 0)
        self.assertIn("UTC", zone.tzname(None))

    def test_get_zone_unknown_and_empty(self):
        self.assertIsNone(clock.get_zone(""))
        self.assertIsNone(clock.get_zone("Not/AZone"))
        self.assertIsNotNone(clock.get_zone("Asia/Shanghai"))

    def test_configure_and_now(self):
        name = clock.configure("Asia/Shanghai")
        self.assertEqual(name, "Asia/Shanghai")
        self.assertTrue(clock.now().year >= 2026)
        self.assertEqual(len(clock.today_str()), 10)
        self.assertIsInstance(clock.utc_offset_minutes(), int)
        self.assertTrue(clock.sql_offset_modifier().endswith(" minutes"))
        self.assertIsNotNone(clock.from_timestamp(time.time()))
        self.assertIsNotNone(clock.from_timestamp(time.time(), "Asia/Shanghai"))

    def test_configure_normalizes_auto(self):
        self.assertEqual(clock.configure("auto"), clock.current_name())

    def test_restore_is_safe(self):
        clock.configure("Asia/Shanghai")
        clock.restore()
        self.assertFalse(clock.applied())

    def test_tzset_failure_branch(self):
        class _NoTz:
            @staticmethod
            def tzset():
                raise RuntimeError("no tzset")

        original = clock._time
        clock._time = _NoTz
        try:
            clock.configure("Asia/Shanghai")
            self.assertFalse(clock.applied())
        finally:
            clock._time = original
            clock.restore()


class SnapshotImportEdgeTests(unittest.TestCase):
    def setUp(self):
        self.store = _store()

    def tearDown(self):
        self.store.close()

    def test_non_dict_returns_zero(self):
        self.assertEqual(self.store.import_snapshot([])["memories"], 0)

    def test_import_edges(self):
        snapshot = {
            "bot_id": BOT,
            "memories": [
                {"uid": "", "content": "缺人"},
                {"uid": UID, "content": ""},
                {"uid": UID, "content": "他喜欢跑步"},
                {"uid": UID, "content": "他喜欢跑步"},  # 重复
                {"uid": UID, "content": "他搬家了", "status": "superseded", "superseded_by": 1},
            ],
            "profiles": [
                {"uid": ""},
                {"uid": UID, "name": "阿明", "updated_at": "坏时间"},
            ],
            "relations": [
                {"a_uid": UID, "b_uid": "u2", "relation": "朋友"},
                {"a_uid": "", "b_uid": "u2"},
            ],
            "person_links": [
                {"uid": ""},
                {"platform": "lark", "uid": UID, "name": "阿明"},
            ],
            "identity_alias": [
                {"platform": "lark", "uid": UID, "alias_platform": "aiocqhttp", "alias_uid": "1"},
                {"platform": "", "uid": ""},
            ],
        }
        counts = self.store.import_snapshot(snapshot)
        self.assertGreaterEqual(counts["memories"], 2)
        self.assertEqual(counts["skipped"], 1)
        self.assertGreaterEqual(counts["profiles"], 1)
        self.assertGreaterEqual(counts["relations"], 1)
        self.assertGreaterEqual(counts["links"], 1)
        self.assertGreaterEqual(counts["aliases"], 1)

    def test_import_older_profile_not_overwritten(self):
        self.store.save_profile(BOT, UID, name="新名字")
        self.store.conn.execute(
            "UPDATE profiles SET updated_at=? WHERE bot_id=? AND uid=?",
            (int(time.time()) + 100, BOT, UID),
        )
        self.store.conn.commit()
        self.store.import_snapshot({
            "bot_id": BOT,
            "profiles": [{"uid": UID, "name": "旧名字", "updated_at": 1}],
        })
        self.assertEqual(self.store.get_profile(BOT, UID)["name"], "新名字")


class BackupEdgeTests(unittest.TestCase):
    def setUp(self):
        self.store = _store()

    def tearDown(self):
        self.store.close()

    def test_prune_bad_keep_and_missing_dir(self):
        self.assertEqual(self.store.prune_backups(os.path.join(tempfile.mkdtemp(), "nope")), 0)
        dest = tempfile.mkdtemp()
        self.assertEqual(self.store.prune_backups(dest, "bad"), 0)

    def test_list_backups_missing_dir(self):
        self.assertEqual(self.store.list_backups(os.path.join(tempfile.mkdtemp(), "nope")), [])

    def test_backup_collision_name(self):
        dest = tempfile.mkdtemp()
        first = self.store.backup_database(dest, keep=5)
        second = self.store.backup_database(dest, keep=5)
        self.assertNotEqual(first, second)
        self.assertTrue(self.store.list_backups(dest))

    def test_stats_with_bot_and_statuses(self):
        self.store.remember(BOT, UID, "他喜欢喝茶")
        self.store.add_message(BOT, "aiocqhttp:private:%s" % UID, UID, "他", "user", "在吗")
        stats = self.store.stats(BOT)
        self.assertGreaterEqual(stats["memories"], 1)
        self.assertIn("memories_active", stats)
        self.assertGreaterEqual(stats["messages"], 1)
        self.assertEqual(self.store.stats("other")["memories"], 0)


class EmbeddingWorkerTests(unittest.TestCase):
    def test_worker_writes_vector(self):
        store = _store({"embedding_enabled": True, "embedding_backfill": False})
        try:
            store._embed = lambda text: [0.1, 0.2, 0.3]
            result = store.remember(BOT, UID, "他喜欢冰美式")
            memory_id = result["memory_id"]
            for _ in range(40):
                row = store.conn.execute(
                    "SELECT COUNT(*) AS n FROM memory_vec WHERE memory_id=?", (memory_id,)
                ).fetchone()
                if row["n"]:
                    break
                time.sleep(0.05)
            self.assertEqual(row["n"], 1)
        finally:
            store.close()

    def test_backfill_embeddings(self):
        store = _store({"embedding_enabled": True, "embedding_backfill": False})
        try:
            store.remember(BOT, UID, "他养了一只猫")
            store._embed = lambda text: [0.5, 0.5]
            count = store.backfill_embeddings(limit=10)
            self.assertGreaterEqual(count, 1)
        finally:
            store.close()

    def test_embed_down_returns_empty(self):
        store = _store({"embedding_enabled": True, "embedding_backfill": False})
        try:
            store._emb_down_until = time.time() + 60
            self.assertEqual(store._embed("hi"), [])
            store._emb_down_until = 0
            store.settings["embedding_base"] = ""
            self.assertEqual(store._embed("hi"), [])
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
