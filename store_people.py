# -*- coding: utf-8 -*-
"""画像 / 身份归一 / 人物合并 / 人物上下文。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from typing import Any
from typing import Dict
from typing import List
from typing import Optional
import sqlite3

if TYPE_CHECKING:  # 只给类型检查看：包内相对导入
    from .store_util import (
        _bot_filter,
        _clean,
        _name_key,
        _now,
        _unique_labels,
        platform_label,
    )
else:  # 运行时：包模式优先，按文件直接加载时退回绝对导入
    try:
        from .store_util import (
            _bot_filter,
            _clean,
            _name_key,
            _now,
            _unique_labels,
            platform_label,
        )
    except ImportError:  # 直接按文件加载时
        from store_util import (
            _bot_filter,
            _clean,
            _name_key,
            _now,
            _unique_labels,
            platform_label,
        )


if TYPE_CHECKING:
    from .store_types import StoreLike as _StoreBase
else:  # 运行时用 object：协议里的声明不能进 MRO，否则会挡掉别的 mixin 的真实实现
    _StoreBase = object


class PeopleMixin(_StoreBase):
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
            # 画像改名后，人物卡的名字也要跟着改：否则会一直显示最早建档时的旧名字
            if merged["name"]:
                self.conn.execute(
                    "UPDATE persons SET name=? WHERE bot_id=? AND pid IN "
                    "(SELECT pid FROM person_links WHERE bot_id=? AND uid=?)",
                    (merged["name"], bot_id, bot_id, uid),
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
        platform, uid = self._alias_of(bot_id, platform, uid)
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
            target = 0
            # 同一个 uid 出现在别的平台（导入的飞书记录里 subject_id 还是 QQ 号）：
            # 并到已有那个人，不另起一个「他」。
            same = self.conn.execute(
                "SELECT pid FROM person_links WHERE bot_id=? AND uid=? AND pid>0 LIMIT 1",
                (bot_id, uid),
            ).fetchone()
            if same and int(same["pid"] or 0):
                target = int(same["pid"])
            key = _name_key(display)
            if not target and auto_merge and len(key) >= 2:
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
                    rows = self.conn.execute(
                        "SELECT id, idem_key FROM memories WHERE bot_id=? AND uid=?",
                        (bot_id, item["uid"]),
                    ).fetchall()
                    for mem in rows:
                        try:
                            self.conn.execute(
                                "UPDATE memories SET uid=? WHERE bot_id=? AND id=?",
                                (keep_uid, bot_id, int(mem["id"])),
                            )
                        except sqlite3.IntegrityError:
                            # 两边有同一条记忆：把旧的标 merged 挂到保留行，别让它变成孤儿
                            conflict = self.conn.execute(
                                "SELECT id FROM memories WHERE bot_id=? AND uid=? AND idem_key=?",
                                (bot_id, keep_uid, mem["idem_key"]),
                            ).fetchone()
                            self.conn.execute(
                                "UPDATE memories SET status='merged', superseded_by=? "
                                "WHERE bot_id=? AND id=?",
                                (int(conflict["id"]) if conflict else None, bot_id, int(mem["id"])),
                            )
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
                    "platforms": _unique_labels(links),
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
    def _alias_of(self, bot_id: str, platform: str, uid: str):
        """把一个 (平台, uid) 换成规范身份；没配别名就原样返回。"""
        platform, uid = str(platform or ""), str(uid)
        try:
            with self.lock:
                row = self.conn.execute(
                    "SELECT alias_platform, alias_uid FROM identity_alias "
                    "WHERE bot_id=? AND platform=? AND uid=?",
                    (str(bot_id), platform, uid),
                ).fetchone()
        except Exception:
            return (platform, uid)
        if row and str(row["alias_platform"] or "").strip():
            return (str(row["alias_platform"]), str(row["alias_uid"]))
        return (platform, uid)
    def set_identity_alias(self, bot_id: str, platform: str, uid: str,
                           alias_platform: str = "", alias_uid: str = "") -> bool:
        """登记「导出的旧身份」→「规范身份」，之后 resolve_person 会自动走别名。"""
        try:
            with self.lock:
                self.conn.execute(
                    "INSERT INTO identity_alias(bot_id, platform, uid, alias_platform, alias_uid, created_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(bot_id, platform, uid) DO UPDATE SET "
                    "alias_platform=excluded.alias_platform, alias_uid=excluded.alias_uid",
                    (str(bot_id), str(platform), str(uid), str(alias_platform),
                     str(alias_uid), _now()),
                )
                self.conn.commit()
            return True
        except Exception:
            return False
    def list_identity_aliases(self, bot_id: str = "") -> List[Dict[str, Any]]:
        where, params = _bot_filter(bot_id)
        try:
            with self.lock:
                rows = self.conn.execute(
                    "SELECT bot_id, platform, uid, alias_platform, alias_uid FROM identity_alias "
                    "WHERE 1=1" + where + " ORDER BY platform, uid", params,
                ).fetchall()
            return [dict(row) for row in rows]
        except Exception:
            return []
    def person_of(self, bot_id: str, platform: str, uid: str) -> Dict[str, Any]:
        platform, uid = self._alias_of(str(bot_id), str(platform or ""), str(uid))
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
        # 每个平台各取 recent_limit 条，再合并：只按全局时间取的话，
        # 聊得多的那个平台会把另一个平台挤没（跨平台上下文就空了）。
        per_platform = max(1, min(int(limit), 50))
        with self.lock:
            gathered: Dict[tuple, sqlite3.Row] = {}
            # 他的话：按 (平台, uid) 取，每个平台各 per_platform 条；
            # 她的话：role='bot' + 同一个 pid（跨平台都算），单独限量。
            # 混在一条 SQL 里时她的话会把他的近聊挤出 limit（跨平台块就只剩她的发言）。
            seen_platforms = set()
            for item in detail.get("links", []) or []:
                # 按 pid + 平台取，不按 uid：飞书消息可能用归一后的 QQ uid 落库，
                # 只按 open_id 查会漏掉最新几条，跨平台上下文就只剩旧消息。
                platform = str(item.get("platform") or "")
                if not platform or platform in seen_platforms:
                    continue
                seen_platforms.add(platform)
                rows = self.conn.execute(
                    "SELECT platform, uid, name, role, content, created_at, compressed FROM messages "
                    "WHERE bot_id=? AND pid=? AND platform=? AND role!='bot' "
                    "ORDER BY created_at DESC LIMIT ?",
                    (str(bot_id), int(detail["pid"]), platform, per_platform),
                ).fetchall()
                for row in rows:
                    gathered[(row["platform"], row["role"], row["created_at"], row["content"])] = row
            bot_rows = self.conn.execute(
                "SELECT platform, uid, name, role, content, created_at, compressed FROM messages "
                "WHERE bot_id=? AND pid=? AND role='bot' "
                "ORDER BY created_at DESC LIMIT ?",
                (str(bot_id), int(detail["pid"]), per_platform),
            ).fetchall()
            for row in bot_rows:
                gathered[(row["platform"], row["role"], row["created_at"], row["content"])] = row
            rows = sorted(gathered.values(), key=lambda row: int(row["created_at"] or 0), reverse=True)[: per_platform * 4]
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
            # 记忆按**主 uid** 存的（飞书 open_id 会被 amo 归一成 QQ uid），
            # 只按当前平台 uid 查，在飞书那头永远是空。把这个人所有身份都查一遍。
            uid_candidates = [str(uid)] + [str(item.get("uid") or "") for item in detail.get("links", [])]
            seen_mem = set()
            seen_uids = set()
            for one_uid in uid_candidates:
                if not one_uid or one_uid in seen_uids:
                    continue
                seen_uids.add(one_uid)
                for row in self.list_memories(str(bot_id), uid=one_uid, limit=limit) or []:
                    key = (str(row.get("content") or "")[:80], row.get("created_at"))
                    if key in seen_mem:
                        continue
                    seen_mem.add(key)
                    memories.append(row)
            memories = memories[: limit]
        except Exception:
            memories = []
        detail["others"] = [
            {"platform": item[0], "label": platform_label(item[0]), "uid": item[1]}
            for item in others
        ]
        detail["recent"] = recent
        detail["memories"] = memories
        return detail
