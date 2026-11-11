# -*- coding: utf-8 -*-
"""画像改名后，人物卡（persons）的名字要跟着更新。"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import DataStore  # noqa: E402

BOT = "大肥鱼"
UID = "302578963"


class PersonNameSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="amo-person-name-")
        self.store = DataStore(os.path.join(self.tmp, "a.db"))

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rename_updates_person(self):
        self.store.save_profile(BOT, UID, name="dafeiyu")
        self.store.resolve_person(BOT, "aiocqhttp", UID, "dafeiyu")
        self.assertEqual(self.store.person_of(BOT, "aiocqhttp", UID)["name"], "dafeiyu")
        self.store.save_profile(BOT, UID, name="family")
        self.assertEqual(self.store.person_of(BOT, "aiocqhttp", UID)["name"], "family")

    def test_empty_name_keeps_person(self):
        self.store.save_profile(BOT, UID, name="family")
        self.store.resolve_person(BOT, "aiocqhttp", UID, "family")
        self.store.save_profile(BOT, UID, alias="老公")
        self.assertEqual(self.store.person_of(BOT, "aiocqhttp", UID)["name"], "family")


if __name__ == "__main__":
    unittest.main()
