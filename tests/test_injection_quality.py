# -*- coding: utf-8 -*-
"""注入质量：多样化、阶段记忆加成、合并保留全文。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import DataStore  # noqa: E402
import clock as amo_clock  # noqa: E402

BOT = "b"
UID = "u"


def _tmpdir() -> str:
    base = os.environ.get("AMO_DATA_TEST_TMP") or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(dir=base)


class DiversityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir()
        self.store = DataStore(os.path.join(self.tmp, "a.db"), settings={"auto_dedupe": False})

    def tearDown(self):
        self.store.close()

    def test_near_duplicate_memories_not_all_returned(self):
        self.store.remember(BOT, UID, "主人喜欢喝美式咖啡，每天早上都要一杯。", importance=0.7)
        self.store.remember(BOT, UID, "主人喜欢喝美式咖啡，每天早上都要来一杯。", importance=0.65)
        self.store.remember(BOT, UID, "主人养了一只叫团子的橘猫。", importance=0.6)
        rows = self.store.recall(BOT, UID, "主人喜欢喝什么咖啡", limit=3)
        contents = [r["content"] for r in rows]
        self.assertEqual(len([c for c in contents if "美式咖啡" in c]), 1)

    def test_diversity_can_be_disabled(self):
        store = DataStore(os.path.join(self.tmp, "b.db"), settings={"auto_dedupe": False, "diversify_ratio": 0})
        try:
            store.remember(BOT, UID, "主人喜欢喝美式咖啡，每天早上都要一杯。", importance=0.7)
            store.remember(BOT, UID, "主人喜欢喝美式咖啡，每天早上都要来一杯。", importance=0.65)
            rows = store.recall(BOT, UID, "主人喜欢喝什么咖啡", limit=3)
            self.assertEqual(len([r for r in rows if "美式咖啡" in r["content"]]), 2)
        finally:
            store.close()


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir()
        self.store = DataStore(os.path.join(self.tmp, "a.db"), settings={"auto_dedupe": False})

    def tearDown(self):
        self.store.close()

    def test_daily_digest_ranks_for_related_query(self):
        self.store.remember(BOT, UID, "【2026-11-08】他当天在家修 bug，下午出门拿快递，晚上吃了猪肉粽。",
                            kind="daily_digest", importance=0.75)
        rows = self.store.recall(BOT, UID, "昨天下午我做了什么", limit=3)
        self.assertTrue(rows)
        self.assertEqual(rows[0]["kind"], "daily_digest")


class SummaryMergeKeepsTextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir()
        self.store = DataStore(os.path.join(self.tmp, "a.db"))

    def tearDown(self):
        self.store.close()


    def test_yesterday_question_pulls_that_day_digest(self):
        from datetime import timedelta
        # 「昨天」按插件配置时区算，不能用系统 date.today()：CI 是 UTC，
        # 北京时间 00:00-08:00 这段系统日期还停在"今天"，测试会假失败。
        tag = (amo_clock.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        self.store.remember(BOT, UID, "【" + tag + "】他当天在家修 bug，中午吃了猪肉粽。",
                            kind="daily_digest", importance=0.7)
        rows = self.store.recall(BOT, UID, "昨天我们主要干了什么", limit=3)
        self.assertTrue(rows)
        self.assertEqual(rows[0]["kind"], "daily_digest")

    def test_time_window_uses_configured_timezone(self):
        """「昨天」的起点必须是配置时区的零点，不是服务器时区的零点。"""
        from datetime import datetime, timedelta
        amo_clock.configure("Asia/Shanghai")
        try:
            start, _ = DataStore._time_window("昨天")
            zone = amo_clock.get_zone("Asia/Shanghai")
            expected = datetime.combine(
                amo_clock.today() - timedelta(days=1), datetime.min.time()
            ).replace(tzinfo=zone).timestamp()
            self.assertEqual(start, int(expected))
        finally:
            amo_clock.restore()

    def test_merged_summary_keeps_all_text(self):
        a = self.store.remember(BOT, UID, "2026-11-08 上午和主人聊了修 bug。", kind="conversation_summary")
        b = self.store.remember(BOT, UID, "2026-11-08 晚上和主人聊了晚饭和天气。", kind="conversation_summary")
        self.store.compress_memories(BOT, apply=True)
        keeper = self.store.conn.execute(
            "SELECT content FROM memories WHERE status='active' AND kind='conversation_summary'"
        ).fetchone()
        self.assertIn("修 bug", keeper["content"])
        self.assertIn("晚饭和天气", keeper["content"])


if __name__ == "__main__":
    unittest.main()
