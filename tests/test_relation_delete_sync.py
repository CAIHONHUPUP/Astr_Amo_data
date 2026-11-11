# -*- coding: utf-8 -*-
"""删除关系：二次确认 + 两侧一起删（想记 ⇄ 陪伴）。

2.6.1 加了「点节点→弹窗→删关系」，但这段同步逻辑当时没有任何测试
（覆盖率报告里 `_amo_plugin` 与同步块整段是 Missing）。这里把它补齐：
确认闸、找到/找不到陪伴插件、同步失败三种结果都要能说清楚。

另外加一条面板源码守卫：删除按钮必须带 confirm（两边一起删，误点找不回来）。
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import astrbot_stub  # noqa: E402

astrbot_stub.install()

from test_api_coverage2 import BOT, FakeContext, PluginApiMixin  # noqa: E402

A_UID = "302578963"
B_UID = "470488586"


class FakeAmoDB:
    def __init__(self, boom=False):
        self.calls = []
        self.boom = boom

    def delete_relation(self, a_uid, b_uid):
        if self.boom:
            raise RuntimeError("陪伴库挂了")
        self.calls.append((a_uid, b_uid))
        return True


class FakeAmo:
    """陪伴插件实例：只需要一个 db.delete_relation。"""

    def __init__(self, boom=False):
        self.db = FakeAmoDB(boom=boom)


class Meta:
    def __init__(self, star=None, name="Astrbot_Amo_forever", module_path=""):
        self.star_cls = star
        self.name = name
        self.module_path = module_path


class AmoContext(FakeContext):
    """能返回陪伴插件 metadata 的 context。"""

    def __init__(self, star=None, stars=None, raise_first=False):
        super().__init__()
        self.star = star
        self.stars = list(stars or [])
        self.raise_first = raise_first

    def get_registered_star(self, name):
        if self.raise_first:
            raise RuntimeError("注册表不可用")
        if self.star is None:
            return None
        return Meta(self.star)

    def get_all_stars(self):
        return list(self.stars)


class RelationDeleteTests(PluginApiMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.plugin.store.save_profile(BOT, A_UID, name="阿明")
        self.plugin.store.save_profile(BOT, B_UID, name="小美")
        self.plugin.store.save_relation(BOT, A_UID, B_UID, "相恋", "主人 × 她")

    def relations(self):
        return self.plugin.store.list_relations(BOT)

    def use_context(self, context):
        self.plugin.context = context

    # ---------- 确认闸 ----------
    def test_delete_without_confirm_is_rejected(self) -> None:
        result = self.run_api(self.plugin.api_relation_delete,
                              {"a_uid": A_UID, "b_uid": B_UID})
        self.assertEqual(result["code"], 1)
        self.assertIn("confirm", result["msg"])
        self.assertEqual(len(self.relations()), 1)  # 一条都没删

    def test_confirm_variants_accepted(self) -> None:
        for value in ("1", "true", "yes"):
            with self.subTest(confirm=value):
                self.plugin.store.save_relation(BOT, A_UID, B_UID, "相恋")
                result = self.run_api(self.plugin.api_relation_delete,
                                      {"a_uid": A_UID, "b_uid": B_UID, "confirm": value})
                self.assertEqual(result["code"], 0)

    def test_missing_uid_still_rejected_before_confirm(self) -> None:
        result = self.run_api(self.plugin.api_relation_delete, {"a_uid": A_UID, "confirm": 1})
        self.assertEqual(result["code"], 1)
        self.assertIn("两个 uid", result["msg"])

    # ---------- 两侧一起删 ----------
    def test_deletes_both_sides(self) -> None:
        amo = FakeAmo()
        self.use_context(AmoContext(star=amo))
        result = self.run_api(self.plugin.api_relation_delete,
                              {"a_uid": A_UID, "b_uid": B_UID, "confirm": 1})
        self.assertEqual(result["code"], 0)
        self.assertEqual(self.relations(), [])
        self.assertEqual(amo.db.calls, [(A_UID, B_UID)])
        self.assertTrue(result["data"]["synced"])
        self.assertEqual(result["data"]["warning"], "")

    def test_sync_failure_keeps_local_delete_and_warns(self) -> None:
        self.use_context(AmoContext(star=FakeAmo(boom=True)))
        result = self.run_api(self.plugin.api_relation_delete,
                              {"a_uid": A_UID, "b_uid": B_UID, "confirm": 1})
        self.assertEqual(result["code"], 0)
        self.assertEqual(self.relations(), [])  # 本侧照删
        self.assertFalse(result["data"]["synced"])
        self.assertIn("删除失败", result["data"]["warning"])

    def test_without_amo_plugin_warns(self) -> None:
        self.use_context(AmoContext(star=None))
        result = self.run_api(self.plugin.api_relation_delete,
                              {"a_uid": A_UID, "b_uid": B_UID, "confirm": 1})
        self.assertEqual(result["code"], 0)
        self.assertFalse(result["data"]["synced"])
        self.assertIn("没找到", result["data"]["warning"])

    def test_missing_relation_still_reports(self) -> None:
        self.use_context(AmoContext(star=FakeAmo()))
        result = self.run_api(self.plugin.api_relation_delete,
                              {"a_uid": "x", "b_uid": "y", "confirm": 1})
        self.assertEqual(result["code"], 1)
        self.assertEqual(result["msg"], "没有这条关系")


class AmoPluginLookupTests(unittest.TestCase):
    """`_amo_plugin()` 的几种返回：注册表命中 / 扫描命中 / 未激活 / 注册表挂了。"""

    def make(self, context):
        plugin = type("P", (), {"context": context})()
        from main import AmoDataPlugin

        return AmoDataPlugin._amo_plugin(plugin)

    def test_hit_by_registered_star(self) -> None:
        amo = FakeAmo()
        self.assertIs(self.make(AmoContext(star=amo)), amo)

    def test_falls_back_to_star_scan(self) -> None:
        amo = FakeAmo()
        context = AmoContext(
            star=None, raise_first=True,
            stars=[Meta(None, name="别人"), Meta(amo, module_path="data.plugins.Astrbot_Amo_forever.main")],
        )
        self.assertIs(self.make(context), amo)

    def test_inactive_plugin_is_not_reported(self) -> None:
        # AstrBot 里未激活时 star_cls 是 None，别乱猜别的属性
        self.assertIsNone(self.make(AmoContext(star=None, stars=[Meta(None)])))

    def test_registry_without_methods(self) -> None:
        self.assertIsNone(self.make(object()))


class PanelConfirmGuardTests(unittest.TestCase):
    """面板删除必须走 confirm：想记 + 陪伴一起删，没有撤销。"""

    @classmethod
    def setUpClass(cls) -> None:
        html = open(os.path.join(ROOT, "views", "dashboard", "index.html"),
                    encoding="utf-8").read()
        cls.html = html

    def test_delete_asks_for_confirmation(self) -> None:
        self.assertIn("window.confirm(", self.html)
        self.assertIn("confirm: 1", self.html)

    def test_no_dead_conditional_left(self) -> None:
        # 以前写成 (data.synced ? "已删除" : "已删除")，两个分支一样
        self.assertNotIn('data.synced ? "已删除" : "已删除"', self.html)

    def test_large_graph_has_pair_budget(self) -> None:
        # 全对斥力是 O(n²) 且跑在主线程，必须有抽样上限
        self.assertIn("SETTLE_PAIR_BUDGET", self.html)
        self.assertIn("Math.ceil(pairs / SETTLE_PAIR_BUDGET)", self.html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
