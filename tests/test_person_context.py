# -*- coding: utf-8 -*-
"""跨平台人物上下文：每个平台各取最近几条，别让话多的平台挤掉另一个。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import DataStore  # noqa: E402

BOT = "bot1"
QQ_UID = "302578963"
LARK_UID = "ou_cec66"


def _tmpdir() -> str:
    base = os.environ.get("AMO_DATA_TEST_TMP") or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(dir=base)


class PersonContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir()
        self.store = DataStore(os.path.join(self.tmp, "amo.db"))
        self.store.save_profile(BOT, QQ_UID, name="family")
        self.store.save_profile(BOT, LARK_UID, name="family")
        a = self.store.resolve_person(BOT, "aiocqhttp", QQ_UID, "family")
        b = self.store.resolve_person(BOT, "lark", LARK_UID, "family")
        if a != b:
            self.store.merge_persons(BOT, a, b)

    def tearDown(self):
        self.store.close()

    def _add(self, platform, uid, text, ts):
        umo = ("aiocqhttp" if platform == "aiocqhttp" else platform) + ":FriendMessage:" + uid
        self.store.add_message(BOT, umo, uid, "他", "user", text, created_at=ts)

    def test_each_platform_is_represented(self):
        base = 1_800_000_000
        # QQ 这边聊了 10 句，飞书只有 1 句：不能因为 QQ 多就把飞书挤没
        for i in range(10):
            self._add("aiocqhttp", QQ_UID, "QQ第%d句" % i, base + i)
        self._add("lark", LARK_UID, "飞书唯一一句", base - 100)
        ctx = self.store.person_context(BOT, "aiocqhttp", QQ_UID, 6)
        platforms = {m["platform"] for m in ctx.get("recent") or []}
        self.assertEqual(platforms, {"aiocqhttp", "lark"})
        self.assertTrue(any("飞书唯一一句" in m["content"] for m in ctx["recent"]))
        self.assertEqual([o["platform"] for o in ctx.get("others") or []], ["lark"])


    def test_same_uid_on_other_platform_merges_automatically(self):
        """导入的飞书记录里 subject_id 还是 QQ 号：应并到已有那个人，不新建「他」。"""
        self._add("aiocqhttp", QQ_UID, "QQ上说了一句", 1_800_000_000)
        # 飞书侧 uid 仍是 QQ 号（历史导入常见）
        self.store.add_message(BOT, "lark:FriendMessage:" + QQ_UID, QQ_UID, "他", "user",
                               "飞书上说了一句", created_at=1_800_000_100)
        pid_qq = self.store.resolve_person(BOT, "aiocqhttp", QQ_UID)
        pid_lark = self.store.resolve_person(BOT, "lark", QQ_UID)
        self.assertEqual(pid_qq, pid_lark)
        names = [p["name"] for p in self.store.list_persons(BOT)]
        self.assertEqual(len(names), 1)

    def test_latest_lark_message_with_canonical_uid_is_seen(self):
        # 飞书消息落库时 uid 可能已归一成 QQ 号；按 open_id 查会漏掉最新消息
        base = 1_800_000_000
        self._add("lark", LARK_UID, "飞书旧消息", base)
        self._add("lark", QQ_UID, "飞书最新消息", base + 100)
        ctx = self.store.person_context(BOT, "lark", LARK_UID, 6)
        texts = [m["content"] for m in ctx.get("recent") or []]
        self.assertIn("飞书最新消息", texts)

    def test_unknown_person_returns_empty(self):
        self.assertEqual(self.store.person_context(BOT, "aiocqhttp", "no-such-uid", 6), {})


if __name__ == "__main__":
    unittest.main()
