# -*- coding: utf-8 -*-
"""关系网 / 缓存 / 操作日志。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from typing import Any
from typing import Dict
from typing import List
import json

if TYPE_CHECKING:  # 只给类型检查看：包内相对导入
    from .store_util import (
        _bot_filter,
        _clean,
        _now,
    )
else:  # 运行时：包模式优先，按文件直接加载时退回绝对导入
    try:
        from .store_util import (
            _bot_filter,
            _clean,
            _now,
        )
    except ImportError:  # 直接按文件加载时
        from store_util import (
            _bot_filter,
            _clean,
            _now,
        )


if TYPE_CHECKING:
    from .store_types import StoreLike as _StoreBase
else:  # 运行时用 object：协议里的声明不能进 MRO，否则会挡掉别的 mixin 的真实实现
    _StoreBase = object


class LogsMixin(_StoreBase):
    def _display_name(self, bot_id: str, uid: Any) -> str:
        """关系图上的显示名：先画像，再人物卡，最后空。"""
        uid = str(uid or "")
        if not uid:
            return ""
        try:
            with self.lock:
                row = self.conn.execute(
                    "SELECT name FROM profiles WHERE bot_id=? AND uid=? AND name!=''", (bot_id, uid)
                ).fetchone()
            if row and str(row["name"] or "").strip():
                return str(row["name"]).strip()
        except Exception:
            pass
        try:
            with self.lock:
                row = self.conn.execute(
                    "SELECT p.name FROM person_links l JOIN persons p ON p.pid=l.pid "
                    "WHERE l.bot_id=? AND l.uid=? AND p.name!='' LIMIT 1", (bot_id, uid)
                ).fetchone()
            if row and str(row["name"] or "").strip():
                return str(row["name"]).strip()
        except Exception:
            pass
        return ""
    def list_relations(self, bot_id: str, uid: str = "") -> List[Dict[str, Any]]:
        where, params = _bot_filter(bot_id)
        sql = "SELECT a_uid, b_uid, relation, note, updated_at FROM relations WHERE 1=1" + where
        if uid:
            sql += " AND (a_uid=? OR b_uid=?)"
            params.extend([str(uid), str(uid)])
        sql += " ORDER BY updated_at DESC"
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["a_name"] = self._display_name(str(bot_id), item.get("a_uid"))
            item["b_name"] = self._display_name(str(bot_id), item.get("b_uid"))
            out.append(item)
        return out
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
        # ttl>0 正常过期；ttl<0 表示一写入就该过期；ttl=0 表示永不过期
        expires = _now() + int(ttl) if ttl else 0
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
    def cache_keys(self, bot_id: str = "") -> List[str]:
        where, params = _bot_filter(bot_id)
        with self.lock:
            rows = self.conn.execute(
                "SELECT key FROM cache WHERE 1=1" + where + " ORDER BY updated_at DESC", params
            ).fetchall()
        return [str(row["key"]) for row in rows]
    def bot_ids(self) -> List[str]:
        """面板用：列出库里出现过的所有 bot_id。"""
        ids = set()
        with self.lock:
            for table in ("profiles", "memories", "messages", "logs", "relations", "cache", "persons"):
                try:
                    for row in self.conn.execute(
                        "SELECT DISTINCT bot_id FROM %s WHERE bot_id != ''" % table
                    ).fetchall():
                        value = str(row["bot_id"] or "").strip()
                        if value:
                            ids.add(value)
                except Exception:
                    continue
        return sorted(ids)
    def add_log(self, bot_id: str, level: str, tag: str, content: str) -> int:
        text = _clean(content, 2000)
        if not text:
            return 0
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO logs(bot_id, level, tag, content, created_at) VALUES(?,?,?,?,?)",
                (str(bot_id), _clean(level, 16) or "info", _clean(tag, 32), text, _now()),
            )
            self.conn.commit()
            return int(cur.lastrowid or 0)
    def list_logs(self, bot_id: str, level: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        where, params = _bot_filter(bot_id)
        sql = "SELECT id, level, tag, content, created_at FROM logs WHERE 1=1" + where
        if level:
            sql += " AND level=?"
            params.append(str(level))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
    def delete_log(self, bot_id: str, log_id: int) -> bool:
        with self.lock:
            cur = self.conn.execute(
                "DELETE FROM logs WHERE bot_id=? AND id=?", (str(bot_id), int(log_id))
            )
            self.conn.commit()
            return bool(cur.rowcount)
    def clear_logs(self, bot_id: str) -> int:
        with self.lock:
            cur = self.conn.execute("DELETE FROM logs WHERE bot_id=?", (str(bot_id),))
            self.conn.commit()
            return max(0, cur.rowcount or 0)
    def purge_logs(self, bot_id: str, days: int = 7) -> int:
        cutoff = _now() - max(1, int(days)) * 86400
        with self.lock:
            cur = self.conn.execute(
                "DELETE FROM logs WHERE bot_id=? AND created_at<?", (str(bot_id), cutoff)
            )
            self.conn.commit()
            return max(0, cur.rowcount or 0)
