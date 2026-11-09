# -*- coding: utf-8 -*-
"""关系图的显示名：画像优先，其次人物卡。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import DataStore  # noqa: E402

BOT = "b"
A = "302578963"
B = "470488586"


def _tmpdir() -> str:
    base = os.environ.get("AMO_DATA_TEST_TMP") or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(dir=base)


class RelationNameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir()
        self.store = DataStore(os.path.join(self.tmp, "a.db"))
        self.store.save_profile(BOT, A, name="family")
        self.other = self.store.resolve_person(BOT, "aiocqhttp", B, "她")

    def tearDown(self):
        self.store.close()

    def test_names_filled_from_profile_and_person(self):
        self.store.save_relation(BOT, A, B, "相恋", "主人 × 她")
        rows = self.store.list_relations(BOT)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["relation"], "相恋")
        self.assertEqual(rows[0]["a_name"], "family")
        self.assertEqual(rows[0]["b_name"], "她")

    def test_unknown_uid_has_empty_name(self):
        self.store.save_relation(BOT, A, "no-such-uid", "朋友")
        rows = self.store.list_relations(BOT)
        self.assertEqual(rows[0]["b_name"], "")


if __name__ == "__main__":
    unittest.main()
