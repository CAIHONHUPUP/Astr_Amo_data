# -*- coding: utf-8 -*-
"""我想永远记住你 —— 轻量数据存储层。

只做四件事：画像、记忆、关系、缓存。没有鉴权、没有证据链、没有哈希身份。

设计要点：
- 全部按 ``bot_id`` 隔离：同一个 QQ 号在不同 bot 下是独立画像和独立记忆；
- ``uid`` 直接用平台原始 ID，不做任何哈希或二次映射；
- 画像字段（name / alias / relation / remark）可以随时被外部改写并保存回去；
- 记忆用 FTS5 做全文检索，不需要向量库；
- 关系网和日程缓存也放这里，陪伴插件只负责聊天。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional


def _now() -> int:
    return int(time.time())


def _clean(text: Any, limit: int = 2000) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value[:limit]


class DataStore:
    """SQLite 存储。所有写操作走同一把 RLock，读操作也加锁，避免并发重开。"""

    def __init__(self, db_path: str):
        self.path = str(db_path)
        self.lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._open()
        self._init_schema()

    # ---------- 连接 ----------
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
        return self._conn

    def close(self) -> None:
        with self.lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def _init_schema(self) -> None:
        with self.lock:
            conn = self.conn
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS profiles (
                    bot_id TEXT NOT NULL,
                    uid TEXT NOT NULL,
                    name TEXT DEFAULT '',
                    alias TEXT DEFAULT '',
                    relation TEXT DEFAULT '',
                    remark TEXT DEFAULT '',
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (bot_id, uid)
                );

                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id TEXT NOT NULL,
                    uid TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'private',
                    group_id TEXT DEFAULT '',
                    kind TEXT DEFAULT 'fact',
                    content TEXT NOT NULL,
                    importance REAL DEFAULT 0.5,
                    hit_count INTEGER DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    last_hit_at INTEGER NOT NULL,
                    idem_key TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_mem_lookup ON memories(bot_id, uid, scope, group_id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_mem_idem ON memories(bot_id, uid, idem_key)
                    WHERE idem_key != '';

                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    content, content='memories', content_rowid='id'
                );
                CREATE TRIGGER IF NOT EXISTS mem_ai AFTER INSERT ON memories BEGIN
                    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
                END;
                CREATE TRIGGER IF NOT EXISTS mem_ad AFTER DELETE ON memories BEGIN
                    DELETE FROM memories_fts WHERE rowid = old.id;
                END;
                CREATE TRIGGER IF NOT EXISTS mem_au AFTER UPDATE OF content ON memories BEGIN
                    UPDATE memories_fts SET content = new.content WHERE rowid = old.id;
                END;

                CREATE TABLE IF NOT EXISTS relations (
                    bot_id TEXT NOT NULL,
                    a_uid TEXT NOT NULL,
                    b_uid TEXT NOT NULL,
                    relation TEXT DEFAULT '朋友',
                    note TEXT DEFAULT '',
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (bot_id, a_uid, b_uid)
                );

                CREATE TABLE IF NOT EXISTS cache (
                    bot_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT DEFAULT '',
                    updated_at INTEGER NOT NULL,
                    expires_at INTEGER DEFAULT 0,
                    PRIMARY KEY (bot_id, key)
                );
                """
            )
            conn.commit()

    # ---------- 画像 ----------
    def get_profile(self, bot_id: str, uid: str) -> Dict[str, Any]:
        with self.lock:
            row = self.conn.execute(
                "SELECT name, alias, relation, remark, updated_at FROM profiles "
                "WHERE bot_id=? AND uid=?",
                (str(bot_id), str(uid)),
            ).fetchone()
        if not row:
            return {
                "uid": str(uid), "name": "", "alias": "", "relation": "",
                "remark": "", "updated_at": 0, "exists": False,
            }
        return {
            "uid": str(uid),
            "name": str(row["name"] or ""),
            "alias": str(row["alias"] or ""),
            "relation": str(row["relation"] or ""),
            "remark": str(row["remark"] or ""),
            "updated_at": int(row["updated_at"] or 0),
            "exists": True,
        }

    def save_profile(
        self,
        bot_id: str,
        uid: str,
        name: Optional[str] = None,
        alias: Optional[str] = None,
        relation: Optional[str] = None,
        remark: Optional[str] = None,
    ) -> Dict[str, Any]:
        """只更新传进来的字段，没传的保持原值；整条不存在就新建。"""
        bot_id, uid = str(bot_id), str(uid)
        with self.lock:
            current = self.get_profile(bot_id, uid)
            merged = {
                "name": current["name"] if name is None else _clean(name, 64),
                "alias": current["alias"] if alias is None else _clean(alias, 128),
                "relation": current["relation"] if relation is None else _clean(relation, 32),
                "remark": current["remark"] if remark is None else _clean(remark, 500),
            }
            self.conn.execute(
                "INSERT INTO profiles(bot_id, uid, name, alias, relation, remark, updated_at) "
                "VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(bot_id, uid) DO UPDATE SET "
                "name=excluded.name, alias=excluded.alias, relation=excluded.relation, "
                "remark=excluded.remark, updated_at=excluded.updated_at",
                (bot_id, uid, merged["name"], merged["alias"], merged["relation"],
                 merged["remark"], _now()),
            )
            self.conn.commit()
        return self.get_profile(bot_id, uid)

    def list_profiles(self, bot_id: str, keyword: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        sql = "SELECT uid, name, alias, relation, remark, updated_at FROM profiles WHERE bot_id=?"
        params: List[Any] = [str(bot_id)]
        if keyword:
            sql += " AND (uid LIKE ? OR name LIKE ? OR alias LIKE ? OR remark LIKE ?)"
            like = "%" + str(keyword) + "%"
            params.extend([like, like, like, like])
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def delete_profile(self, bot_id: str, uid: str) -> Dict[str, int]:
        """删画像，连带这个人的记忆、关系边和缓存。"""
        bot_id, uid = str(bot_id), str(uid)
        counts: Dict[str, int] = {}
        with self.lock:
            for table, sql, params in (
                ("profiles", "DELETE FROM profiles WHERE bot_id=? AND uid=?", (bot_id, uid)),
                ("memories", "DELETE FROM memories WHERE bot_id=? AND uid=?", (bot_id, uid)),
                ("relations", "DELETE FROM relations WHERE bot_id=? AND (a_uid=? OR b_uid=?)",
                 (bot_id, uid, uid)),
            ):
                cur = self.conn.execute(sql, params)
                counts[table] = max(0, cur.rowcount or 0)
            self.conn.commit()
        return counts

    # ---------- 记忆 ----------
    def remember(
        self,
        bot_id: str,
        uid: str,
        content: str,
        kind: str = "fact",
        importance: float = 0.5,
        scope: str = "private",
        group_id: str = "",
        idem_key: str = "",
    ) -> Dict[str, Any]:
        text = _clean(content)
        if not text:
            return {"ok": False, "reason": "empty"}
        bot_id, uid = str(bot_id), str(uid)
        scope = "group" if str(scope) == "group" else "private"
        group_id = str(group_id or "") if scope == "group" else ""
        try:
            importance = max(0.0, min(1.0, float(importance)))
        except (TypeError, ValueError):
            importance = 0.5
        now = _now()
        idem_key = _clean(idem_key, 120)
        with self.lock:
            if idem_key:
                row = self.conn.execute(
                    "SELECT id FROM memories WHERE bot_id=? AND uid=? AND idem_key=?",
                    (bot_id, uid, idem_key),
                ).fetchone()
                if row:
                    return {"ok": True, "memory_id": int(row["id"]), "duplicate": True}
            cur = self.conn.execute(
                "INSERT INTO memories(bot_id, uid, scope, group_id, kind, content, "
                "importance, hit_count, created_at, last_hit_at, idem_key) "
                "VALUES(?,?,?,?,?,?,?,0,?,?,?)",
                (bot_id, uid, scope, group_id, _clean(kind, 32) or "fact", text,
                 importance, now, now, idem_key),
            )
            self.conn.commit()
            memory_id = int(cur.lastrowid or 0)
        return {"ok": True, "memory_id": memory_id, "duplicate": False}

    def recall(
        self,
        bot_id: str,
        uid: str,
        query: str = "",
        limit: int = 6,
        scope: str = "",
        group_id: str = "",
    ) -> List[Dict[str, Any]]:
        """query 为空时按重要度+最近访问排序；有 query 时先走 FTS5，失败退回 LIKE。"""
        bot_id, uid = str(bot_id), str(uid)
        limit = max(1, min(int(limit or 6), 100))
        base = (
            "SELECT id, content, kind, importance, created_at, hit_count, scope, group_id "
            "FROM memories WHERE bot_id=? AND uid=?"
        )
        params: List[Any] = [bot_id, uid]
        if scope in ("private", "group"):
            base += " AND scope=?"
            params.append(scope)
        if group_id:
            base += " AND group_id=?"
            params.append(str(group_id))

        rows: List[sqlite3.Row] = []
        text = _clean(query, 200)
        if text:
            try:
                with self.lock:
                    rows = self.conn.execute(
                        "SELECT m.id, m.content, m.kind, m.importance, m.created_at, "
                        "m.hit_count, m.scope, m.group_id FROM memories m "
                        "JOIN memories_fts f ON f.rowid = m.id "
                        "WHERE f.content MATCH ? AND m.bot_id=? AND m.uid=? "
                        "ORDER BY m.importance DESC, m.last_hit_at DESC LIMIT ?",
                        (text, bot_id, uid, limit),
                    ).fetchall()
            except Exception:
                rows = []
            if not rows:
                with self.lock:
                    rows = self.conn.execute(
                        base + " AND content LIKE ? ORDER BY importance DESC, last_hit_at DESC LIMIT ?",
                        params + ["%" + text + "%", limit],
                    ).fetchall()
        else:
            with self.lock:
                rows = self.conn.execute(
                    base + " ORDER BY importance DESC, last_hit_at DESC LIMIT ?",
                    params + [limit],
                ).fetchall()

        items = [dict(row) for row in rows]
        if items:
            now = _now()
            with self.lock:
                self.conn.executemany(
                    "UPDATE memories SET hit_count=hit_count+1, last_hit_at=? WHERE id=?",
                    [(now, item["id"]) for item in items],
                )
                self.conn.commit()
        return items

    def list_memories(self, bot_id: str, uid: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        sql = (
            "SELECT id, uid, content, kind, importance, created_at, hit_count, scope, group_id "
            "FROM memories WHERE bot_id=?"
        )
        params: List[Any] = [str(bot_id)]
        if uid:
            sql += " AND uid=?"
            params.append(str(uid))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def delete_memory(self, bot_id: str, memory_id: int) -> bool:
        with self.lock:
            cur = self.conn.execute(
                "DELETE FROM memories WHERE bot_id=? AND id=?",
                (str(bot_id), int(memory_id)),
            )
            self.conn.commit()
            return bool(cur.rowcount)

    # ---------- 关系网 ----------
    def list_relations(self, bot_id: str, uid: str = "") -> List[Dict[str, Any]]:
        sql = "SELECT a_uid, b_uid, relation, note, updated_at FROM relations WHERE bot_id=?"
        params: List[Any] = [str(bot_id)]
        if uid:
            sql += " AND (a_uid=? OR b_uid=?)"
            params.extend([str(uid), str(uid)])
        sql += " ORDER BY updated_at DESC"
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def save_relation(
        self, bot_id: str, a_uid: str, b_uid: str, relation: str, note: str = ""
    ) -> bool:
        a_uid, b_uid = str(a_uid), str(b_uid)
        if not a_uid or not b_uid or a_uid == b_uid:
            return False
        with self.lock:
            self.conn.execute(
                "INSERT INTO relations(bot_id, a_uid, b_uid, relation, note, updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(bot_id, a_uid, b_uid) DO UPDATE SET "
                "relation=excluded.relation, note=excluded.note, updated_at=excluded.updated_at",
                (str(bot_id), a_uid, b_uid, _clean(relation, 32) or "朋友", _clean(note, 200), _now()),
            )
            self.conn.commit()
        return True

    def delete_relation(self, bot_id: str, a_uid: str, b_uid: str) -> bool:
        with self.lock:
            cur = self.conn.execute(
                "DELETE FROM relations WHERE bot_id=? AND a_uid=? AND b_uid=?",
                (str(bot_id), str(a_uid), str(b_uid)),
            )
            self.conn.commit()
            return bool(cur.rowcount)

    # ---------- 缓存（给日程/世界观这类大块数据用） ----------
    def cache_get(self, bot_id: str, key: str) -> Any:
        with self.lock:
            row = self.conn.execute(
                "SELECT value, expires_at FROM cache WHERE bot_id=? AND key=?",
                (str(bot_id), str(key)),
            ).fetchone()
        if not row:
            return None
        if row["expires_at"] and int(row["expires_at"]) < _now():
            self.cache_delete(bot_id, key)
            return None
        try:
            return json.loads(row["value"])
        except Exception:
            return None

    def cache_set(self, bot_id: str, key: str, value: Any, ttl: int = 0) -> bool:
        try:
            payload = json.dumps(value, ensure_ascii=False)
        except Exception:
            return False
        expires = _now() + int(ttl) if ttl and int(ttl) > 0 else 0
        with self.lock:
            self.conn.execute(
                "INSERT INTO cache(bot_id, key, value, updated_at, expires_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(bot_id, key) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at, expires_at=excluded.expires_at",
                (str(bot_id), str(key), payload, _now(), expires),
            )
            self.conn.commit()
        return True

    def cache_delete(self, bot_id: str, key: str) -> bool:
        with self.lock:
            cur = self.conn.execute(
                "DELETE FROM cache WHERE bot_id=? AND key=?", (str(bot_id), str(key))
            )
            self.conn.commit()
            return bool(cur.rowcount)

    def cache_keys(self, bot_id: str) -> List[str]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT key FROM cache WHERE bot_id=? ORDER BY updated_at DESC", (str(bot_id),)
            ).fetchall()
        return [str(row["key"]) for row in rows]

    def stats(self, bot_id: str) -> Dict[str, int]:
        with self.lock:
            out = {}
            for name, sql in (
                ("profiles", "SELECT COUNT(*) AS c FROM profiles WHERE bot_id=?"),
                ("memories", "SELECT COUNT(*) AS c FROM memories WHERE bot_id=?"),
                ("relations", "SELECT COUNT(*) AS c FROM relations WHERE bot_id=?"),
                ("cache", "SELECT COUNT(*) AS c FROM cache WHERE bot_id=?"),
            ):
                row = self.conn.execute(sql, (str(bot_id),)).fetchone()
                out[name] = int(row["c"] or 0) if row else 0
        return out
