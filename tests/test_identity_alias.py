# -*- coding: utf-8 -*-
"""身份别名：导入遗留的 (平台, uid) 自动换成规范身份。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import DataStore  # noqa: E402

BOT = "b"
OWNER_QQ = "302578963"
FEISHU = "ou_cec66"


def _tmpdir() -> str:
    base = os.environ.get("AMO_DATA_TEST_TMP") or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(dir=base)


class IdentityAliasTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir()
        self.store = DataStore(os.path.join(self.tmp, "a.db"))
        self.store.save_profile(BOT, OWNER_QQ, name="family")
        self.store.save_profile(BOT, FEISHU, name="family")
        self.pid = self.store.resolve_person(BOT, "aiocqhttp", OWNER_QQ, "family")
        # 规范身份先落地（线上是已经存在的飞书身份），别名再把遗留身份指过来
        self.store.resolve_person(BOT, "lark", FEISHU, "family")
        self.store.set_identity_alias(BOT, "lark", OWNER_QQ, "lark", FEISHU)
        self.store.set_identity_alias(BOT, "qq", OWNER_QQ, "aiocqhttp", OWNER_QQ)

    def tearDown(self):
        self.store.close()

    def test_legacy_lark_uid_resolves_to_feishu_identity(self):
        pid = self.store.resolve_person(BOT, "lark", OWNER_QQ)
        self.assertEqual(pid, self.pid)
        detail = self.store.person_of(BOT, "lark", OWNER_QQ)
        self.assertEqual(int(detail["pid"]), self.pid)
        links = {(l["platform"], l["uid"]) for l in detail.get("links") or []}
        self.assertIn(("lark", FEISHU), links)

    def test_legacy_qq_platform_resolves_to_aiocqhttp(self):
        pid = self.store.resolve_person(BOT, "qq", OWNER_QQ)
        self.assertEqual(pid, self.pid)
        detail = self.store.person_of(BOT, "qq", OWNER_QQ)
        self.assertEqual(int(detail["pid"]), self.pid)

    def test_alias_listed(self):
        rows = self.store.list_identity_aliases(BOT)
        self.assertTrue(any(r["platform"] == "lark" and r["alias_uid"] == FEISHU for r in rows))

    def test_unknown_identity_not_redirected(self):
        self.assertEqual(self.store.person_of(BOT, "lark", "someone-else"), {})


if __name__ == "__main__":
    unittest.main()
