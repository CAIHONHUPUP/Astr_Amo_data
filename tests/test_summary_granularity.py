# -*- coding: utf-8 -*-
"""摘要颗粒度（拆分/合并上限）与元问题兜底的回归测试。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import DataStore  # noqa: E402
from store_memory import _is_meta_query, _split_long_text  # noqa: E402

BOT = "bot1"
UID = "302578963"


def _sentences(chars, prefix="故事"):
    """造一段可切分的中文：每句 20 字，句号结尾。"""
    unit = (prefix + "甲乙丙丁戊己庚辛壬癸") * 2
    pieces = []
    total = 0
    i = 0
    while total < chars:
        piece = "%02d%s。" % (i, unit)
        pieces.append(piece)
        total += len(piece)
        i += 1
    return "".join(pieces)


class SplitTextTests(unittest.TestCase):
    def test_short_text_unchanged(self):
        self.assertEqual(_split_long_text("很短。", 100), ["很短。"])

    def test_long_text_split_without_loss(self):
        text = _sentences(2000)
        pieces = _split_long_text(text, 300)
        self.assertGreater(len(pieces), 1)
        self.assertEqual("".join(pieces), text)
        self.assertTrue(all(len(piece) <= 300 for piece in pieces))

    def test_single_huge_sentence_is_hard_cut(self):
        text = "啊" * 500
        pieces = _split_long_text(text, 120)
        self.assertEqual("".join(pieces), text)
        self.assertTrue(all(len(piece) <= 120 for piece in pieces))

    def test_empty_text(self):
        self.assertEqual(_split_long_text("", 100), [])

    def test_zero_limit_disables(self):
        self.assertEqual(_split_long_text("abc", 0), ["abc"])


class MetaQueryTests(unittest.TestCase):
    def test_patterns(self):
        self.assertTrue(_is_meta_query("你还记得我吗"))
        self.assertTrue(_is_meta_query("你记得我说过什么吗？"))
        self.assertTrue(_is_meta_query("你知道我是谁吗"))
        self.assertFalse(_is_meta_query("你记得我生日吗"))
        self.assertFalse(_is_meta_query("今天天气怎么样"))

    def test_fallback_returns_important_memories(self):
        tmp = tempfile.mkdtemp()
        store = DataStore(os.path.join(tmp, "s.db"))
        try:
            store.remember(BOT, UID, "他养了一只叫团子的猫", kind="fact", importance=0.9)
            store.remember(BOT, UID, "他喜欢喝冰美式", kind="fact", importance=0.6)
            items = store.recall(BOT, UID, "你还记得我吗", limit=6)
            self.assertTrue(items)
            self.assertTrue(all(item.get("meta_fallback") for item in items))
            self.assertLessEqual(len(items), 3)
            self.assertEqual(items[0]["importance"], 0.9)
        finally:
            store.close()

    def test_fallback_off_returns_empty(self):
        tmp = tempfile.mkdtemp()
        store = DataStore(os.path.join(tmp, "s.db"),
                          {"meta_query_fallback": False})
        try:
            store.remember(BOT, UID, "他养了一只叫团子的猫", importance=0.9)
            self.assertEqual(store.recall(BOT, UID, "你还记得我吗"), [])
        finally:
            store.close()

    def test_normal_query_not_polluted(self):
        tmp = tempfile.mkdtemp()
        store = DataStore(os.path.join(tmp, "s.db"))
        try:
            store.remember(BOT, UID, "他养了一只叫团子的猫", importance=0.9)
            self.assertEqual(store.recall(BOT, UID, "你记得我生日吗"), [])
        finally:
            store.close()

    def test_fallback_respects_scope(self):
        tmp = tempfile.mkdtemp()
        store = DataStore(os.path.join(tmp, "s.db"))
        try:
            store.remember(BOT, UID, "群里有人约周末打球", importance=0.9,
                           scope="group", group_id="g1")
            self.assertEqual(store.recall(BOT, UID, "你还记得我吗", scope="private"), [])
            items = store.recall(BOT, UID, "你还记得我吗", scope="group", group_id="g1")
            self.assertEqual(len(items), 1)
        finally:
            store.close()

    def test_fallback_updates_hit_count(self):
        tmp = tempfile.mkdtemp()
        store = DataStore(os.path.join(tmp, "s.db"))
        try:
            store.remember(BOT, UID, "他养了一只叫团子的猫", importance=0.9)
            store.recall(BOT, UID, "你还记得我吗")
            rows = store.list_memories(BOT, UID)
            self.assertGreaterEqual(rows[0]["hit_count"], 1)
        finally:
            store.close()


class SummaryGranularityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = DataStore(os.path.join(self.tmp, "s.db"),
                               {"summary_merge_max_chars": 800})

    def tearDown(self):
        self.store.close()

    def test_split_long_summary_into_rows(self):
        day = "2026-11-01"
        text = day + " " + _sentences(2000)
        result = self.store.remember(BOT, UID, text, kind="conversation_summary", importance=0.8)
        self.assertTrue(result.get("ok"))
        out = self.store.split_long_summaries(BOT, 800)
        self.assertEqual(out["summaries"], 1)
        self.assertGreaterEqual(out["created"], 2)
        rows = [r for r in self.store.list_memories(BOT, UID, limit=50)
                if r["kind"] == "conversation_summary"]
        self.assertGreaterEqual(len(rows), 3)
        self.assertTrue(all(len(r["content"]) <= 800 for r in rows))

    def test_split_preview_does_not_write(self):
        self.store.remember(BOT, UID, "2026-11-01 " + _sentences(2000),
                            kind="conversation_summary")
        before = len(self.store.list_memories(BOT, UID, limit=100))
        out = self.store.split_long_summaries(BOT, 800, apply=False)
        self.assertEqual(out["summaries"], 1)
        self.assertGreaterEqual(out["created"], 2)
        after = len(self.store.list_memories(BOT, UID, limit=100))
        self.assertEqual(before, after)

    def test_merge_never_exceeds_cap(self):
        day = "2026-11-02"
        a = day + " " + _sentences(500, prefix="上班")
        b = day + " " + _sentences(500, prefix="晚饭")
        self.store.remember(BOT, UID, a, kind="conversation_summary", importance=0.6)
        self.store.remember(BOT, UID, b, kind="conversation_summary", importance=0.6)
        out = self.store.compress_memories(BOT, apply=True)
        self.assertIn("split_summaries", out)
        rows = [r for r in self.store.list_memories(BOT, UID, limit=100)
                if r["kind"] == "conversation_summary"]
        self.assertTrue(rows)
        self.assertTrue(all(len(r["content"]) <= 800 for r in rows))

    def test_compress_reports_split_preview(self):
        self.store.remember(BOT, UID, "2026-11-03 " + _sentences(1500),
                            kind="conversation_summary")
        out = self.store.compress_memories(BOT, apply=False)
        self.assertGreaterEqual(out["split_summaries"], 1)
        self.assertGreaterEqual(out["split_pieces"], 1)


if __name__ == "__main__":
    unittest.main()
