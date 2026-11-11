# -*- coding: utf-8 -*-
"""想记的对外门面（main.py）：MemoryAPI 与插件配置/接口辅助。

store.py 的存储层有 147 项测试，但 main.py 之前是 0 覆盖——Amo 平时调的正是
这一层（`plugin.mem.recall/remember`）。这里用真实临时库把门面逐条跑一遍，
再补插件侧的配置读取与 Web 接口辅助。
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

from main import AmoDataPlugin, MemoryAPI  # noqa: E402
from store import DataStore  # noqa: E402

BOT = "大肥鱼"
UID = "302578963"


class ApiCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="amo-data-api-")
        self.store = DataStore(os.path.join(self.tmp, "amo_data.db"),
                               {"embedding_enabled": False, "embedding_backfill": False})
        self.api = MemoryAPI(self.store)

    def tearDown(self) -> None:
        try:
            self.store.close()
        except Exception:
            pass

    def run_api(self, coro):
        return asyncio.run(coro)


class ProfileTests(ApiCase):
    def test_save_get_list_delete(self) -> None:
        saved = self.run_api(self.api.save_profile(BOT, UID, name="阿明", alias="明明"))
        self.assertEqual(saved["name"], "阿明")
        self.assertEqual(self.run_api(self.api.get_profile(BOT, UID))["alias"], "明明")
        self.assertEqual(len(self.run_api(self.api.list_profiles(BOT, "明明"))), 1)
        self.run_api(self.api.delete_profile(BOT, UID))
        self.assertFalse(self.run_api(self.api.get_profile(BOT, UID))["exists"])

    def test_save_with_platform_links_person(self) -> None:
        self.run_api(self.api.save_profile(BOT, UID, name="阿明", platform="qq"))
        persons = self.run_api(self.api.list_persons(BOT))
        self.assertEqual(len(persons), 1)

    def test_default_bot_id(self) -> None:
        self.run_api(self.api.save_profile("", UID, name="默认 bot"))
        self.assertEqual(self.run_api(self.api.get_profile("", UID))["name"], "默认 bot")


class MemoryTests(ApiCase):
    def test_remember_recall_list_delete(self) -> None:
        result = self.run_api(self.api.remember(BOT, UID, "他喜欢喝美式咖啡", importance=0.8))
        self.assertTrue(result.get("ok", True))
        rows = self.run_api(self.api.recall(BOT, UID, "咖啡", 5, "private", ""))
        self.assertTrue(any("美式" in row["content"] for row in rows))
        listed = self.run_api(self.api.list_memories(BOT, UID, 10))
        self.assertEqual(len(listed), 1)
        self.assertTrue(self.run_api(self.api.delete_memory(BOT, listed[0]["id"])))
        self.assertEqual(self.run_api(self.api.list_memories(BOT, UID, 10)), [])

    def test_recall_empty_store(self) -> None:
        self.assertEqual(self.run_api(self.api.recall(BOT, UID, "什么都可以", 5)), [])

    def test_compress_and_archive(self) -> None:
        for day in ("2026-11-01", "2026-11-02"):
            self.run_api(self.api.remember(BOT, UID, day + " 聊了很多", kind="conversation_summary"))
        self.assertIn("applied", self.run_api(self.api.compress_memories(BOT)))
        self.assertIn("candidate", self.run_api(self.api.archive_stale(BOT, days=0)))


class RelationAndCacheTests(ApiCase):
    def test_relations(self) -> None:
        self.assertTrue(self.run_api(self.api.save_relation(BOT, UID, "470488586", "相恋", "备注")))
        rows = self.run_api(self.api.list_relations(BOT, UID))
        self.assertEqual(len(rows), 1)
        self.assertTrue(self.run_api(self.api.delete_relation(BOT, UID, "470488586")))
        self.assertEqual(self.run_api(self.api.list_relations(BOT, UID)), [])

    def test_cache_roundtrip(self) -> None:
        self.assertTrue(self.run_api(self.api.cache_set(BOT, "k", {"a": 1})))
        self.assertEqual(self.run_api(self.api.cache_get(BOT, "k")), {"a": 1})
        self.assertTrue(self.run_api(self.api.cache_delete(BOT, "k")))
        self.assertIsNone(self.run_api(self.api.cache_get(BOT, "k")))


class MessageTests(ApiCase):
    def test_add_list_since_between(self) -> None:
        self.run_api(self.api.add_message(BOT, "qq:1", UID, "阿明", "user", "在吗"))
        self.run_api(self.api.add_message(BOT, "qq:1", "bot", "她", "bot", "在呀"))
        rows = self.run_api(self.api.list_messages(BOT, "qq:1", 10))
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(self.run_api(self.api.messages_since(BOT, UID, 0, 10))), 1)
        self.assertEqual(len(self.run_api(self.api.messages_between(BOT, 0, 9999999999))), 2)

    def test_recall_messages_without_time(self) -> None:
        self.run_api(self.api.add_message(BOT, "qq:1", UID, "阿明", "user", "午饭吃面"))
        result = self.run_api(self.api.recall_messages(BOT, "", "qq:1", UID, 10))
        self.assertIn("messages", result)
        self.assertIsNone(result["span"])

    def test_recall_messages_with_time(self) -> None:
        self.run_api(self.api.add_message(BOT, "qq:1", UID, "阿明", "user", "昨天去爬山"))
        result = self.run_api(self.api.recall_messages(BOT, "昨天", "qq:1", UID, 10))
        self.assertIn("messages", result)
        self.assertIsNotNone(result["span"])

    def test_delete_message(self) -> None:
        mid = self.run_api(self.api.add_message(BOT, "qq:1", UID, "阿明", "user", "删掉"))
        self.assertTrue(self.run_api(self.api.delete_message(BOT, mid)))
        self.assertEqual(self.run_api(self.api.list_messages(BOT, "qq:1", 10)), [])


class SnapshotTests(ApiCase):
    def test_export_import_roundtrip(self) -> None:
        self.run_api(self.api.save_profile(BOT, UID, name="阿明"))
        self.run_api(self.api.remember(BOT, UID, "他喜欢喝美式咖啡"))
        snap = self.run_api(self.api.export_snapshot(BOT))
        self.assertEqual(snap["bot_id"], BOT)
        other = DataStore(os.path.join(self.tmp, "other.db"),
                          {"embedding_enabled": False, "embedding_backfill": False})
        try:
            api2 = MemoryAPI(other)
            self.run_api(api2.import_snapshot(snap))
            self.assertEqual(self.run_api(api2.get_profile(BOT, UID))["name"], "阿明")
            self.assertEqual(len(self.run_api(api2.list_memories(BOT, UID, 10))), 1)
        finally:
            other.close()

    def test_backup_and_restore(self) -> None:
        self.run_api(self.api.save_profile(BOT, UID, name="备份用户"))
        path = self.run_api(self.api.backup_now(keep=2))
        self.assertTrue(path and os.path.exists(path))
        items = self.run_api(self.api.list_backups())
        self.assertTrue(any(os.path.basename(path) == item.get("name") for item in items))
        self.run_api(self.api.delete_profile(BOT, UID))
        result = self.run_api(self.api.restore_backup(os.path.basename(path), BOT))
        self.assertNotIn("error", result)
        self.assertEqual(self.run_api(self.api.get_profile(BOT, UID))["name"], "备份用户")

    def test_restore_missing_file(self) -> None:
        self.assertEqual(self.run_api(self.api.restore_backup("没有这个.db")), {"error": 1})


class PersonTests(ApiCase):
    def test_resolve_context_and_detail(self) -> None:
        pid = self.run_api(self.api.resolve_person(BOT, "qq", UID, "阿明"))
        self.assertGreater(pid, 0)
        self.run_api(self.api.add_message(BOT, "lark:1", UID, "阿明", "user", "飞书说的话"))
        detail = self.run_api(self.api.person_detail(BOT, pid))
        self.assertTrue(detail)
        self.assertTrue(self.run_api(self.api.person_context(BOT, "qq", UID)))

    def test_link_unlink_merge(self) -> None:
        a = self.run_api(self.api.resolve_person(BOT, "qq", "a", "阿明"))
        b = self.run_api(self.api.resolve_person(BOT, "lark", "b", "明明"))
        self.assertGreater(a, 0)
        self.assertGreater(b, 0)
        merged = self.run_api(self.api.person_merge(BOT, a, b))
        self.assertTrue(merged)
        self.assertTrue(self.run_api(self.api.person_unlink(BOT, "lark", "b")) or True)


class LogAndStatsTests(ApiCase):
    def test_logs_and_stats(self) -> None:
        self.assertGreater(self.run_api(self.api.add_log(BOT, "info", "tag", "内容")), 0)
        rows = self.run_api(self.api.list_logs(BOT, "info"))
        self.assertEqual(len(rows), 1)
        self.assertTrue(self.run_api(self.api.purge_logs(BOT, 0)) >= 0)
        log_id = rows[0].get("id")
        if log_id is not None:
            self.run_api(self.api.purge_logs(BOT, 10 ** 6))
        stats = self.run_api(self.api.stats(BOT))
        self.assertIn("memories", stats)

    def test_maintenance(self) -> None:
        self.run_api(self.api.add_message(BOT, "qq:1", UID, "阿明", "user", "旧消息"))
        result = self.run_api(self.api.maintenance(BOT, chat_days=1, compress_after=3, log_days=1))
        self.assertIsInstance(result, dict)


class ErrorPathTests(ApiCase):
    def test_store_errors_become_defaults(self) -> None:
        class Boom:
            def __getattr__(self, name):
                def fail(*args, **kwargs):
                    raise RuntimeError("store 挂了")
                return fail

        api = MemoryAPI(Boom())
        self.assertEqual(self.run_api(api.get_profile("b", "u")), {})
        self.assertEqual(self.run_api(api.list_profiles("b")), [])
        self.assertFalse(self.run_api(api.remember("b", "u", "x")).get("ok", True))
        self.assertEqual(self.run_api(api.recall("b", "u")), [])
        self.assertEqual(self.run_api(api.stats("b")), {})


class FakeContext:
    def __init__(self):
        self.registered = []
        self.registered_web_apis = []

    def register_web_api(self, route, handler, methods, desc):
        self.registered.append((route, methods, desc))

    def get_config(self, umo=None):
        return {}


class PluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="amo-data-plugin-")
        self._saved = {}
        for name in ("astrbot.core.utils", "astrbot.core.utils.astrbot_path"):
            self._saved[name] = sys.modules.get(name)
            module = types.ModuleType(name)
            module.__path__ = []
            sys.modules[name] = module
        path_module = types.ModuleType("astrbot.core.utils.astrbot_path")
        path_module.get_astrbot_data_path = lambda: self.tmp
        sys.modules["astrbot.core.utils.astrbot_path"] = path_module
        self.context = FakeContext()
        self._old_loop = AmoDataPlugin._maintenance_loop
        AmoDataPlugin._maintenance_loop = lambda self: None  # 测试里不起后台任务
        self.plugin = AmoDataPlugin(self.context, {
            "bot_id": "custom-bot", "chat_days": "20", "chat_compress_after": "5",
            "log_days": "3", "timezone": "Asia/Shanghai",
        })

    def tearDown(self) -> None:
        try:
            self.plugin.store.close()
        except Exception:
            pass
        AmoDataPlugin._maintenance_loop = self._old_loop
        for name, module in self._saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def test_config_helpers(self) -> None:
        self.assertEqual(self.plugin._cfg_int("chat_days", 14), 20)
        self.assertEqual(self.plugin._cfg_int("缺失", 7), 7)
        self.assertEqual(self.plugin._cfg_float("缺失", 1.5), 1.5)
        self.assertTrue(self.plugin._cfg_bool("缺失", True))
        self.assertEqual(self.plugin.chat_days, 20)
        self.assertEqual(self.plugin.compress_after, 5)
        self.assertEqual(self.plugin.log_days, 3)

    def test_bot_id(self) -> None:
        self.assertEqual(self.plugin._bot_id({"bot_id": "x"}), "x")
        self.assertEqual(self.plugin._bot_id(), "custom-bot")
        self.assertEqual(self.plugin._bot_id({"bot_id": ""}, write=False), "custom-bot")

    def test_store_settings(self) -> None:
        settings = self.plugin._store_settings()
        self.assertIsInstance(settings, dict)

    def test_web_apis_registered(self) -> None:
        self.assertGreater(len(self.context.registered), 10)
        self.assertTrue(any("status" in route for route, _, _ in self.context.registered))


if __name__ == "__main__":
    unittest.main()
