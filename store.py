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


def _bot_filter(bot_id: str, column: str = "bot_id"):
    """bot_id 为空表示不过滤（面板默认看全部 bot）。"""
    value = str(bot_id or "").strip()
    if not value:
        return "", []
    return " AND %s=?" % column, [value]


# 平台前缀 -> 给人看的名字。Amo 传进来的平台名可能是 aiocqhttp / lark / qq_official …
PLATFORM_LABELS = {
    "aiocqhttp": "QQ",
    "qq": "QQ",
    "qq_official": "QQ",
    "qqofficial": "QQ",
    "onebot": "QQ",
    "napcat": "QQ",
    "lark": "飞书",
    "feishu": "飞书",
    "wecom": "企业微信",
    "wework": "企业微信",
    "dingtalk": "钉钉",
    "telegram": "Telegram",
    "discord": "Discord",
    "slack": "Slack",
    "kook": "KOOK",
    "satori": "Satori",
    "webchat": "网页",
    "websocket": "网页",
    "wechat": "微信",
    "wx": "微信",
}


def platform_label(platform: str) -> str:
    key = str(platform or "").strip().lower()
    if not key:
        return "未知"
    return PLATFORM_LABELS.get(key, str(platform).strip())


def platform_of_umo(umo: str) -> str:
    """umo 形如 ``aiocqhttp:GroupMessage:123456``，第一段就是平台。"""
    text = str(umo or "").strip()
    if not text:
        return ""
    return text.split(":", 1)[0].strip()


def _name_key(text: Any) -> str:
    """认人用的名字归一化：去掉空白和常见装饰符号，兼容大小写。"""
    value = re.sub(r"[\s\u3000·•、,，。.!！?？~～\-—_]+", "", str(text or ""))
    return value.strip().lower()[:32]


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

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id TEXT NOT NULL,
                    umo TEXT NOT NULL,
                    uid TEXT DEFAULT '',
                    name TEXT DEFAULT '',
                    role TEXT DEFAULT 'user',
                    content TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    compressed INTEGER DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_msg_scan ON messages(bot_id, umo, created_at);

                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id TEXT NOT NULL,
                    level TEXT DEFAULT 'info',
                    tag TEXT DEFAULT '',
                    content TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_log_scan ON logs(bot_id, created_at);

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

                CREATE TABLE IF NOT EXISTS persons (
                    pid INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id TEXT NOT NULL,
                    name TEXT DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_person_bot ON persons(bot_id, updated_at);

                CREATE TABLE IF NOT EXISTS person_links (
                    bot_id TEXT NOT NULL,
                    platform TEXT NOT NULL DEFAULT '',
                    uid TEXT NOT NULL,
                    pid INTEGER NOT NULL,
                    name TEXT DEFAULT '',
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY (bot_id, platform, uid)
                );
                CREATE INDEX IF NOT EXISTS idx_link_pid ON person_links(bot_id, pid);
                """
            )
            self._migrate_columns()
            conn.commit()

    def _migrate_columns(self) -> None:
        """老库补列：messages 增加 platform / pid。"""
        with self.lock:
            conn = self.conn
            try:
                cols = {str(row["name"]) for row in conn.execute("PRAGMA table_info(messages)")}
            except Exception:
                return
            for column, ddl in (
                ("platform", "ALTER TABLE messages ADD COLUMN platform TEXT DEFAULT ''"),
                ("pid", "ALTER TABLE messages ADD COLUMN pid INTEGER DEFAULT 0"),
            ):
                if column in cols:
                    continue
                try:
                    conn.execute(ddl)
                except Exception:
                    continue
            try:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_pid ON messages(bot_id, pid, created_at)")
            except Exception:
                pass

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

    def find_profile(self, uid: str) -> Dict[str, Any]:
        """跨 bot 找画像：面板默认聚合所有 bot，编辑时要先知道这条属于哪个 bot。"""
        uid = str(uid)
        if not uid:
            return {}
        with self.lock:
            row = self.conn.execute(
                "SELECT bot_id FROM profiles WHERE uid=? ORDER BY updated_at DESC LIMIT 1",
                (uid,),
            ).fetchone()
        if not row:
            return {}
        bot_id = str(row["bot_id"] or "")
        profile = self.get_profile(bot_id, uid)
        profile["bot_id"] = bot_id
        return profile

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
        where, params = _bot_filter(bot_id)
        sql = "SELECT uid, name, alias, relation, remark, updated_at FROM profiles WHERE 1=1" + where
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

    # ---------- 人物：同一个人跨平台（QQ / 飞书 / …）合并 ----------
    def resolve_person(
        self, bot_id: str, platform: str, uid: str, name: str = "", auto_merge: bool = True
    ) -> int:
        """拿到 (平台, uid) 对应的人物 pid：没有就建，同名就自动并过去。

        自动合并只在「同一个 bot、名字归一化后完全一样、并且只命中一个人」时发生，
        宁可少并错，不可乱并；并错了面板可以解绑。
        """
        bot_id, platform, uid = str(bot_id), str(platform or ""), str(uid)
        if not uid:
            return 0
        display = _clean(name, 64)
        with self.lock:
            row = self.conn.execute(
                "SELECT pid FROM person_links WHERE bot_id=? AND platform=? AND uid=?",
                (bot_id, platform, uid),
            ).fetchone()
            if row:
                pid = int(row["pid"] or 0)
                if pid:
                    if display:
                        self.conn.execute(
                            "UPDATE person_links SET name=? WHERE bot_id=? AND platform=? AND uid=?",
                            (display, bot_id, platform, uid),
                        )
                        self.conn.execute(
                            "UPDATE persons SET name=?, updated_at=? WHERE pid=?",
                            (display, _now(), pid),
                        )
                        self.conn.commit()
                    return pid
            key = _name_key(display)
            target = 0
            if auto_merge and len(key) >= 2:
                rows = self.conn.execute(
                    "SELECT pid, name FROM person_links WHERE bot_id=? AND pid>0 AND platform!=?",
                    (bot_id, platform),
                ).fetchall()
                pids = {
                    int(item["pid"]) for item in rows
                    if int(item["pid"] or 0) > 0 and _name_key(item["name"]) == key
                }
                if len(pids) == 1:
                    target = pids.pop()
            if not target:
                cur = self.conn.execute(
                    "INSERT INTO persons(bot_id, name, created_at, updated_at) VALUES(?,?,?,?)",
                    (bot_id, display, _now(), _now()),
                )
                target = int(cur.lastrowid or 0)
            self.conn.execute(
                "INSERT INTO person_links(bot_id, platform, uid, pid, name, created_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(bot_id, platform, uid) DO UPDATE SET "
                "pid=excluded.pid, name=CASE WHEN excluded.name!='' THEN excluded.name ELSE person_links.name END",
                (bot_id, platform, uid, target, display, _now()),
            )
            self._backfill_messages(bot_id, platform, uid, target)
            self.conn.commit()
            return target

    def _backfill_messages(self, bot_id: str, platform: str, uid: str, pid: int) -> None:
        try:
            self.conn.execute(
                "UPDATE messages SET pid=?, platform=CASE WHEN platform='' THEN ? ELSE platform END "
                "WHERE bot_id=? AND uid=? AND (pid=0 OR pid IS NULL) "
                "AND (platform=? OR platform='')",
                (int(pid), str(platform), str(bot_id), str(uid), str(platform)),
            )
        except Exception:
            pass

    def link_identity(self, bot_id: str, platform: str, uid: str, pid: int = 0,
                      name: str = "") -> int:
        """手动把某个 (平台, uid) 绑到指定人物；pid=0 表示新建人物。"""
        bot_id, platform, uid = str(bot_id), str(platform or ""), str(uid)
        if not uid:
            return 0
        with self.lock:
            pid = int(pid or 0)
            if pid <= 0:
                cur = self.conn.execute(
                    "INSERT INTO persons(bot_id, name, created_at, updated_at) VALUES(?,?,?,?)",
                    (bot_id, _clean(name, 64), _now(), _now()),
                )
                pid = int(cur.lastrowid or 0)
            row = self.conn.execute(
                "SELECT pid FROM person_links WHERE bot_id=? AND platform=? AND uid=?",
                (bot_id, platform, uid),
            ).fetchone()
            if row and int(row["pid"] or 0) and int(row["pid"]) != pid:
                left = self.conn.execute(
                    "SELECT COUNT(*) AS c FROM person_links WHERE bot_id=? AND pid=?",
                    (bot_id, int(row["pid"])),
                ).fetchone()
                old_pid = int(row["pid"])
                if left is not None and int(left["c"] or 0) <= 1:
                    self.conn.execute("DELETE FROM persons WHERE pid=?", (old_pid,))
            self.conn.execute(
                "INSERT INTO person_links(bot_id, platform, uid, pid, name, created_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(bot_id, platform, uid) DO UPDATE SET "
                "pid=excluded.pid, name=CASE WHEN excluded.name!='' THEN excluded.name ELSE person_links.name END",
                (bot_id, platform, uid, pid, _clean(name, 64), _now()),
            )
            self._backfill_messages(bot_id, platform, uid, pid)
            self.conn.commit()
            return pid

    def unlink_identity(self, bot_id: str, platform: str, uid: str) -> bool:
        """解绑：这个身份单独变成一个人物，不删聊天记录。"""
        with self.lock:
            row = self.conn.execute(
                "SELECT pid FROM person_links WHERE bot_id=? AND platform=? AND uid=?",
                (str(bot_id), str(platform or ""), str(uid)),
            ).fetchone()
            if not row:
                return False
            old_pid = int(row["pid"] or 0)
            cur = self.conn.execute(
                "INSERT INTO persons(bot_id, name, created_at, updated_at) VALUES(?,?,?,?)",
                (str(bot_id), "", _now(), _now()),
            )
            pid = int(cur.lastrowid or 0)
            self.conn.execute(
                "UPDATE person_links SET pid=? WHERE bot_id=? AND platform=? AND uid=?",
                (pid, str(bot_id), str(platform or ""), str(uid)),
            )
            self.conn.execute(
                "UPDATE messages SET pid=? WHERE bot_id=? AND pid=? AND platform=? AND uid=?",
                (pid, str(bot_id), old_pid, str(platform or ""), str(uid)),
            )
            if old_pid:
                left = self.conn.execute(
                    "SELECT COUNT(*) AS c FROM person_links WHERE bot_id=? AND pid=?",
                    (str(bot_id), old_pid),
                ).fetchone()
                if left is not None and int(left["c"] or 0) == 0:
                    self.conn.execute("DELETE FROM persons WHERE pid=?", (old_pid,))
            self.conn.commit()
            return True

    def merge_persons(self, bot_id: str, keep_pid: int, drop_pid: int) -> bool:
        """把 drop 合并进 keep：身份、聊天记录、记忆都并过去，然后删掉 drop。"""
        bot_id = str(bot_id)
        keep_pid, drop_pid = int(keep_pid or 0), int(drop_pid or 0)
        if not keep_pid or not drop_pid or keep_pid == drop_pid:
            return False
        with self.lock:
            exists = self.conn.execute(
                "SELECT COUNT(*) AS c FROM persons WHERE bot_id=? AND pid IN (?,?)",
                (bot_id, keep_pid, drop_pid),
            ).fetchone()
            if not exists or int(exists["c"] or 0) < 2:
                return False
            for table, column in (("person_links", "pid"), ("messages", "pid")):
                try:
                    self.conn.execute(
                        "UPDATE %s SET %s=? WHERE bot_id=? AND %s=?" % (table, column, column),
                        (keep_pid, bot_id, drop_pid),
                    )
                except Exception:
                    continue
            # 记忆是按 uid 存的，合并时并到保留人物的第一个 uid 上
            keep_links = self.person_links(bot_id, keep_pid)
            drop_links = self.person_links(bot_id, drop_pid)
            keep_uid = keep_links[0]["uid"] if keep_links else ""
            if keep_uid:
                for item in drop_links:
                    try:
                        self.conn.execute(
                            "UPDATE OR IGNORE memories SET uid=? WHERE bot_id=? AND uid=?",
                            (keep_uid, bot_id, item["uid"]),
                        )
                    except Exception:
                        continue
            self.conn.execute("DELETE FROM persons WHERE bot_id=? AND pid=?", (bot_id, drop_pid))
            self.conn.execute(
                "UPDATE persons SET updated_at=? WHERE pid=?", (_now(), keep_pid)
            )
            self.conn.commit()
            return True

    def person_links(self, bot_id: str, pid: int) -> List[Dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT platform, uid, name, created_at FROM person_links "
                "WHERE bot_id=? AND pid=? ORDER BY created_at",
                (str(bot_id), int(pid)),
            ).fetchall()
        return [
            {
                "platform": str(row["platform"] or ""),
                "label": platform_label(str(row["platform"] or "")),
                "uid": str(row["uid"] or ""),
                "name": str(row["name"] or ""),
                "created_at": int(row["created_at"] or 0),
            }
            for row in rows
        ]

    def list_persons(self, bot_id: str = "", keyword: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        where, params = _bot_filter(bot_id)
        sql = "SELECT pid, bot_id, name, updated_at FROM persons WHERE 1=1" + where
        if keyword:
            like = "%" + str(keyword) + "%"
            sql += (" AND (name LIKE ? OR pid IN (SELECT pid FROM person_links "
                    "WHERE bot_id=persons.bot_id AND (uid LIKE ? OR name LIKE ?)))")
            params.extend([like, like, like])
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
            out: List[Dict[str, Any]] = []
            for row in rows:
                pid = int(row["pid"] or 0)
                bot = str(row["bot_id"] or "")
                links = self.person_links(bot, pid)
                stats = self.conn.execute(
                    "SELECT COUNT(*) AS c, MAX(created_at) AS last FROM messages "
                    "WHERE bot_id=? AND pid=?",
                    (bot, pid),
                ).fetchone()
                out.append({
                    "pid": pid,
                    "bot_id": bot,
                    "name": str(row["name"] or "") or next(
                        (item["name"] for item in links if item["name"]), ""
                    ),
                    "links": links,
                    "platforms": [item["label"] for item in links],
                    "msg_count": int(stats["c"] or 0) if stats else 0,
                    "last_at": int(stats["last"] or 0) if stats else 0,
                    "updated_at": int(row["updated_at"] or 0),
                })
        return out

    def person_detail(self, bot_id: str, pid: int) -> Dict[str, Any]:
        with self.lock:
            row = self.conn.execute(
                "SELECT pid, bot_id, name, created_at, updated_at FROM persons WHERE pid=?",
                (int(pid),),
            ).fetchone()
            if not row:
                return {}
            bot = str(row["bot_id"] or "")
            links = self.person_links(bot, int(pid))
            stats = self.conn.execute(
                "SELECT COUNT(*) AS c, MAX(created_at) AS last FROM messages WHERE bot_id=? AND pid=?",
                (bot, int(pid)),
            ).fetchone()
            profiles = []
            for item in links:
                profile = self.get_profile(bot, item["uid"])
                if profile.get("exists"):
                    profiles.append(profile)
            rows = self.conn.execute(
                "SELECT platform, uid, name, role, content, created_at, compressed FROM messages "
                "WHERE bot_id=? AND pid=? ORDER BY created_at DESC LIMIT 60",
                (bot, int(pid)),
            ).fetchall()
            recent = []
            for item in rows:
                info = dict(item)
                info["label"] = platform_label(str(info.get("platform") or ""))
                recent.append(info)
        return {
            "pid": int(row["pid"]),
            "bot_id": bot,
            "name": str(row["name"] or ""),
            "links": links,
            "platforms": [item["label"] for item in links],
            "profiles": profiles,
            "recent": recent,
            "msg_count": int(stats["c"] or 0) if stats else 0,
            "last_at": int(stats["last"] or 0) if stats else 0,
            "created_at": int(row["created_at"] or 0),
            "updated_at": int(row["updated_at"] or 0),
        }

    def person_of(self, bot_id: str, platform: str, uid: str) -> Dict[str, Any]:
        with self.lock:
            row = self.conn.execute(
                "SELECT pid FROM person_links WHERE bot_id=? AND platform=? AND uid=?",
                (str(bot_id), str(platform or ""), str(uid)),
            ).fetchone()
        if not row:
            return {}
        return self.person_detail(str(bot_id), int(row["pid"] or 0))

    def person_context(
        self, bot_id: str, platform: str, uid: str, limit: int = 8
    ) -> Dict[str, Any]:
        """给 Amo 用：这个人在别的平台说过什么，一起带回来。"""
        detail = self.person_of(bot_id, platform, uid)
        if not detail:
            return {}
        pairs = [(item["platform"], item["uid"]) for item in detail.get("links", [])]
        others = [item for item in pairs if not (item[0] == str(platform) and item[1] == str(uid))]
        recent: List[Dict[str, Any]] = []
        with self.lock:
            rows = self.conn.execute(
                "SELECT platform, uid, name, role, content, created_at, compressed FROM messages "
                "WHERE bot_id=? AND pid=? ORDER BY created_at DESC LIMIT ?",
                (str(bot_id), int(detail["pid"]), max(1, min(int(limit), 50))),
            ).fetchall()
            recent = [
                {
                    "platform": str(row["platform"] or ""),
                    "label": platform_label(str(row["platform"] or "")),
                    "uid": str(row["uid"] or ""),
                    "name": str(row["name"] or ""),
                    "role": str(row["role"] or "user"),
                    "content": str(row["content"] or ""),
                    "created_at": int(row["created_at"] or 0),
                    "compressed": int(row["compressed"] or 0),
                }
                for row in rows
            ]
        memories: List[Dict[str, Any]] = []
        try:
            memories = self.list_memories(str(bot_id), uid=str(uid), limit=limit) if hasattr(self, "list_memories") else []
        except Exception:
            memories = []
        detail["others"] = [
            {"platform": item[0], "label": platform_label(item[0]), "uid": item[1]}
            for item in others
        ]
        detail["recent"] = recent
        detail["memories"] = memories
        return detail

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
        where, params = _bot_filter(bot_id)
        sql = (
            "SELECT id, uid, content, kind, importance, created_at, hit_count, scope, group_id "
            "FROM memories WHERE 1=1" + where
        )
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

    # ---------- 聊天记录 ----------
    def add_message(self, bot_id: str, umo: str, uid: str = "", name: str = "",
                    role: str = "user", content: str = "", platform: str = "") -> int:
        text = _clean(content, 4000)
        if not text:
            return 0
        platform = str(platform or "").strip() or platform_of_umo(umo)
        pid = 0
        if str(role) != "bot" and uid:
            try:
                pid = self.resolve_person(bot_id, platform, uid, name)
            except Exception:
                pid = 0
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO messages(bot_id, umo, uid, name, role, content, created_at, platform, pid) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (str(bot_id), str(umo), str(uid), _clean(name, 64),
                 "bot" if str(role) == "bot" else "user", text, _now(), platform, pid),
            )
            self.conn.commit()
            return int(cur.lastrowid or 0)

    def list_messages(self, bot_id: str, umo: str = "", limit: int = 200,
                      pid: int = 0, platform: str = "", uid: str = "") -> List[Dict[str, Any]]:
        where, params = _bot_filter(bot_id)
        sql = ("SELECT id, umo, uid, name, role, content, created_at, compressed, "
               "platform, pid FROM messages WHERE 1=1" + where)
        if umo:
            sql += " AND umo=?"
            params.append(str(umo))
        if pid:
            sql += " AND pid=?"
            params.append(int(pid))
        if platform:
            sql += " AND platform=?"
            params.append(str(platform))
        if uid:
            sql += " AND uid=?"
            params.append(str(uid))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["label"] = platform_label(str(item.get("platform") or ""))
            out.append(item)
        return out

    def delete_message(self, bot_id: str, message_id: int) -> bool:
        with self.lock:
            cur = self.conn.execute(
                "DELETE FROM messages WHERE bot_id=? AND id=?",
                (str(bot_id), int(message_id)),
            )
            self.conn.commit()
            return bool(cur.rowcount)

    def clear_messages(self, bot_id: str, umo: str = "") -> int:
        sql = "DELETE FROM messages WHERE bot_id=?"
        params: List[Any] = [str(bot_id)]
        if umo:
            sql += " AND umo=?"
            params.append(str(umo))
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return max(0, cur.rowcount or 0)

    def purge_messages(self, bot_id: str, days: int = 14) -> int:
        """超过保留期的原始聊天记录直接删掉。"""
        cutoff = _now() - max(1, int(days)) * 86400
        with self.lock:
            cur = self.conn.execute(
                "DELETE FROM messages WHERE bot_id=? AND created_at<? AND compressed=0",
                (str(bot_id), cutoff),
            )
            self.conn.commit()
            return max(0, cur.rowcount or 0)

    def compress_messages(self, bot_id: str, after_days: int = 3, keep_days: int = 14) -> Dict[str, int]:
        """把 N 天前的原始聊天按「会话 + 天」压成一条摘要，超过保留期连摘要一起删。"""
        now = _now()
        cut = now - max(1, int(after_days)) * 86400
        expire = now - max(1, int(keep_days)) * 86400
        counts = {"compressed": 0, "purged": 0}
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, umo, uid, name, role, content, created_at FROM messages "
                "WHERE bot_id=? AND created_at<? AND compressed=0 ORDER BY created_at",
                (str(bot_id), cut),
            ).fetchall()
            buckets: Dict[Any, List[sqlite3.Row]] = {}
            for row in rows:
                day = time.strftime("%Y-%m-%d", time.localtime(int(row["created_at"])))
                buckets.setdefault((str(row["umo"]), day), []).append(row)
            for (umo, day), items in buckets.items():
                speakers = []
                for item in items:
                    label = "她" if str(item["role"]) == "bot" else str(item["name"] or item["uid"] or "对方")
                    if label not in speakers:
                        speakers.append(label)
                body = " / ".join(
                    ("她：" if str(item["role"]) == "bot" else str(item["name"] or "对方") + "：")
                    + _clean(item["content"], 120)
                    for item in items[:40]
                )
                summary = _clean(
                    "[%s 共 %d 条 · %s] %s" % (day, len(items), "、".join(speakers), body), 2000
                )
                stamp = int(items[0]["created_at"])
                self.conn.execute(
                    "INSERT INTO messages(bot_id, umo, uid, name, role, content, created_at, "
                    "compressed, platform, pid) VALUES(?,?,?,?,?,?,?,1,?,?)",
                    (str(bot_id), umo, "", "摘要", "summary", summary, stamp,
                     platform_of_umo(umo), int(items[0]["pid"] or 0)
                     if "pid" in items[0].keys() else 0),
                )
                ids = [int(item["id"]) for item in items]
                self.conn.executemany(
                    "DELETE FROM messages WHERE bot_id=? AND id=?",
                    [(str(bot_id), item_id) for item_id in ids],
                )
                counts["compressed"] += 1
            cur = self.conn.execute(
                "DELETE FROM messages WHERE bot_id=? AND created_at<?",
                (str(bot_id), expire),
            )
            counts["purged"] = max(0, cur.rowcount or 0)
            self.conn.commit()
        return counts

    # ---------- 运行日志 ----------
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

    # ---------- 关系网 ----------

    def list_relations(self, bot_id: str, uid: str = "") -> List[Dict[str, Any]]:
        where, params = _bot_filter(bot_id)
        sql = "SELECT a_uid, b_uid, relation, note, updated_at FROM relations WHERE 1=1" + where
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

    def stats(self, bot_id: str = "") -> Dict[str, int]:
        where, params = _bot_filter(bot_id)
        with self.lock:
            out = {}
            for name, table in (
                ("profiles", "profiles"), ("memories", "memories"),
                ("relations", "relations"), ("cache", "cache"),
                ("messages", "messages"), ("logs", "logs"), ("persons", "persons"),
            ):
                if where:
                    row = self.conn.execute(
                        "SELECT COUNT(*) AS c FROM %s WHERE 1=1%s" % (table, where), params
                    ).fetchone()
                else:
                    row = self.conn.execute("SELECT COUNT(*) AS c FROM %s" % table).fetchone()
                out[name] = int(row["c"] or 0) if row else 0
        return out
