# -*- coding: utf-8 -*-
"""main.py 覆盖率补齐（第二批）：MemoryAPI 的失败兜底 + 插件 Web 接口。

第一批 ``test_memory_api.py`` 只跑通了门面的正常路径；这里补两块：

1. ``MemoryAPI`` 每个方法在 store 整体抛异常时都要返回**安全默认值**
   （聊天主链路绝不能被数据层拖崩）；
2. ``AmoDataPlugin`` 的 31 个 ``api_*`` 处理器、CSRF 守卫、请求体读取、
   bot_id 归一、每日备份/每周整理与维护循环。

全部用真实临时库，断言返回 dict 的关键字段和落库结果，不联网、不 sleep。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import astrbot_stub  # noqa: E402

astrbot_stub.install()

import main as main_module  # noqa: E402
from main import AmoDataPlugin, MemoryAPI  # noqa: E402
from store import DataStore  # noqa: E402

BOT = "test-bot"
UID = "302578963"


class BoomStore:
    """任何方法都炸的假 store：验证门面的 except 分支。"""

    path = ""

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise RuntimeError("store 挂了")
        return fail


class MemoryApiErrorTests(unittest.TestCase):
    """MemoryAPI 的失败兜底：每个方法都必须吞掉异常并返回安全默认值。"""

    def setUp(self) -> None:
        self.api = MemoryAPI(BoomStore())

    def test_all_methods_swallow_store_errors(self) -> None:
        cases = [
            ("get_profile", lambda: self.api.get_profile("b", "u"), {}),
            ("save_profile", lambda: self.api.save_profile("b", "u", name="x"), {}),
            ("list_profiles", lambda: self.api.list_profiles("b"), []),
            ("delete_profile", lambda: self.api.delete_profile("b", "u"), {}),
            ("remember", lambda: self.api.remember("b", "u", "x"), None),
            ("recall", lambda: self.api.recall("b", "u", "x"), []),
            ("list_memories", lambda: self.api.list_memories("b", "u"), []),
            ("delete_memory", lambda: self.api.delete_memory("b", 1), False),
            ("list_relations", lambda: self.api.list_relations("b", "u"), []),
            ("save_relation", lambda: self.api.save_relation("b", "a", "c", "朋友"), False),
            ("delete_relation", lambda: self.api.delete_relation("b", "a", "c"), False),
            ("cache_get", lambda: self.api.cache_get("b", "k"), None),
            ("cache_set", lambda: self.api.cache_set("b", "k", 1), False),
            ("cache_delete", lambda: self.api.cache_delete("b", "k"), False),
            ("add_message", lambda: self.api.add_message("b", "umo", "u", "阿明"), 0),
            ("list_messages", lambda: self.api.list_messages("b", "umo"), []),
            ("export_snapshot", lambda: self.api.export_snapshot("b"), {}),
            ("import_snapshot", lambda: self.api.import_snapshot({"x": 1}), {"error": 1}),
            ("backup_now", lambda: self.api.backup_now(), ""),
            ("list_backups", lambda: self.api.list_backups(), []),
            ("restore_backup", lambda: self.api.restore_backup("x.db"), {"error": 1}),
            ("messages_since", lambda: self.api.messages_since("b", "u"), []),
            ("messages_between", lambda: self.api.messages_between("b", 0, 1), []),
            ("recall_messages", lambda: self.api.recall_messages("b", "昨天"), None),
            ("compress_memories", lambda: self.api.compress_memories("b"), None),
            ("archive_stale", lambda: self.api.archive_stale("b"), None),
            ("delete_message", lambda: self.api.delete_message("b", 1), False),
            ("add_log", lambda: self.api.add_log("b", "info", "t", "c"), 0),
            ("list_logs", lambda: self.api.list_logs("b"), []),
            ("maintenance", lambda: self.api.maintenance("b"), {}),
            ("purge_logs", lambda: self.api.purge_logs("b"), 0),
            ("stats", lambda: self.api.stats("b"), {}),
            ("resolve_person", lambda: self.api.resolve_person("b", "qq", "u"), 0),
            ("person_context", lambda: self.api.person_context("b", "qq", "u"), {}),
            ("list_persons", lambda: self.api.list_persons("b"), []),
            ("person_detail", lambda: self.api.person_detail("b", 1), {}),
            ("person_link", lambda: self.api.person_link("b", "qq", "u"), 0),
            ("person_unlink", lambda: self.api.person_unlink("b", "qq", "u"), False),
            ("person_merge", lambda: self.api.person_merge("b", 1, 2), False),
        ]
        for name, factory, expected in cases:
            with self.subTest(method=name):
                result = asyncio.run(factory())
                if name == "remember":
                    self.assertFalse(result.get("ok", True))
                    self.assertIn("store 挂了", result.get("reason", ""))
                elif name == "recall_messages":
                    self.assertEqual(result, {"messages": [], "span": None,
                                              "path": {}, "label": ""})
                elif name == "compress_memories":
                    self.assertEqual(result, {"merged_summaries": 0,
                                              "merged_near_duplicates": 0,
                                              "applied": True})
                elif name == "archive_stale":
                    self.assertEqual(result, {"candidate": 0, "archived": 0, "ids": []})
                else:
                    self.assertEqual(result, expected)
                    self.assertIs(type(result), type(expected))


class MemoryApiExtraTests(unittest.TestCase):
    """门面里第一批没覆盖到的正常分支（人物/增量/时间段/开关）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="amo-data-api2-extra-")
        self.store = DataStore(
            os.path.join(self.tmp, "amo_data.db"),
            {"embedding_enabled": False, "embedding_backfill": False},
        )
        self.api = MemoryAPI(self.store)

    def tearDown(self) -> None:
        try:
            self.store.close()
        except Exception:
            pass

    def test_person_link_context_unlink(self) -> None:
        pid = asyncio.run(self.api.resolve_person(BOT, "qq", UID, "阿明"))
        self.assertGreater(pid, 0)
        asyncio.run(self.api.add_message(BOT, "qq:1", UID, "阿明", "user", "QQ 上说的话"))
        asyncio.run(self.api.add_message(BOT, "lark:1", UID, "阿明", "user", "飞书上说的话"))
        linked = asyncio.run(self.api.person_link(BOT, "lark", UID, pid, "阿明"))
        self.assertEqual(linked, pid)
        context = asyncio.run(self.api.person_context(BOT, "qq", UID))
        self.assertEqual(context["pid"], pid)
        self.assertTrue(any("飞书上说的话" in item["content"]
                            for item in context["recent"]))
        self.assertEqual(len(asyncio.run(self.api.list_persons(BOT, "阿明"))), 1)
        self.assertTrue(asyncio.run(self.api.person_unlink(BOT, "lark", UID)))

    def test_messages_since_with_umo_and_between_uid(self) -> None:
        first = asyncio.run(self.api.add_message(BOT, "qq:1", UID, "阿明", "user", "一"))
        asyncio.run(self.api.add_message(BOT, "qq:2", "other", "别人", "user", "二"))
        since = asyncio.run(self.api.messages_since(BOT, "", 0, 10, umo="qq:1"))
        self.assertEqual([row["id"] for row in since], [first])
        between = asyncio.run(self.api.messages_between(BOT, 0, 10 ** 11, uid=UID))
        self.assertEqual(len(between), 1)
        self.assertEqual(between[0]["content"], "一")

    def test_compress_and_archive_preview(self) -> None:
        asyncio.run(self.api.remember(BOT, UID, "2026-11-01 聊了很多",
                                      kind="conversation_summary"))
        compressed = asyncio.run(self.api.compress_memories(BOT, apply=False))
        self.assertFalse(compressed["applied"])
        archived = asyncio.run(self.api.archive_stale(BOT, days=0, apply=False))
        self.assertEqual(archived["archived"], 0)
        self.assertIn("candidate", archived)

    def test_delete_message_and_restore_missing_backup(self) -> None:
        mid = asyncio.run(self.api.add_message(BOT, "qq:1", UID, "阿明", "user", "删我"))
        self.assertTrue(asyncio.run(self.api.delete_message(BOT, mid)))
        self.assertEqual(asyncio.run(self.api.list_messages(BOT, "qq:1")), [])
        self.assertEqual(asyncio.run(self.api.restore_backup("nope.db")), {"error": 1})


class FakeRequest:
    """最小 quart/astrbot request 桩。"""

    def __init__(self, payload=None, method="POST", headers=None,
                 json_error=False, get_json_error=False):
        self.payload = {} if payload is None else payload
        self.method = method
        self.headers = {} if headers is None else headers
        self.json_error = json_error
        self.get_json_error = get_json_error

    async def json(self, default=None):
        if self.json_error:
            raise RuntimeError("没有 json")
        return self.payload

    async def get_json(self, silent=True):
        if self.get_json_error:
            raise RuntimeError("没有 get_json")
        return self.payload


class NoJsonRequest:
    """只有 get_json、没有 json 的旧版请求对象。"""

    def __init__(self, payload):
        self.payload = payload

    async def get_json(self, silent=True):
        return self.payload


class HeaderBoom:
    def get(self, key):
        raise RuntimeError("header 挂了")


class FakeContext:
    def __init__(self, registry=None, raise_on_register=False):
        self.registered = []
        self.registered_web_apis = list(registry or [])
        self.raise_on_register = raise_on_register

    def register_web_api(self, route, handler, methods, desc):
        if self.raise_on_register:
            raise RuntimeError("注册失败")
        self.registered.append((route, methods, desc))
        self.registered_web_apis.append((route, methods, handler))

    def get_config(self, umo=None):
        return {}


class PluginApiCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="amo-data-api2-plugin-")
        self._saved = {}
        for name in ("astrbot.core.utils", "astrbot.core.utils.astrbot_path"):
            self._saved[name] = sys.modules.get(name)
        utils = types.ModuleType("astrbot.core.utils")
        utils.__path__ = []
        path_module = types.ModuleType("astrbot.core.utils.astrbot_path")
        path_module.get_astrbot_data_path = lambda: self.tmp
        sys.modules["astrbot.core.utils"] = utils
        sys.modules["astrbot.core.utils.astrbot_path"] = path_module
        self.context = FakeContext()
        self._old_loop = AmoDataPlugin._maintenance_loop
        AmoDataPlugin._maintenance_loop = lambda self: None
        self._saved_web_request = main_module.web_request
        main_module.web_request = None
        self.plugin = self.make_plugin()

    def tearDown(self) -> None:
        try:
            self.plugin.store.close()
        except Exception:
            pass
        AmoDataPlugin._maintenance_loop = self._old_loop
        main_module.web_request = self._saved_web_request
        for name, module in self._saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def make_plugin(self, config=None):
        return AmoDataPlugin(self.context, config or {
            "bot_id": BOT, "timezone": "Asia/Shanghai",
            "chat_days": "10", "chat_compress_after": "2", "log_days": "3",
            "backup_keep": "2",
        })

    def run_api(self, handler, payload=None, method="POST", headers=None):
        main_module.web_request = None if payload is None else FakeRequest(
            payload, method=method, headers=headers
        )
        try:
            return asyncio.run(handler())
        finally:
            main_module.web_request = self._saved_web_request

    def run_coro(self, coro):
        return asyncio.run(coro)

    # ---------- 状态 / 画像 ----------
    def test_status_and_profiles(self) -> None:
        self.plugin.store.save_profile(BOT, UID, name="阿明", alias="明明")
        self.plugin.store.remember(BOT, UID, "他喜欢喝美式咖啡")

        status = self.run_api(self.plugin.api_status, {})
        self.assertEqual(status["code"], 0)
        data = status["data"]
        self.assertEqual(data["version"], main_module.PLUGIN_VERSION)
        self.assertEqual(data["bot_id"], BOT)
        self.assertEqual(data["db"], self.plugin.db_path)
        self.assertEqual(data["counts"]["profiles"], 1)
        self.assertEqual(data["counts"]["memories"], 1)
        self.assertEqual(data["bots"], [BOT])
        self.assertEqual(data["recall"]["pool_size"],
                         self.plugin.store.setting("pool_size"))
        self.assertIn("enabled", data["embedding"])

        listed = self.run_api(self.plugin.api_profiles, {"q": "明明"})
        self.assertEqual(listed["code"], 0)
        self.assertEqual(len(listed["data"]["items"]), 1)
        self.assertEqual(listed["data"]["items"][0]["name"], "阿明")

        missing = self.run_api(self.plugin.api_profile, {})
        self.assertEqual(missing["code"], 1)
        self.assertIn("uid", missing["msg"])

        detail = self.run_api(self.plugin.api_profile, {"uid": UID})
        self.assertEqual(detail["code"], 0)
        self.assertEqual(detail["data"]["bot_id"], BOT)
        self.assertTrue(detail["data"]["profile"]["exists"])
        self.assertEqual(len(detail["data"]["memories"]), 1)

        # 面板不传 bot_id 且配置也没有默认 bot：要跨 bot 找到画像所属的库
        self.plugin.config["bot_id"] = ""
        found = self.run_api(self.plugin.api_profile, {"uid": UID})
        self.assertEqual(found["data"]["bot_id"], BOT)
        self.assertTrue(found["data"]["profile"]["exists"])

    def test_profile_save_and_delete(self) -> None:
        missing = self.run_api(self.plugin.api_profile_save, {})
        self.assertEqual(missing["code"], 1)
        self.assertIn("uid", missing["msg"])

        saved = self.run_api(self.plugin.api_profile_save,
                             {"uid": "u2", "name": "小美", "platform": "qq"})
        self.assertEqual(saved["code"], 0)
        self.assertEqual(saved["msg"], "已保存")
        self.assertEqual(saved["data"]["name"], "小美")
        self.assertEqual(self.plugin.store.get_profile(BOT, "u2")["name"], "小美")
        persons = self.plugin.store.list_persons(BOT)
        self.assertEqual(len(persons), 1)
        self.assertEqual(persons[0]["links"][0]["platform"], "qq")
        self.assertEqual(persons[0]["links"][0]["uid"], "u2")

        # 配置里没有默认 bot、payload 也不传：应改回画像原来所属的 bot
        self.plugin.config["bot_id"] = ""
        patched = self.run_api(self.plugin.api_profile_save,
                               {"uid": "u2", "alias": "美美"})
        self.assertEqual(patched["code"], 0)
        profile = self.plugin.store.get_profile(BOT, "u2")
        self.assertEqual(profile["name"], "小美")
        self.assertEqual(profile["alias"], "美美")
        self.assertEqual(self.plugin.store.get_profile("default", "u2")["exists"], False)
        self.plugin.config["bot_id"] = BOT

        self.plugin.store.save_profile(BOT, UID, name="阿明")
        no_uid = self.run_api(self.plugin.api_profile_delete, {})
        self.assertEqual(no_uid["code"], 1)
        no_confirm = self.run_api(self.plugin.api_profile_delete, {"uid": UID})
        self.assertEqual(no_confirm["code"], 1)
        self.assertIn("confirm", no_confirm["msg"])
        deleted = self.run_api(self.plugin.api_profile_delete,
                               {"uid": UID, "confirm": "1"})
        self.assertEqual(deleted["code"], 0)
        self.assertEqual(deleted["data"]["profiles"], 1)
        self.assertFalse(self.plugin.store.get_profile(BOT, UID)["exists"])

    # ---------- 人物 ----------
    def test_person_endpoints(self) -> None:
        a = self.plugin.store.resolve_person(BOT, "qq", "a", "阿明")
        b = self.plugin.store.resolve_person(BOT, "lark", "b", "明明")
        self.assertGreater(a, 0)
        self.assertGreater(b, 0)

        persons = self.run_api(self.plugin.api_persons, {})
        self.assertEqual(persons["code"], 0)
        self.assertEqual(len(persons["data"]["items"]), 2)

        by_pid = self.run_api(self.plugin.api_person, {"pid": a})
        self.assertEqual(by_pid["code"], 0)
        self.assertEqual(by_pid["data"]["pid"], a)
        self.assertEqual(by_pid["data"]["name"], "阿明")

        by_identity = self.run_api(self.plugin.api_person,
                                   {"platform": "qq", "uid": "a"})
        self.assertEqual(by_identity["code"], 0)
        self.assertEqual(by_identity["data"]["pid"], a)

        not_found = self.run_api(self.plugin.api_person, {"pid": 99999})
        self.assertEqual(not_found["code"], 1)
        self.assertIn("没找到", not_found["msg"])
        empty = self.run_api(self.plugin.api_person, {})
        self.assertEqual(empty["code"], 1)

        link_missing = self.run_api(self.plugin.api_person_link,
                                    {"platform": "lark", "name": "小 c"})
        self.assertEqual(link_missing["code"], 1)
        linked = self.run_api(self.plugin.api_person_link,
                              {"platform": "lark", "uid": "c", "name": "小 c"})
        self.assertEqual(linked["code"], 0)
        self.assertEqual(linked["msg"], "已绑定")
        self.assertGreater(linked["data"]["pid"], 0)
        self.assertTrue(self.plugin.store.person_of(BOT, "lark", "c"))

        unlink_missing = self.run_api(self.plugin.api_person_unlink,
                                      {"platform": "lark"})
        self.assertEqual(unlink_missing["code"], 1)
        unlinked = self.run_api(self.plugin.api_person_unlink,
                                {"platform": "lark", "uid": "c"})
        self.assertEqual(unlinked["code"], 0)
        self.assertTrue(unlinked["data"]["ok"])
        unlinked_again = self.run_api(self.plugin.api_person_unlink,
                                      {"platform": "lark", "uid": "从来没有绑过"})
        self.assertEqual(unlinked_again["code"], 1)
        self.assertFalse(unlinked_again["data"]["ok"])

        bad_merge = self.run_api(self.plugin.api_person_merge,
                                 {"keep_pid": a, "drop_pid": a})
        self.assertEqual(bad_merge["code"], 1)
        missing_merge = self.run_api(self.plugin.api_person_merge,
                                     {"keep_pid": a, "drop_pid": 0})
        self.assertEqual(missing_merge["code"], 1)
        merged = self.run_api(self.plugin.api_person_merge,
                              {"keep_pid": a, "drop_pid": b})
        self.assertEqual(merged["code"], 0)
        self.assertTrue(merged["data"]["ok"])
        pids = {item["pid"] for item in self.plugin.store.list_persons(BOT)}
        self.assertIn(a, pids)
        self.assertNotIn(b, pids)
        self.assertEqual(len(self.plugin.store.person_detail(BOT, a)["links"]), 2)
        failed = self.run_api(self.plugin.api_person_merge,
                              {"keep_pid": a, "drop_pid": 99999})
        self.assertEqual(failed["code"], 1)
        self.assertEqual(failed["msg"], "合并失败")

    # ---------- 记忆 ----------
    def test_memory_endpoints(self) -> None:
        self.plugin.store.remember(BOT, UID, "他养了一只叫团子的猫")

        all_items = self.run_api(self.plugin.api_memories, {})
        self.assertEqual(all_items["code"], 0)
        self.assertEqual(len(all_items["data"]["items"]), 1)

        queried = self.run_api(self.plugin.api_memories, {"q": "团子", "uid": UID})
        self.assertEqual(len(queried["data"]["items"]), 1)
        self.assertIn("团子", queried["data"]["items"][0]["content"])
        no_uid = self.run_api(self.plugin.api_memories, {"q": "团子"})
        self.assertEqual(no_uid["data"]["items"], [])

        missing = self.run_api(self.plugin.api_memory_save,
                               {"uid": UID, "content": "  "})
        self.assertEqual(missing["code"], 1)
        self.assertIn("不能为空", missing["msg"])
        saved = self.run_api(self.plugin.api_memory_save,
                             {"uid": UID, "content": "他喜欢喝冰美式",
                              "importance": 0.9})
        self.assertEqual(saved["code"], 0)
        self.assertTrue(saved["data"]["ok"])
        self.assertEqual(saved["msg"], "已记住")
        self.assertEqual(len(self.plugin.store.list_memories(BOT, UID, 10)), 2)

        bad_id = self.run_api(self.plugin.api_memory_delete, {"id": "不是数字"})
        self.assertEqual(bad_id["code"], 1)
        self.assertIn("id", bad_id["msg"])
        memory = self.plugin.store.list_memories(BOT, UID, 10)[0]
        deleted = self.run_api(self.plugin.api_memory_delete, {"id": memory["id"]})
        self.assertEqual(deleted["code"], 0)
        self.assertTrue(deleted["data"]["deleted"])
        again = self.run_api(self.plugin.api_memory_delete, {"id": memory["id"]})
        self.assertFalse(again["data"]["deleted"])

    # ---------- 备份 / 快照 ----------
    def test_backup_endpoints(self) -> None:
        self.plugin.store.save_profile(BOT, UID, name="阿明")

        made = self.run_api(self.plugin.api_backup_now, {})
        self.assertEqual(made["code"], 0)
        path = made["data"]["path"]
        self.assertTrue(os.path.exists(path))
        name = made["data"]["name"]
        self.assertEqual(os.path.basename(path), name)
        self.assertTrue(any(item["name"] == name for item in made["data"]["items"]))

        listed = self.run_api(self.plugin.api_backup_list, {})
        self.assertEqual(listed["code"], 0)
        self.assertTrue(any(item["name"] == name for item in listed["data"]["items"]))

        exported = self.run_api(self.plugin.api_backup_export, {})
        self.assertEqual(exported["code"], 0)
        snapshot = exported["data"]["snapshot"]
        self.assertGreaterEqual(snapshot["counts"]["profiles"], 1)

        missing = self.run_api(self.plugin.api_backup_import, {})
        self.assertEqual(missing["code"], 1)
        self.assertIn("snapshot", missing["msg"])
        imported = self.run_api(self.plugin.api_backup_import, {"snapshot": {
            "bot_id": BOT,
            "profiles": [{"bot_id": BOT, "uid": "n9", "name": "导入的人",
                          "updated_at": 10 ** 10}],
            "memories": [{"bot_id": BOT, "uid": "n9", "content": "导入的记忆"}],
        }})
        self.assertEqual(imported["code"], 0)
        self.assertEqual(imported["data"]["profiles"], 1)
        self.assertEqual(imported["data"]["memories"], 1)
        self.assertEqual(self.plugin.store.get_profile(BOT, "n9")["name"], "导入的人")

        no_name = self.run_api(self.plugin.api_backup_restore, {})
        self.assertEqual(no_name["code"], 1)
        self.assertIn("备份文件名", no_name["msg"])
        restored = self.run_api(self.plugin.api_backup_restore, {"name": name})
        self.assertEqual(restored["code"], 0)
        self.assertNotIn("error", restored["data"])
        missing_file = self.run_api(self.plugin.api_backup_restore,
                                    {"name": "a_file_that_does_not_exist.db"})
        self.assertEqual(missing_file["code"], 0)
        self.assertEqual(missing_file["data"], {"error": 1})

        async def empty_backup(keep=7):
            return ""

        original = self.plugin.mem.backup_now
        self.plugin.mem.backup_now = empty_backup
        try:
            failed = self.run_api(self.plugin.api_backup_now, {})
        finally:
            self.plugin.mem.backup_now = original
        self.assertEqual(failed["code"], 1)
        self.assertEqual(failed["msg"], "备份失败")

    # ---------- 关系 ----------
    def test_relation_endpoints(self) -> None:
        self.plugin.store.save_profile(BOT, "a", name="阿明")
        self.plugin.store.save_profile(BOT, "b", name="小美")

        missing = self.run_api(self.plugin.api_relation_save,
                               {"a_uid": "a"})
        self.assertEqual(missing["code"], 1)
        self.assertIn("两个 uid", missing["msg"])
        saved = self.run_api(self.plugin.api_relation_save,
                             {"a_uid": "a", "b_uid": "b",
                              "relation": "相恋", "note": "在一起两年"})
        self.assertEqual(saved["code"], 0)
        self.assertEqual(saved["msg"], "已保存")

        listed = self.run_api(self.plugin.api_relations, {})
        self.assertEqual(listed["code"], 0)
        self.assertEqual(len(listed["data"]["items"]), 1)
        item = listed["data"]["items"][0]
        self.assertEqual(item["relation"], "相恋")
        self.assertEqual(item["a_name"], "阿明")
        self.assertEqual(item["b_name"], "小美")

        # 画像查询失败时接口仍要能给出关系（名字退回人物卡算出来的值）
        original = self.plugin.store.list_profiles

        def broken(*args, **kwargs):
            raise RuntimeError("画像表坏了")

        self.plugin.store.list_profiles = broken
        try:
            fallback = self.run_api(self.plugin.api_relations, {})
        finally:
            self.plugin.store.list_profiles = original
        self.assertEqual(fallback["code"], 0)
        self.assertEqual(len(fallback["data"]["items"]), 1)

        delete_missing = self.run_api(self.plugin.api_relation_delete,
                                      {"a_uid": "a"})
        self.assertEqual(delete_missing["code"], 1)
        deleted = self.run_api(self.plugin.api_relation_delete,
                               {"a_uid": "a", "b_uid": "b"})
        self.assertEqual(deleted["code"], 0)
        self.assertEqual(deleted["msg"], "已删除")
        deleted_again = self.run_api(self.plugin.api_relation_delete,
                                     {"a_uid": "a", "b_uid": "b"})
        self.assertEqual(deleted_again["code"], 1)
        self.assertEqual(deleted_again["msg"], "没有这条关系")

    # ---------- 聊天 ----------
    def test_message_endpoints(self) -> None:
        self.plugin.store.add_message(BOT, "qq:1", UID, "阿明", "user", "在吗")
        self.plugin.store.add_message(BOT, "qq:1", "bot", "她", "bot", "在呀")

        items = self.run_api(self.plugin.api_messages, {"limit": 10})
        self.assertEqual(items["code"], 0)
        self.assertEqual(len(items["data"]["items"]), 2)

        bad_pid = self.run_api(self.plugin.api_messages, {"limit": 10, "pid": "x"})
        self.assertEqual(len(bad_pid["data"]["items"]), 2)

        queried = self.run_api(self.plugin.api_messages, {"query": "今天", "limit": 10})
        self.assertEqual(queried["code"], 0)
        self.assertIsNotNone(queried["data"]["span"])
        self.assertIn("path", queried["data"])
        self.assertTrue(queried["data"]["label"])

        bad_id = self.run_api(self.plugin.api_message_delete, {"id": "abc"})
        self.assertEqual(bad_id["code"], 1)
        message_id = items["data"]["items"][0]["id"]
        deleted = self.run_api(self.plugin.api_message_delete, {"id": message_id})
        self.assertEqual(deleted["code"], 0)
        self.assertTrue(deleted["data"]["deleted"])

        no_confirm = self.run_api(self.plugin.api_message_clear, {})
        self.assertEqual(no_confirm["code"], 1)
        self.assertIn("confirm", no_confirm["msg"])
        cleared = self.run_api(self.plugin.api_message_clear, {"confirm": "yes"})
        self.assertEqual(cleared["code"], 0)
        self.assertEqual(cleared["data"]["deleted"], 1)

    # ---------- 日志 ----------
    def test_log_endpoints(self) -> None:
        log_id = self.plugin.store.add_log(BOT, "info", "tag", "一条日志")
        self.assertGreater(log_id, 0)
        self.plugin.store.add_log(BOT, "warn", "tag", "两条日志")

        listed = self.run_api(self.plugin.api_logs, {"level": "info", "limit": 10})
        self.assertEqual(listed["code"], 0)
        self.assertEqual(len(listed["data"]["items"]), 1)
        self.assertEqual(listed["data"]["items"][0]["content"], "一条日志")

        bad_id = self.run_api(self.plugin.api_log_delete, {"id": "abc"})
        self.assertEqual(bad_id["code"], 1)
        deleted = self.run_api(self.plugin.api_log_delete, {"id": log_id})
        self.assertEqual(deleted["code"], 0)
        self.assertTrue(deleted["data"]["deleted"])

        no_confirm = self.run_api(self.plugin.api_log_clear, {})
        self.assertEqual(no_confirm["code"], 1)
        cleared = self.run_api(self.plugin.api_log_clear, {"confirm": "1"})
        self.assertEqual(cleared["code"], 0)
        self.assertEqual(cleared["data"]["deleted"], 1)

    # ---------- 日程缓存 ----------
    def test_schedule_endpoints(self) -> None:
        self.plugin.store.cache_set(BOT, "schedule:2026-11-10", {"items": [1, 2]})
        self.plugin.store.cache_set(BOT, "worldview", {"city": "东莞"})

        listed = self.run_api(self.plugin.api_schedule, {})
        self.assertEqual(listed["code"], 0)
        keys = {item["key"]: item["size"] for item in listed["data"]["items"]}
        self.assertEqual(set(keys), {"schedule:2026-11-10", "worldview"})
        self.assertTrue(all(size > 0 for size in keys.values()))

        one = self.run_api(self.plugin.api_schedule, {"key": "schedule:2026-11-10"})
        self.assertEqual(one["code"], 0)
        self.assertEqual(one["data"]["value"], {"items": [1, 2]})
        absent = self.run_api(self.plugin.api_schedule, {"key": "没有这个 key"})
        self.assertIsNone(absent["data"]["value"])

        missing = self.run_api(self.plugin.api_schedule_delete, {})
        self.assertEqual(missing["code"], 1)
        self.assertIn("key", missing["msg"])
        deleted = self.run_api(self.plugin.api_schedule_delete, {"key": "worldview"})
        self.assertEqual(deleted["code"], 0)
        self.assertTrue(deleted["data"]["deleted"])
        again = self.run_api(self.plugin.api_schedule_delete, {"key": "worldview"})
        self.assertFalse(again["data"]["deleted"])

    # ---------- 手动维护 ----------
    def test_maintenance_endpoints(self) -> None:
        self.plugin.store.add_message(BOT, "qq:1", UID, "阿明", "user", "旧消息")
        self.plugin.store.add_log(BOT, "info", "tag", "旧日志")
        result = self.run_api(self.plugin.api_maintenance, {})
        self.assertEqual(result["code"], 0)
        self.assertIn("compressed", result["data"])
        self.assertIn("purged", result["data"])
        self.assertIn("logs_purged", result["data"])

        # 维护失败也要把错误写进返回，而不是 500
        original_compress = self.plugin.store.compress_messages
        original_purge = self.plugin.store.purge_logs

        def broken(*args, **kwargs):
            raise RuntimeError("坏了")

        self.plugin.store.compress_messages = broken
        self.plugin.store.purge_logs = broken
        try:
            failed = self.run_api(self.plugin.api_maintenance, {})
        finally:
            self.plugin.store.compress_messages = original_compress
            self.plugin.store.purge_logs = original_purge
        self.assertEqual(failed["code"], 0)
        self.assertIn("compress_error", failed["data"])
        self.assertIn("log_error", failed["data"])

        preview = self.run_api(self.plugin.api_memory_maintain, {"apply": False})
        self.assertEqual(preview["code"], 0)
        self.assertFalse(preview["applied"])
        self.assertIn("compress", preview["data"])
        self.assertIn("archive", preview["data"])
        self.assertFalse(preview["data"]["compress"]["applied"])

        string_off = self.run_api(self.plugin.api_memory_maintain, {"apply": "off"})
        self.assertFalse(string_off["applied"])
        applied = self.run_api(self.plugin.api_memory_maintain, {})
        self.assertTrue(applied["applied"])
        self.assertTrue(applied["data"]["compress"]["applied"])

    # ---------- 每日备份 / 每周整理 ----------
    def test_daily_backup_and_weekly_cleanup(self) -> None:
        path = self.run_coro(self.plugin._maybe_daily_backup(BOT))
        self.assertTrue(path and os.path.exists(path))
        self.assertEqual(self.run_coro(self.plugin._maybe_daily_backup(BOT)), "")

        self.plugin.config["backup_daily"] = False
        self.assertEqual(self.run_coro(self.plugin._maybe_daily_backup(BOT)), "")
        self.plugin.config["backup_daily"] = True

        result = self.run_coro(self.plugin._maybe_weekly_cleanup(BOT))
        self.assertEqual(set(result), {"compress", "archive"})
        self.assertTrue(result["compress"]["applied"])
        self.assertEqual(result["archive"]["archived"], 0)
        self.assertEqual(self.run_coro(self.plugin._maybe_weekly_cleanup(BOT)),
                         {"skipped": True})

    def test_maintenance_loop_runs_one_cycle_then_stops(self) -> None:
        self.plugin.store.add_log(BOT, "info", "tag", "循环里的日志")
        calls = {"n": 0}
        original_sleep = asyncio.sleep

        async def fake_sleep(seconds):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise asyncio.CancelledError()

        asyncio.sleep = fake_sleep
        try:
            self.run_coro(self._old_loop(self.plugin))
        finally:
            asyncio.sleep = original_sleep
        self.assertEqual(calls["n"], 2)

        today = main_module._clock.today()
        daily_key = "backup_day:" + today.strftime("%Y-%m-%d")
        weekly_key = "cleanup_week:" + today.strftime("%G-W%V")
        self.assertEqual(self.plugin.store.cache_get(BOT, daily_key), "1")
        self.assertEqual(self.plugin.store.cache_get(BOT, weekly_key), "1")
        backups = self.plugin.store.list_backups(self.plugin.mem._backup_dir())
        self.assertTrue(backups)
        # 刚写进去的日志还没过期，清理只删 log_days 之前的
        logs = self.plugin.store.list_logs(BOT)
        self.assertEqual([row["content"] for row in logs], ["循环里的日志"])

    # ---------- CSRF 守卫 ----------
    def test_guard_blocks_writes_and_passes_reads(self) -> None:
        async def handler(*args, **kwargs):
            return {"ok": True, "args": args}

        write = self.plugin._guard(handler, is_write=True)
        read = self.plugin._guard(handler, is_write=False)

        main_module.web_request = FakeRequest({}, method="GET", headers={})
        try:
            rejected = self.run_coro(write())
        finally:
            main_module.web_request = None
        self.assertEqual(rejected["code"], 403)
        self.assertIn("POST", rejected["msg"])

        main_module.web_request = FakeRequest(
            {}, method="POST", headers={"sec-fetch-site": "cross-site"})
        try:
            cross = self.run_coro(write())
        finally:
            main_module.web_request = None
        self.assertEqual(cross["code"], 403)
        self.assertIn("跨站", cross["msg"])

        main_module.web_request = FakeRequest(
            {}, method="POST",
            headers={"origin": "https://evil.example", "host": "panel.local:6185"})
        try:
            mismatch = self.run_coro(write())
        finally:
            main_module.web_request = None
        self.assertEqual(mismatch["code"], 403)
        self.assertIn("来源不一致", mismatch["msg"])

        # 同源但 Host 被反代改写：放行
        main_module.web_request = FakeRequest(
            {}, method="POST",
            headers={"origin": "http://127.0.0.1:6185", "host": "panel.local:6185",
                     "sec-fetch-site": "same-origin"})
        try:
            allowed = self.run_coro(write())
        finally:
            main_module.web_request = None
        self.assertTrue(allowed["ok"])

        # X-Forwarded-Host 是可信来源：放行
        main_module.web_request = FakeRequest(
            {}, method="POST",
            headers={"origin": "https://proxy.example", "host": "internal:8000",
                     "x-forwarded-host": "proxy.example:443"})
        try:
            forwarded = self.run_coro(write())
        finally:
            main_module.web_request = None
        self.assertTrue(forwarded["ok"])

        # 非写接口不看 method；没有 request 也能跑
        main_module.web_request = FakeRequest({}, method="GET", headers={})
        try:
            read_ok = self.run_coro(read())
        finally:
            main_module.web_request = None
        self.assertTrue(read_ok["ok"])
        main_module.web_request = None
        self.assertTrue(self.run_coro(read())["ok"])

        # header 读取抛异常时放行，别把面板整个打死
        main_module.web_request = FakeRequest({}, method="POST", headers=HeaderBoom())
        try:
            still_ok = self.run_coro(write())
        finally:
            main_module.web_request = None
        self.assertTrue(still_ok["ok"])

        async def exploding(*args, **kwargs):
            raise RuntimeError("处理器炸了")

        guarded_boom = self.plugin._guard(exploding)
        self.assertEqual(self.run_coro(guarded_boom()),
                         {"code": 1, "msg": "处理器炸了"})

    # ---------- 请求体 / bot_id ----------
    def test_body_reader_variants(self) -> None:
        original = main_module.web_request
        try:
            main_module.web_request = None
            self.assertEqual(self.run_coro(self.plugin._body()), {})

            main_module.web_request = FakeRequest({"a": 1})
            self.assertEqual(self.run_coro(self.plugin._body()), {"a": 1})

            main_module.web_request = FakeRequest({"a": 1},
                                                  json_error=True, get_json_error=True)
            self.assertEqual(self.run_coro(self.plugin._body()), {})

            main_module.web_request = FakeRequest({"b": 2}, json_error=True)
            self.assertEqual(self.run_coro(self.plugin._body()), {"b": 2})

            main_module.web_request = FakeRequest([1, 2])
            self.assertEqual(self.run_coro(self.plugin._body()), {})

            main_module.web_request = NoJsonRequest({"c": 3})
            self.assertEqual(self.run_coro(self.plugin._body()), {"c": 3})
        finally:
            main_module.web_request = original

    def test_bot_id_resolution(self) -> None:
        plugin = self.plugin
        self.assertEqual(plugin._bot_id({"bot_id": " x "}), "x")
        self.assertEqual(plugin._bot_id(), BOT)

        plugin.store.save_profile(BOT, "u1", name="第一个人")
        plugin.config["bot_id"] = "default"
        self.assertEqual(plugin._bot_id(), "")
        self.assertEqual(plugin._bot_id({"bot_id": ""}, write=True),
                         plugin.store.bot_ids()[0])

        plugin.store.save_profile("other-bot", "u9", name="另一个人")
        self.assertEqual(plugin._bot_id({}, write=True), "default")

        plugin.config["bot_id"] = ""
        self.assertEqual(plugin._bot_id({}, write=True), "default")

        original = plugin.store.bot_ids

        def two_bots():
            return ["a", "b"]

        plugin.store.bot_ids = two_bots
        try:
            self.assertEqual(plugin._bot_id({}, write=True), "default")
        finally:
            plugin.store.bot_ids = original

        def broken():
            raise RuntimeError("bot_ids 挂了")

        plugin.store.bot_ids = broken
        try:
            self.assertEqual(plugin._bot_id({}, write=True), "default")
        finally:
            plugin.store.bot_ids = original

    def test_config_helpers_coerce_strings(self) -> None:
        plugin = self.plugin
        plugin.config.update({
            "bad_int": "不是数字", "bad_float": "也不是",
            "yes": "是", "off": "off", "num": 1,
        })
        self.assertEqual(plugin._cfg_int("bad_int", 7), 7)
        self.assertEqual(plugin._cfg_float("bad_float", 1.5), 1.5)
        self.assertTrue(plugin._cfg_bool("yes", False))
        self.assertFalse(plugin._cfg_bool("off", True))
        self.assertTrue(plugin._cfg_bool("num", False))

    # ---------- 初始化 / 注册 / 卸载 ----------
    def test_init_falls_back_to_plugin_data_dir_and_bad_timezone(self) -> None:
        original_configure = main_module._clock.configure

        def bad_configure(name):
            raise RuntimeError("时区坏了")

        main_module._clock.configure = bad_configure
        try:
            plugin = self.make_plugin()
        finally:
            main_module._clock.configure = original_configure
        try:
            self.assertEqual(plugin.timezone_name, "")
        finally:
            plugin.store.close()

        original_file = main_module.__file__
        original_path_module = sys.modules["astrbot.core.utils.astrbot_path"]
        main_module.__file__ = os.path.join(self.tmp, "pkg", "main.py")
        sys.modules["astrbot.core.utils.astrbot_path"] = types.ModuleType(
            "astrbot.core.utils.astrbot_path")
        try:
            fallback = self.make_plugin()
        finally:
            main_module.__file__ = original_file
            sys.modules["astrbot.core.utils.astrbot_path"] = original_path_module
        try:
            self.assertEqual(fallback.plugin_dir, os.path.dirname(
                os.path.join(self.tmp, "pkg", "main.py")))
            self.assertEqual(fallback.data_dir,
                             os.path.join(fallback.plugin_dir, "data"))
            self.assertTrue(os.path.isdir(fallback.data_dir))
        finally:
            fallback.store.close()

    def test_web_api_cleanup_replaces_stale_routes(self) -> None:
        self.context.registered_web_apis.extend([
            ("/Astr_Amo_data/old-route", ["GET", "POST"], "old"),
            ("/other/keep", ["GET"], "keep"),
        ])
        plugin = self.make_plugin()
        try:
            routes = [item[0] for item in self.context.registered_web_apis]
            self.assertNotIn("/Astr_Amo_data/old-route", routes)
            self.assertIn("/other/keep", routes)
            own = [route for route in routes if route.startswith("/Astr_Amo_data/")]
            self.assertEqual(len(own), 31)
            self.assertIn("/Astr_Amo_data/status", own)
            self.assertIn("/Astr_Amo_data/backup/now", own)
        finally:
            plugin.store.close()

    def test_web_api_registration_failures_do_not_crash_init(self) -> None:
        context = FakeContext(raise_on_register=True)
        plugin = AmoDataPlugin(context, {"bot_id": BOT, "timezone": "Asia/Shanghai"})
        try:
            self.assertEqual(context.registered, [])
            self.assertTrue(os.path.exists(plugin.db_path))
        finally:
            plugin.store.close()

        class BadStr:
            def __str__(self):
                raise RuntimeError("str 挂了")

        weird = FakeContext(registry=[(BadStr(), ["GET"], "x")])
        old = self.context
        self.context = weird
        try:
            plugin2 = self.make_plugin()
        finally:
            self.context = old
        try:
            self.assertEqual(len(weird.registered), 31)
        finally:
            plugin2.store.close()

    def test_terminate_cancels_task_and_closes_store(self) -> None:
        plugin = self.make_plugin()

        class FakeTask:
            def __init__(self):
                self.cancelled = False

            def cancel(self):
                self.cancelled = True

        task = FakeTask()
        plugin._loop_task = task
        self.run_coro(plugin.terminate())
        self.assertTrue(task.cancelled)
        self.assertIsNone(plugin.store._conn)


if __name__ == "__main__":
    unittest.main()
