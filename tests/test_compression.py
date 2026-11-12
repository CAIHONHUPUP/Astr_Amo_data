# -*- coding: utf-8 -*-
"""压缩/槽位相关的回归测试：丢内容、丢 uid、把「记住」当住址。"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import DataStore, _slot_facts  # noqa: E402

BOT = "bot1"
UID = "302578963"
UMO = "lark:FriendMessage:ou_cec66"


class SlotTests(unittest.TestCase):
    def test_remember_is_not_home(self):
        # 「记住北京的咖啡」里的「住」不是住址；否则会抽出假 home 并参与冲突判定
        self.assertEqual(_slot_facts("我记住北京的咖啡很好喝"), {})
        self.assertEqual(_slot_facts("我记住了他说的那家店"), {})

    def test_real_home_still_recognized(self):
        self.assertEqual(_slot_facts("我住在东莞东城").get("home"), "东莞东城")
        self.assertEqual(_slot_facts("他搬到深圳南山了").get("home"), "深圳南山")


class CompressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = DataStore(os.path.join(self.tmp, "c.db"))

    def tearDown(self):
        self.store.close()

    def test_message_summary_keeps_uid(self):
        # 3 天前的聊天会被压成 summary；摘要必须保留 uid，否则按 uid 的【按时间回看】取不到
        old = int(time.time()) - 5 * 86400
        for i in range(3):
            self.store.add_message(BOT, UMO, UID, "family", "user", "旧消息%d" % i, created_at=old + i)
        self.store.compress_messages(BOT, after_days=1, keep_days=30)
        rows = self.store.messages_in_span(BOT, old - 1, old + 10, umo=UMO, uid=UID)
        summaries = [row for row in rows if row["role"] == "summary"]
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["uid"], UID)
        self.assertIn("旧消息", summaries[0]["content"])

    def test_memory_merge_over_budget_keeps_rows_active(self):
        # 两段长摘要塞不进 4000 字：装不下的不能标 merged（否则正文永远召不回来）。
        # 2.8 起先按 800 字拆条再合并，所以这里断言"正文不丢 + 没有超长行"。
        # 注意 remember() 单条上限 2000 字（_clean），两段各 2000 字、合起来超预算。
        day = "2026-11-01"
        self.store.remember(BOT, UID, "%s %s" % (day, "甲" * 3000), kind="conversation_summary")
        self.store.remember(BOT, UID, "%s %s" % (day, "乙" * 3000), kind="conversation_summary")
        self.store.compress_memories(BOT, apply=True)
        rows = self.store.conn.execute(
            "SELECT id, content FROM memories WHERE bot_id=? AND uid=? "
            "AND kind='conversation_summary' AND COALESCE(status,'active')='active' "
            "ORDER BY id", (BOT, UID),
        ).fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(len(row["content"]) <= 800 for row in rows))
        joined = "".join(row["content"] for row in rows)
        # remember() 把单条截到 2000 字，两段各扣掉 "2026-11-01 " 前缀
        self.assertEqual(joined.count("甲"), 1989)
        self.assertEqual(joined.count("乙"), 1989)


class NearDuplicateEquivalenceTests(unittest.TestCase):
    """近重复合并（P2：token 集合改成建一次）必须照样能抓出该合并的那一对。"""

    PAIR_A = "他今天在公司加班到很晚才回家路上还下着雨衣服都湿透了"
    PAIR_B = "他今天在公司加班到很晚才回家路上还下着雨衣服都湿透了，还买了烤鱼"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = DataStore(os.path.join(self.tmp, "d.db"))

    def tearDown(self):
        self.store.close()

    def _fill(self, count):
        # 填充用完全不相干的句子，避免它们自己互相合并干扰断言
        words = ["蓝鲸", "青铜", "陶罐", "风筝", "苔藓", "铁轨", "磨坊", "沙丘",
                 "雨靴", "蒲扇", "灯笼", "沙漏", "芦苇", "铜锣", "瓦片", "竹筏"]
        for i in range(count):
            self.store.remember(BOT, UID, "%s%d 只是随手记第%d条，和别的事没关系"
                                % (words[i % len(words)], i, i))

    def _status(self, memory_id):
        # list_memories 不带 status 列，直接查库
        row = self.store.conn.execute(
            "SELECT status FROM memories WHERE id=?", (int(memory_id),)
        ).fetchone()
        if row is None:
            raise AssertionError("找不到记忆 %s" % memory_id)
        return row["status"] or "active"

    def test_duplicate_pair_found_among_many_rows(self):
        self._fill(60)
        a = self.store.remember(BOT, UID, self.PAIR_A)["memory_id"]
        b = self.store.remember(BOT, UID, self.PAIR_B)["memory_id"]
        self.store.compress_memories(BOT, apply=True)
        self.assertEqual(sorted([self._status(a), self._status(b)]), ["active", "merged"])

    def test_unrelated_rows_stay_active(self):
        self._fill(40)
        ids = [self.store.remember(BOT, UID, text)["memory_id"] for text in (self.PAIR_A, self.PAIR_B)]
        self.store.compress_memories(BOT, apply=False)
        self.assertEqual([self._status(i) for i in ids], ["active", "active"])

    def test_same_pair_detected_regardless_of_order(self):
        # 两行中间隔了 30 条无关记忆：倒排索引不该漏掉这一对
        first = self.store.remember(BOT, UID, self.PAIR_A)["memory_id"]
        self._fill(30)
        second = self.store.remember(BOT, UID, self.PAIR_B)["memory_id"]
        self.store.compress_memories(BOT, apply=True)
        self.assertEqual(sorted([self._status(first), self._status(second)]), ["active", "merged"])

    def test_old_full_scan_equivalent_on_300_rows(self):
        # 300 条规模下仍然能找出唯一的一对近重复（token 集合只建一次，不再每对重建）
        self._fill(300)
        a = self.store.remember(BOT, UID, self.PAIR_A)["memory_id"]
        b = self.store.remember(BOT, UID, self.PAIR_A + "，后来又补了一句")["memory_id"]
        self.store.compress_memories(BOT, apply=True)
        self.assertEqual(sorted([self._status(a), self._status(b)]), ["active", "merged"])


if __name__ == "__main__":
    unittest.main()
