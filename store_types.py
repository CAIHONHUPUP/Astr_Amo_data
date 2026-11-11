# -*- coding: utf-8 -*-
"""store_* mixin 组合后的公共接口（只给 mypy 看，运行时用 object 顶替）。"""
from __future__ import annotations

from typing import Any, Protocol


class StoreLike(Protocol):
    """DataStore 组合完成后的公共接口。"""

    _CN_NUM: Any
    _EXPORT_TABLES: Any
    _PERIODS: Any
    _emb_cache: Any
    _emb_failed: Any
    _emb_pending: Any
    _emb_pending_lock: Any
    _emb_queue: Any
    _emb_total: Any
    conn: Any
    def get_profile(self, bot_id, uid):
        ...
    def list_memories(self, bot_id, uid=..., limit=...):
        ...
    lock: Any
    def remember(self, bot_id, uid, content, kind=..., importance=..., scope=..., group_id=..., idem_key=..., created_at=..., last_hit_at=..., hit_count=..., keep_existing_content=...):
        ...
    def resolve_person(self, bot_id, platform, uid, name=..., auto_merge=...):
        ...
    def save_profile(self, bot_id, uid, name=..., alias=..., relation=..., remark=...):
        ...
    def save_relation(self, bot_id, a_uid, b_uid, relation, note=...):
        ...
    def set_identity_alias(self, bot_id, platform, uid, alias_platform=..., alias_uid=...):
        ...
    def setting(self, key, default=...):
        ...

