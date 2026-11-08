# -*- coding: utf-8 -*-
"""我想永远记住你（Astr_Amo_data）。

给「我想永远陪伴你」配套的轻量数据后端：记忆、画像、关系网、缓存。

- 不做通用大而全的记忆服务，只服务 Amo；
- 身份直接用平台 uid，不做哈希、不做五元组；
- 没有鉴权闸门、证据链、审计、多人格嵌套；
- 画像（名字/称呼/关系/备注）随时可被 Amo 或面板改写并保存回去。

对外接口挂在插件实例的 ``mem`` 上，Amo 只需要认这几个方法：

    await plugin.mem.get_profile(bot_id, uid)
    await plugin.mem.save_profile(bot_id, uid, name=..., alias=..., relation=..., remark=...)
    await plugin.mem.remember(bot_id, uid, content, kind=..., importance=..., scope=..., group_id=..., idem_key=...)
    await plugin.mem.recall(bot_id, uid, query, limit, scope, group_id)
    await plugin.mem.delete_memory(bot_id, memory_id)
    await plugin.mem.list_relations(bot_id, uid)
    await plugin.mem.save_relation(bot_id, a_uid, b_uid, relation, note)
    await plugin.mem.delete_relation(bot_id, a_uid, b_uid)
    await plugin.mem.cache_get(bot_id, key) / cache_set / cache_delete
"""

from __future__ import annotations

import os

from astrbot.api import logger
from astrbot.api.star import Context, Star

try:  # AstrBot 4.x
    from astrbot.api.event import filter  # noqa: F401
except Exception:  # pragma: no cover - 老版本没有该模块时不影响加载
    filter = None

try:
    from astrbot.api.web import request as web_request
except Exception:  # pragma: no cover
    try:
        from quart import request as web_request  # type: ignore
    except Exception:
        web_request = None

try:
    from .store import DataStore
except ImportError:  # 单文件加载时的兜底
    from store import DataStore  # type: ignore

PLUGIN_NAME = "Astr_Amo_data"
PLUGIN_VERSION = "1.0.0"
DEFAULT_BOT_ID = "default"


class MemoryAPI:
    """给 Amo 调用的门面：把同步的 DataStore 包成 async，顺便兜住异常。

    Amo 那边只需要拿到插件实例，然后 `await plugin.mem.recall(...)`；
    出错一律返回空值/False，绝不把异常抛回聊天主链路。
    """

    def __init__(self, store: DataStore):
        self._store = store

    @staticmethod
    def _bot(bot_id: str) -> str:
        return str(bot_id or DEFAULT_BOT_ID)

    async def get_profile(self, bot_id: str, uid: str) -> dict:
        try:
            return self._store.get_profile(self._bot(bot_id), uid)
        except Exception as exc:
            logger.debug("[amo_data] 读取画像失败: %s" % exc)
            return {}

    async def save_profile(self, bot_id: str, uid: str, name=None, alias=None,
                           relation=None, remark=None) -> dict:
        """Amo 改完称呼/关系直接调这个存回去；只传要改的字段即可。"""
        try:
            return self._store.save_profile(
                self._bot(bot_id), uid,
                name=name, alias=alias, relation=relation, remark=remark,
            )
        except Exception as exc:
            logger.debug("[amo_data] 保存画像失败: %s" % exc)
            return {}

    async def list_profiles(self, bot_id: str, keyword: str = "", limit: int = 100) -> list:
        try:
            return self._store.list_profiles(self._bot(bot_id), keyword, limit)
        except Exception as exc:
            logger.debug("[amo_data] 列画像失败: %s" % exc)
            return []

    async def delete_profile(self, bot_id: str, uid: str) -> dict:
        try:
            return self._store.delete_profile(self._bot(bot_id), uid)
        except Exception as exc:
            logger.debug("[amo_data] 删除画像失败: %s" % exc)
            return {}

    async def remember(self, bot_id: str, uid: str, content: str, kind: str = "fact",
                       importance: float = 0.5, scope: str = "private",
                       group_id: str = "", idem_key: str = "") -> dict:
        try:
            return self._store.remember(
                self._bot(bot_id), uid, content,
                kind=kind, importance=importance, scope=scope,
                group_id=group_id, idem_key=idem_key,
            )
        except Exception as exc:
            logger.debug("[amo_data] 写入记忆失败: %s" % exc)
            return {"ok": False, "reason": str(exc)}

    async def recall(self, bot_id: str, uid: str, query: str = "", limit: int = 6,
                     scope: str = "", group_id: str = "") -> list:
        try:
            return self._store.recall(
                self._bot(bot_id), uid, query,
                limit=limit, scope=scope, group_id=group_id,
            )
        except Exception as exc:
            logger.debug("[amo_data] 召回记忆失败: %s" % exc)
            return []

    async def list_memories(self, bot_id: str, uid: str = "", limit: int = 100) -> list:
        try:
            return self._store.list_memories(self._bot(bot_id), uid, limit)
        except Exception as exc:
            logger.debug("[amo_data] 列记忆失败: %s" % exc)
            return []

    async def delete_memory(self, bot_id: str, memory_id: int) -> bool:
        try:
            return self._store.delete_memory(self._bot(bot_id), memory_id)
        except Exception as exc:
            logger.debug("[amo_data] 删除记忆失败: %s" % exc)
            return False

    async def list_relations(self, bot_id: str, uid: str = "") -> list:
        try:
            return self._store.list_relations(self._bot(bot_id), uid)
        except Exception as exc:
            logger.debug("[amo_data] 列关系失败: %s" % exc)
            return []

    async def save_relation(self, bot_id: str, a_uid: str, b_uid: str,
                            relation: str, note: str = "") -> bool:
        try:
            return self._store.save_relation(self._bot(bot_id), a_uid, b_uid, relation, note)
        except Exception as exc:
            logger.debug("[amo_data] 保存关系失败: %s" % exc)
            return False

    async def delete_relation(self, bot_id: str, a_uid: str, b_uid: str) -> bool:
        try:
            return self._store.delete_relation(self._bot(bot_id), a_uid, b_uid)
        except Exception as exc:
            logger.debug("[amo_data] 删除关系失败: %s" % exc)
            return False

    async def cache_get(self, bot_id: str, key: str):
        try:
            return self._store.cache_get(self._bot(bot_id), key)
        except Exception:
            return None

    async def cache_set(self, bot_id: str, key: str, value, ttl: int = 0) -> bool:
        try:
            return self._store.cache_set(self._bot(bot_id), key, value, ttl)
        except Exception:
            return False

    async def cache_delete(self, bot_id: str, key: str) -> bool:
        try:
            return self._store.cache_delete(self._bot(bot_id), key)
        except Exception:
            return False

    async def stats(self, bot_id: str) -> dict:
        try:
            return self._store.stats(self._bot(bot_id))
        except Exception:
            return {}


class AmoDataPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.name = PLUGIN_NAME
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = str(getattr(self, "data_dir", "") or "").strip()
        if not data_dir:
            try:
                from astrbot.core.utils.astrbot_path import get_astrbot_data_path
                data_dir = os.path.join(str(get_astrbot_data_path()), "plugin_data", PLUGIN_NAME)
            except Exception:
                data_dir = os.path.join(self.plugin_dir, "data")
        os.makedirs(data_dir, exist_ok=True)
        self.data_dir = data_dir
        self.db_path = os.path.join(data_dir, "amo_data.db")
        self.store = DataStore(self.db_path)
        self.mem = MemoryAPI(self.store)
        self._safe_register_web_apis()
        logger.info("[amo_data] 我想永远记住你 %s 已加载（%s）" % (PLUGIN_VERSION, self.db_path))

    # ---------- 工具 ----------
    def _bot_id(self, payload: dict = None) -> str:
        data = payload or {}
        return str(data.get("bot_id") or self.config.get("bot_id") or DEFAULT_BOT_ID)

    async def _body(self) -> dict:
        try:
            if web_request is not None and hasattr(web_request, "json"):
                data = await web_request.json(default={})
                return data if isinstance(data, dict) else {}
        except Exception:
            pass
        try:
            if web_request is not None:
                data = await web_request.get_json(silent=True)
                return data if isinstance(data, dict) else {}
        except Exception:
            pass
        return {}

    # ---------- Web 面板 ----------
    def _safe_register_web_apis(self) -> None:
        routes = [
            ("/status", self.api_status),
            ("/profiles", self.api_profiles),
            ("/profile", self.api_profile),
            ("/profile/save", self.api_profile_save),
            ("/profile/delete", self.api_profile_delete),
            ("/memories", self.api_memories),
            ("/memory/save", self.api_memory_save),
            ("/memory/delete", self.api_memory_delete),
            ("/relations", self.api_relations),
            ("/relation/save", self.api_relation_save),
            ("/relation/delete", self.api_relation_delete),
        ]
        prefixes = [PLUGIN_NAME, "Astr_Amo_data", getattr(self, "name", PLUGIN_NAME)]
        seen = set()
        for prefix in prefixes:
            clean = str(prefix or "").strip("/")
            if not clean or clean in seen:
                continue
            seen.add(clean)
            for path, handler in routes:
                try:
                    self.context.register_web_api(
                        "/" + clean + path, self._guard(handler), ["GET", "POST"], path
                    )
                except Exception as exc:
                    logger.debug("[amo_data] 注册 %s%s 失败: %s" % (clean, path, exc))

    def _guard(self, handler):
        async def wrapper(*args, **kwargs):
            try:
                return await handler(*args, **kwargs)
            except Exception as exc:
                logger.error("[amo_data] Web API 异常: %s" % exc)
                return {"code": 1, "msg": str(exc)}
        return wrapper

    async def api_status(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        return {
            "code": 0,
            "data": {
                "version": PLUGIN_VERSION,
                "bot_id": bot_id,
                "db": self.db_path,
                "counts": self.store.stats(bot_id),
            },
        }

    async def api_profiles(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        keyword = str(payload.get("keyword") or payload.get("q") or "").strip()
        return {"code": 0, "data": {"items": self.store.list_profiles(bot_id, keyword, 500)}}

    async def api_profile(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        uid = str(payload.get("uid") or "").strip()
        if not uid:
            return {"code": 1, "msg": "缺少 uid"}
        return {
            "code": 0,
            "data": {
                "profile": self.store.get_profile(bot_id, uid),
                "memories": self.store.list_memories(bot_id, uid, 200),
            },
        }

    async def api_profile_save(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        uid = str(payload.get("uid") or "").strip()
        if not uid:
            return {"code": 1, "msg": "缺少 uid"}
        profile = self.store.save_profile(
            bot_id, uid,
            name=payload.get("name"),
            alias=payload.get("alias"),
            relation=payload.get("relation"),
            remark=payload.get("remark"),
        )
        return {"code": 0, "data": profile, "msg": "已保存"}

    async def api_profile_delete(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        uid = str(payload.get("uid") or "").strip()
        if not uid:
            return {"code": 1, "msg": "缺少 uid"}
        if str(payload.get("confirm") or "") not in ("1", "true", "yes"):
            return {"code": 1, "msg": "删除需要确认：confirm=1"}
        return {"code": 0, "data": self.store.delete_profile(bot_id, uid), "msg": "已删除"}

    async def api_memories(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        uid = str(payload.get("uid") or "").strip()
        query = str(payload.get("q") or "").strip()
        if query:
            items = self.store.recall(bot_id, uid, query, 100) if uid else []
        else:
            items = self.store.list_memories(bot_id, uid, 200)
        return {"code": 0, "data": {"items": items}}

    async def api_memory_save(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        uid = str(payload.get("uid") or "").strip()
        content = str(payload.get("content") or "").strip()
        if not uid or not content:
            return {"code": 1, "msg": "uid 和 content 不能为空"}
        result = self.store.remember(
            bot_id, uid, content,
            kind=str(payload.get("kind") or "manual"),
            importance=payload.get("importance", 0.6),
            scope=str(payload.get("scope") or "private"),
            group_id=str(payload.get("group_id") or ""),
        )
        return {"code": 0 if result.get("ok") else 1, "data": result, "msg": "已记住"}

    async def api_memory_delete(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        try:
            memory_id = int(payload.get("id") or 0)
        except (TypeError, ValueError):
            memory_id = 0
        if not memory_id:
            return {"code": 1, "msg": "缺少 id"}
        return {"code": 0, "data": {"deleted": self.store.delete_memory(bot_id, memory_id)}}

    async def api_relations(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        uid = str(payload.get("uid") or "").strip()
        return {"code": 0, "data": {"items": self.store.list_relations(bot_id, uid)}}

    async def api_relation_save(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        a_uid = str(payload.get("a_uid") or "").strip()
        b_uid = str(payload.get("b_uid") or "").strip()
        if not a_uid or not b_uid:
            return {"code": 1, "msg": "需要两个 uid"}
        ok = self.store.save_relation(
            bot_id, a_uid, b_uid,
            str(payload.get("relation") or "朋友"),
            str(payload.get("note") or ""),
        )
        return {"code": 0 if ok else 1, "msg": "已保存" if ok else "保存失败"}

    async def api_relation_delete(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        a_uid = str(payload.get("a_uid") or "").strip()
        b_uid = str(payload.get("b_uid") or "").strip()
        if not a_uid or not b_uid:
            return {"code": 1, "msg": "需要两个 uid"}
        ok = self.store.delete_relation(bot_id, a_uid, b_uid)
        return {"code": 0 if ok else 1, "msg": "已删除" if ok else "没有这条关系"}

    # ---------- 生命周期 ----------
    async def terminate(self):
        try:
            self.store.close()
        except Exception:
            pass
        logger.info("[amo_data] 我想永远记住你已卸载")
