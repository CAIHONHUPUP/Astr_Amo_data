# -*- coding: utf-8 -*-
"""删画像要连陪伴插件本地一起删，否则 amo 每 10 分钟对账会把它推回来。"""

from __future__ import annotations

import os
import sys
import types
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import astrbot_stub  # noqa: E402

astrbot_stub.install()

import main as data_main  # noqa: E402


class _FakeDB:
    def __init__(self):
        self.deleted = []

    def delete_profile(self, uid):
        self.deleted.append(uid)


class _FakeAmo:
    def __init__(self):
        self.db = _FakeDB()


class _Ctx:
    def __init__(self, amo):
        self.amo = amo

    def get_registered_star(self, name):
        return types.SimpleNamespace(star_cls=self.amo)


def _plugin(ctx):
    obj = object.__new__(data_main.AmoDataPlugin)
    obj.context = ctx
    return obj


class ForgetInAmoTests(unittest.TestCase):
    def test_deletes_local_profile(self):
        amo = _FakeAmo()
        plugin = _plugin(_Ctx(amo))
        self.assertTrue(plugin._forget_in_amo("u1"))
        self.assertEqual(amo.db.deleted, ["u1"])

    def test_missing_plugin_is_safe(self):
        plugin = _plugin(_Ctx(None))
        self.assertFalse(plugin._forget_in_amo("u1"))

    def test_missing_db_is_safe(self):
        amo = types.SimpleNamespace()
        plugin = _plugin(_Ctx(amo))
        self.assertFalse(plugin._forget_in_amo("u1"))

    def test_broken_context_is_safe(self):
        plugin = _plugin(object())
        self.assertFalse(plugin._forget_in_amo("u1"))


class HandlerWiringTests(unittest.TestCase):
    def test_delete_handler_calls_forget(self):
        src = open(os.path.join(_ROOT, "main.py"), encoding="utf-8").read()
        body = src[src.index("async def api_profile_delete"):src.index("async def api_persons")]
        self.assertIn("_forget_in_amo(uid)", body)


if __name__ == "__main__":
    unittest.main()
