# -*- coding: utf-8 -*-
"""记忆备份与导出/导入。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import DataStore  # noqa: E402

BOT = "b"
A = "302578963"


def _tmpdir() -> str:
    base = os.environ.get("AMO_DATA_TEST_TMP") or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(dir=base)


class ExportImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir()
        self.store = DataStore(os.path.join(self.tmp, "a.db"))
        self.store.save_profile(BOT, A, name="family", alias="老公", relation="相恋")
        self.store.remember(BOT, A, "主人喜欢喝美式咖啡", importance=0.7)
        self.store.remember(BOT, A, "主人生日是十二月十号", importance=0.8)
        self.store.save_relation(BOT, A, "470488586", "相恋", "主人 × 她")
        self.store.resolve_person(BOT, "aiocqhttp", A, "family")
        self.store.set_identity_alias(BOT, "qq", A, "aiocqhttp", A)

    def tearDown(self):
        self.store.close()

    def test_export_contains_everything(self):
        snap = self.store.export_snapshot(BOT)
        self.assertEqual(snap["format"], "astr_amo_data")
        self.assertEqual(len(snap["memories"]), 2)
        self.assertEqual(len(snap["profiles"]), 1)
        self.assertEqual(len(snap["relations"]), 1)
        self.assertEqual(len(snap["person_links"]), 1)
        self.assertEqual(len(snap["identity_alias"]), 1)
        self.assertEqual(snap["counts"]["memories"], 2)

    def test_import_roundtrip_into_fresh_store(self):
        snap = self.store.export_snapshot(BOT)
        other = DataStore(os.path.join(self.tmp, "b.db"))
        try:
            counts = other.import_snapshot(snap)
            self.assertEqual(counts["memories"], 2)
            self.assertEqual(len(other.list_memories(BOT, A, 10)), 2)
            self.assertEqual(other.get_profile(BOT, A).get("name"), "family")
            self.assertEqual(len(other.list_relations(BOT)), 1)
            self.assertEqual(len(other.list_persons(BOT)), 1)
        finally:
            other.close()

    def test_import_twice_does_not_duplicate(self):
        snap = self.store.export_snapshot(BOT)
        other = DataStore(os.path.join(self.tmp, "c.db"))
        try:
            other.import_snapshot(snap)
            other.import_snapshot(snap)
            self.assertEqual(len(other.list_memories(BOT, A, 50)), 2)
        finally:
            other.close()

    def test_backup_file_and_prune(self):
        dest = os.path.join(self.tmp, "backups")
        paths = []
        for i in range(4):
            path = self.store.backup_database(dest, keep=2)
            paths.append(path)
            self.assertTrue(os.path.isfile(path))
        # 同一秒内文件名相同会覆盖，改用列表接口确认保留数量 <= keep
        items = self.store.list_backups(dest)
        self.assertLessEqual(len(items), 2)
        self.assertTrue(items)
        self.assertGreater(os.path.getsize(items[0]["name"] and os.path.join(dest, items[0]["name"])), 0)

    def test_load_snapshot_file(self):
        dest = os.path.join(self.tmp, "backups")
        path = self.store.backup_database(dest, keep=5)
        snap = self.store.load_snapshot_file(path, BOT)
        self.assertEqual(snap["format"], "astr_amo_data")
        self.assertEqual(len(snap["memories"]), 2)

    def test_import_does_not_overwrite_richer_existing_memory(self):
        other = DataStore(os.path.join(self.tmp, "d.db"))
        try:
            # 现在的库里是一条更完整的记忆
            self.store.remember(BOT, A, "主人喜欢喝美式咖啡，不加糖，早上一定要喝一杯", importance=0.8)
            other.remember(BOT, A, "主人喜欢喝美式咖啡", importance=0.5)
            snapshot = other.export_snapshot(BOT)
            self.store.import_snapshot(snapshot)
            contents = [row["content"] for row in self.store.list_memories(BOT, A, 50)]
            self.assertIn("主人喜欢喝美式咖啡，不加糖，早上一定要喝一杯", contents)
        finally:
            other.close()

    def test_backup_names_are_unique(self):
        dest = os.path.join(self.tmp, "backups2")
        first = self.store.backup_database(dest, keep=10)
        second = self.store.backup_database(dest, keep=10)
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.store.list_backups(dest)), 2)


if __name__ == "__main__":
    unittest.main()
