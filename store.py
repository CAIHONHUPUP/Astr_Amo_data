# -*- coding: utf-8 -*-
"""我想永远记住你 —— 数据存储入口（组合各子模块，对外 API 不变）。

    store_util.py      常量 + 纯函数
    store_schema.py    建表/迁移/备份/快照
    store_people.py    画像/身份/人物
    store_memory.py    记忆写入/召回/压缩
    store_messages.py  消息/时间窗/总结
    store_logs.py      关系/缓存/日志
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from typing import Any
from typing import Dict
from typing import List
from typing import Optional
import os
import queue
import sqlite3
import threading

if TYPE_CHECKING:  # 只给类型检查看：包内相对导入
    from .store_schema import SchemaMixin
    from .store_people import PeopleMixin
    from .store_memory import MemoryMixin
    from .store_messages import MessagesMixin
    from .store_logs import LogsMixin
    from .store_util import (
        DEFAULT_SETTINGS,
        KNOWN_PLATFORMS,
        PLATFORM_ALIASES,
        PLATFORM_LABELS,
        RECALL_HALF_LIFE_DAYS,
        RECALL_IMPORTANCE_DECAY,
        RECALL_MIN_REL,
        RECALL_POOL,
        RECALL_W_FRESH,
        RECALL_W_IMPORTANCE,
        RECALL_W_REL,
        SLOT_PATTERNS,
        STOP_BIGRAMS,
        STOP_CHARS,
        _bm25_scores,
        _bot_filter,
        _clean,
        _clock,
        _cosine,
        _freshness,
        _from_vec,
        _informative_terms,
        _jaccard,
        _name_key,
        _norm_key,
        _now,
        _parse_time_span,
        _query_chars,
        _recall_tokens,
        _slot_facts,
        _to_vec,
        _unique_labels,
        logger,
        normalize_platform,
        platform_label,
        platform_of_umo,
    )
else:  # 运行时：包模式优先，按文件直接加载时退回绝对导入
    try:
        from .store_schema import SchemaMixin
        from .store_people import PeopleMixin
        from .store_memory import MemoryMixin
        from .store_messages import MessagesMixin
        from .store_logs import LogsMixin
        from .store_util import (
            DEFAULT_SETTINGS,
            KNOWN_PLATFORMS,
            PLATFORM_ALIASES,
            PLATFORM_LABELS,
            RECALL_HALF_LIFE_DAYS,
            RECALL_IMPORTANCE_DECAY,
            RECALL_MIN_REL,
            RECALL_POOL,
            RECALL_W_FRESH,
            RECALL_W_IMPORTANCE,
            RECALL_W_REL,
            SLOT_PATTERNS,
            STOP_BIGRAMS,
            STOP_CHARS,
            _bm25_scores,
            _bot_filter,
            _clean,
            _clock,
            _cosine,
            _freshness,
            _from_vec,
            _informative_terms,
            _jaccard,
            _name_key,
            _norm_key,
            _now,
            _parse_time_span,
            _query_chars,
            _recall_tokens,
            _slot_facts,
            _to_vec,
            _unique_labels,
            logger,
            normalize_platform,
            platform_label,
            platform_of_umo,
        )
    except ImportError:  # 直接按文件加载时
        from store_schema import SchemaMixin
        from store_people import PeopleMixin
        from store_memory import MemoryMixin
        from store_messages import MessagesMixin
        from store_logs import LogsMixin
        from store_util import (
            DEFAULT_SETTINGS,
            KNOWN_PLATFORMS,
            PLATFORM_ALIASES,
            PLATFORM_LABELS,
            RECALL_HALF_LIFE_DAYS,
            RECALL_IMPORTANCE_DECAY,
            RECALL_MIN_REL,
            RECALL_POOL,
            RECALL_W_FRESH,
            RECALL_W_IMPORTANCE,
            RECALL_W_REL,
            SLOT_PATTERNS,
            STOP_BIGRAMS,
            STOP_CHARS,
            _bm25_scores,
            _bot_filter,
            _clean,
            _clock,
            _cosine,
            _freshness,
            _from_vec,
            _informative_terms,
            _jaccard,
            _name_key,
            _norm_key,
            _now,
            _parse_time_span,
            _query_chars,
            _recall_tokens,
            _slot_facts,
            _to_vec,
            _unique_labels,
            logger,
            normalize_platform,
            platform_label,
            platform_of_umo,
        )

# 旧 API 兼容：常量/纯函数仍从 store 模块直接取（测试和面板都在用）
__all__ = [
    "DataStore",
    "DEFAULT_SETTINGS",
    "KNOWN_PLATFORMS",
    "PLATFORM_ALIASES",
    "PLATFORM_LABELS",
    "RECALL_HALF_LIFE_DAYS",
    "RECALL_IMPORTANCE_DECAY",
    "RECALL_MIN_REL",
    "RECALL_POOL",
    "RECALL_W_FRESH",
    "RECALL_W_IMPORTANCE",
    "RECALL_W_REL",
    "SLOT_PATTERNS",
    "STOP_BIGRAMS",
    "STOP_CHARS",
    "_bm25_scores",
    "_bot_filter",
    "_clean",
    "_clock",
    "_cosine",
    "_freshness",
    "_from_vec",
    "_informative_terms",
    "_jaccard",
    "_name_key",
    "_norm_key",
    "_now",
    "_parse_time_span",
    "_query_chars",
    "_recall_tokens",
    "_slot_facts",
    "_to_vec",
    "_unique_labels",
    "logger",
    "normalize_platform",
    "platform_label",
    "platform_of_umo",
]


class DataStore(SchemaMixin, PeopleMixin, MemoryMixin, MessagesMixin, LogsMixin):
    """连接/设置/关闭留在这一层，其余方法见各 mixin。"""

    _CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
               "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    _PERIODS = (
        (("凌晨",), 0, 6),
        (("早上", "早晨", "上午"), 6, 12),
        (("中午",), 11, 14),
        (("下午",), 12, 18),
        (("傍晚", "晚上", "今晚", "昨晚", "夜里", "深夜"), 18, 24),
    )
    _EXPORT_TABLES = ("memories", "profiles", "relations", "persons",
                      "person_links", "identity_alias")
    def __init__(self, db_path: str, settings: Optional[Dict[str, Any]] = None):
        self.path = str(db_path)
        self.lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self.settings: Dict[str, Any] = dict(DEFAULT_SETTINGS)
        if settings:
            self.settings.update({k: v for k, v in settings.items() if v is not None})
        # 向量回落：Ollama 挂了就别每条查询都等超时
        self._emb_down_until = 0.0
        self._emb_cache: Dict[str, List[float]] = {}
        self._emb_queue: "queue.Queue" = queue.Queue(maxsize=512)
        self._emb_pending: set = set()
        self._emb_pending_lock = threading.Lock()
        self._emb_thread: Optional[threading.Thread] = None
        self._emb_total = 0
        self._emb_failed = 0
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._open()
        self._init_schema()
        if self.setting("embedding_enabled"):
            self._ensure_emb_worker()
            if self.setting("embedding_backfill"):
                self.backfill_embeddings(limit=int(self.setting("embedding_backfill_limit", 200)))
    def setting(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, DEFAULT_SETTINGS.get(key, default))
    def _open(self) -> None:
        with self.lock:
            self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=15)
            self._conn.row_factory = sqlite3.Row
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except Exception:
                pass
    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            with self.lock:
                if self._conn is None:
                    self._open()
        assert self._conn is not None
        return self._conn
    def close(self) -> None:
        with self.lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
        # 让后台向量任务尽快退出（worker 只处理到队列空）
        try:
            self._emb_queue.put_nowait(None)
        except Exception:
            pass
