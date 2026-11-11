# -*- coding: utf-8 -*-
"""建表 / 迁移 / 备份 / 快照 / 统计。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from typing import Any
from typing import Dict
from typing import List
import os
import sqlite3

if TYPE_CHECKING:  # 只给类型检查看：包内相对导入
    from .store_util import (
        _bot_filter,
        _clock,
        _now,
        logger,
    )
else:  # 运行时：包模式优先，按文件直接加载时退回绝对导入
    try:
        from .store_util import (
            _bot_filter,
            _clock,
            _now,
            logger,
        )
    except ImportError:  # 直接按文件加载时
        from store_util import (
            _bot_filter,
            _clock,
            _now,
            logger,
        )


if TYPE_CHECKING:
    from .store_types import StoreLike as _StoreBase
else:  # 运行时用 object：协议里的声明不能进 MRO，否则会挡掉别的 mixin 的真实实现
    _StoreBase = object


class SchemaMixin(_StoreBase):
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
                    idem_key TEXT DEFAULT '',
                    status TEXT DEFAULT 'active',
                    superseded_by INTEGER DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_mem_lookup ON memories(bot_id, uid, scope, group_id);
                CREATE INDEX IF NOT EXISTS idx_mem_hit ON memories(bot_id, uid, last_hit_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_mem_idem
                    ON memories(bot_id, uid, scope, group_id, idem_key)
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

                CREATE TABLE IF NOT EXISTS memory_vec (
                    memory_id INTEGER PRIMARY KEY,
                    model TEXT DEFAULT '',
                    dim INTEGER DEFAULT 0,
                    vec BLOB,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id TEXT NOT NULL,
                    umo TEXT NOT NULL,
                    uid TEXT DEFAULT '',
                    name TEXT DEFAULT '',
                    role TEXT DEFAULT 'user',
                    content TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    compressed INTEGER DEFAULT 0,
                    message_id TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_msg_scan ON messages(bot_id, umo, created_at);
                -- idx_msg_mid 在 _migrate_columns 里建：老库还没有 message_id 列

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
                CREATE TABLE IF NOT EXISTS identity_alias (
                    bot_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    uid TEXT NOT NULL,
                    alias_platform TEXT NOT NULL,
                    alias_uid TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY (bot_id, platform, uid)
                );
                """
            )
            self._migrate_columns()
            conn.commit()
        self._init_trigram_fts()
    def _init_trigram_fts(self) -> None:
        """建一个 trigram FTS5 表做子串粗筛；SQLite 不支持时自动停用。

        memories_fts（unicode61）对中文没用——整段中文会被当成一个 token，
        所以之前虽然建了表却从没被查询过。trigram 才能按「>=3 个字」做子串匹配，
        用来替代大表上的 `content LIKE '%查询%'` 全表扫；2 个字以下的查询仍走 LIKE。
        """
        self._fts_tri = False
        if not self.setting("recall_fts_enabled", False):
            return
        try:
            with self.lock:
                conn = self.conn
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS memories_ftst USING fts5("
                    "content, content='memories', content_rowid='id', tokenize='trigram')"
                )
                conn.executescript(
                    """
                    CREATE TRIGGER IF NOT EXISTS memt_ai AFTER INSERT ON memories BEGIN
                        INSERT INTO memories_ftst(rowid, content) VALUES (new.id, new.content);
                    END;
                    CREATE TRIGGER IF NOT EXISTS memt_ad AFTER DELETE ON memories BEGIN
                        DELETE FROM memories_ftst WHERE rowid = old.id;
                    END;
                    CREATE TRIGGER IF NOT EXISTS memt_au AFTER UPDATE OF content ON memories BEGIN
                        INSERT INTO memories_ftst(memories_ftst, rowid, content)
                            VALUES('delete', old.id, old.content);
                        INSERT INTO memories_ftst(rowid, content) VALUES (new.id, new.content);
                    END;
                    CREATE TABLE IF NOT EXISTS store_meta(
                        key TEXT PRIMARY KEY, value TEXT DEFAULT ''
                    );
                    """
                )
                # 影子表的行数不等于「建了索引的文档数」（trigram 表有固定头部行），
                # 所以用 store_meta 记「上次 rebuild 时 memories 有多少条」来判断是否要重建。
                total = int(conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"])
                row = conn.execute(
                    "SELECT value FROM store_meta WHERE key='fts_tri_rows'"
                ).fetchone()
                try:
                    built = int(row["value"]) if row else -1
                except (TypeError, ValueError):
                    built = -1
                rebuilt = built != total
                if rebuilt:
                    conn.execute("INSERT INTO memories_ftst(memories_ftst) VALUES('rebuild')")
                    conn.execute(
                        "INSERT INTO store_meta(key, value) VALUES('fts_tri_rows', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(total),),
                    )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_mem_recall ON memories("
                    "bot_id, uid, status, last_hit_at)"
                )
                conn.commit()
                self._fts_tri = True
                if rebuilt:
                    logger.info("[amo_data] trigram 索引已重建：%d 条记忆" % total)
        except Exception as exc:
            self._fts_tri = False
            logger.warning("[amo_data] trigram 索引不可用，召回退回 LIKE 全表扫: %s" % exc)
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
                ("message_id", "ALTER TABLE messages ADD COLUMN message_id TEXT DEFAULT ''"),
            ):
                if column in cols:
                    continue
                try:
                    conn.execute(ddl)
                except Exception as exc:
                    logger.warning("[amo_data] 补列 %s 失败: %s" % (column, exc))
                    continue
            try:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_pid ON messages(bot_id, pid, created_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_mid ON messages(bot_id, message_id)")
            except Exception:
                pass
            # 记忆表补列：近重复合并计数（老库自动补，不需要重建表）
            try:
                mcols = {str(row["name"]) for row in conn.execute("PRAGMA table_info(memories)")}
            except Exception:
                mcols = set()
            for column, ddl in (
                ("merged_count", "ALTER TABLE memories ADD COLUMN merged_count INTEGER DEFAULT 1"),
                ("status", "ALTER TABLE memories ADD COLUMN status TEXT DEFAULT 'active'"),
                ("superseded_by", "ALTER TABLE memories ADD COLUMN superseded_by INTEGER DEFAULT 0"),
            ):
                if column in mcols:
                    continue
                try:
                    conn.execute(ddl)
                except Exception:
                    pass
            # 去重索引也要跟着 scope 走：老库是 (bot_id, uid, idem_key)，同一句话在群里
            # 和私聊各说一次就会撞 UNIQUE（私聊那条写不进去，或反过来），而代码里
            # 的去重查询已经按 (scope, group_id) 分流了，索引必须一致。
            try:
                row = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_mem_idem'"
                ).fetchone()
                sql = str(row["sql"] or "") if row else ""
                if row is None or "scope" not in sql:
                    conn.execute("DROP INDEX IF EXISTS idx_mem_idem")
                    conn.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_mem_idem "
                        "ON memories(bot_id, uid, scope, group_id, idem_key) "
                        "WHERE idem_key != ''"
                    )
                    logger.info("[amo_data] 记忆去重索引已升级为按会话隔离")
                conn.commit()
            except Exception as exc:
                # 建不出来（例如库里有遗留的重复 idem_key）也不影响用：
                # remember 里的查询照样按 (scope, group_id) 兜着
                logger.warning("[amo_data] 记忆去重索引升级失败: %s" % exc)
    def _snapshot_from_conn(self, conn, bot_id: str = "") -> Dict[str, Any]:
        where, params = _bot_filter(bot_id)
        out: Dict[str, Any] = {
            "format": "astr_amo_data", "version": 1,
            "exported_at": _now(), "bot_id": str(bot_id or ""),
        }
        for table in self._EXPORT_TABLES:
            try:
                rows = conn.execute(
                    "SELECT * FROM %s WHERE 1=1%s" % (table, where), params
                ).fetchall()
            except Exception:
                rows = []
            out[table] = [dict(row) for row in rows]
        return out
    def export_snapshot(self, bot_id: str = "") -> Dict[str, Any]:
        """导出画像 / 记忆 / 关系 / 人物 / 身份别名（不含聊天记录）。"""
        with self.lock:
            snapshot = self._snapshot_from_conn(self.conn, bot_id)
        snapshot["counts"] = {k: len(snapshot[k]) for k in self._EXPORT_TABLES}
        return snapshot
    def load_snapshot_file(self, db_path: str, bot_id: str = "") -> Dict[str, Any]:
        """从一个备份数据库文件导出同样的快照。"""
        path = str(db_path)
        if not os.path.isfile(path):
            return {}
        conn = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
        try:
            conn.row_factory = sqlite3.Row
            snapshot = self._snapshot_from_conn(conn, bot_id)
        finally:
            conn.close()
        snapshot["counts"] = {k: len(snapshot.get(k) or []) for k in self._EXPORT_TABLES}
        return snapshot
    def import_snapshot(self, data: Dict[str, Any]) -> Dict[str, int]:
        """把快照合并进当前库：记忆/画像/关系按内容去重，人物按身份别名合并。"""
        if not isinstance(data, dict):
            return {"memories": 0, "profiles": 0, "relations": 0, "links": 0, "aliases": 0}
        counts = {"memories": 0, "profiles": 0, "relations": 0, "links": 0,
                  "aliases": 0, "skipped": 0, "failed": 0}
        default_bot = str(data.get("bot_id") or "") or ""
        for item in data.get("memories") or []:
            try:
                bot = str(item.get("bot_id") or default_bot)
                uid = str(item.get("uid") or "")
                content = str(item.get("content") or "")
                if not bot or not uid or not content:
                    continue
                res = self.remember(
                    bot, uid, content,
                    kind=str(item.get("kind") or "fact"),
                    importance=item.get("importance", 0.5),
                    scope=str(item.get("scope") or "private"),
                    group_id=str(item.get("group_id") or ""),
                    idem_key=str(item.get("idem_key") or ""),
                    created_at=item.get("created_at"),
                    last_hit_at=item.get("last_hit_at"),
                    hit_count=item.get("hit_count", 0),
                    keep_existing_content=True,
                )
                if not res.get("ok"):
                    continue
                if res.get("duplicate"):
                    # 已经存在（内容指纹相同）就跳过，重复导入不会算成新增
                    counts["skipped"] += 1
                    continue
                counts["memories"] += 1
                mid = int(res.get("memory_id") or 0)
                status = str(item.get("status") or "")
                if mid and status in ("superseded", "archived", "merged"):
                    with self.lock:
                        self.conn.execute(
                            "UPDATE memories SET status=?, superseded_by=? WHERE id=?",
                            (status, int(item.get("superseded_by") or 0), mid),
                        )
                        self.conn.commit()
            except Exception as exc:
                # 以前这里静默 continue：二次导入时 74 条无声消失、计数还显示成功
                counts["failed"] += 1
                try:
                    logger.warning("[amo_data] 导入记忆失败: %s" % exc)
                except Exception:
                    pass
                continue
        for item in data.get("profiles") or []:
            try:
                bot = str(item.get("bot_id") or default_bot)
                uid = str(item.get("uid") or "")
                if not bot or not uid:
                    continue
                current = self.get_profile(bot, uid)
                try:
                    remote_at = int(item.get("updated_at") or 0)
                except (TypeError, ValueError):
                    remote_at = 0
                if current and remote_at and remote_at < int(current.get("updated_at") or 0):
                    continue
                self.save_profile(
                    bot, uid,
                    name=item.get("name"), alias=item.get("alias"),
                    relation=item.get("relation"), remark=item.get("remark"),
                )
                counts["profiles"] += 1
            except Exception:
                continue
        for item in data.get("relations") or []:
            try:
                if self.save_relation(
                    str(item.get("bot_id") or default_bot),
                    str(item.get("a_uid") or ""), str(item.get("b_uid") or ""),
                    str(item.get("relation") or "朋友"), str(item.get("note") or ""),
                ):
                    counts["relations"] += 1
            except Exception:
                continue
        for item in data.get("person_links") or []:
            try:
                bot = str(item.get("bot_id") or default_bot)
                platform, uid = str(item.get("platform") or ""), str(item.get("uid") or "")
                if not bot or not uid:
                    continue
                self.resolve_person(bot, platform, uid, str(item.get("name") or ""))
                counts["links"] += 1
            except Exception:
                continue
        for item in data.get("identity_alias") or []:
            try:
                if self.set_identity_alias(
                    str(item.get("bot_id") or default_bot),
                    str(item.get("platform") or ""), str(item.get("uid") or ""),
                    str(item.get("alias_platform") or ""), str(item.get("alias_uid") or ""),
                ):
                    counts["aliases"] += 1
            except Exception:
                continue
        return counts
    def backup_database(self, dest_dir: str, keep: int = 7) -> str:
        """把当前 SQLite 拷贝一份到 dest_dir，并只保留最近 keep 份。"""
        dest_dir = str(dest_dir)
        os.makedirs(dest_dir, exist_ok=True)
        stem = "amo_data-%s" % _clock.now().strftime("%Y%m%d-%H%M%S")
        name = stem + ".db"
        index = 1
        while os.path.exists(os.path.join(dest_dir, name)):
            name = "%s-%d.db" % (stem, index)
            index += 1
        dest = os.path.join(dest_dir, name)
        with self.lock:
            target = sqlite3.connect(dest)
            try:
                self.conn.backup(target)
            finally:
                target.close()
        self.prune_backups(dest_dir, keep)
        return dest
    def prune_backups(self, dest_dir: str, keep: int = 7) -> int:
        try:
            keep = max(1, int(keep))
        except (TypeError, ValueError):
            keep = 7
        try:
            names = [n for n in os.listdir(dest_dir)
                     if n.startswith("amo_data-") and n.endswith(".db")]
        except Exception:
            return 0
        files = []
        for name in names:
            path = os.path.join(dest_dir, name)
            try:
                files.append((os.path.getmtime(path), path))
            except Exception:
                continue
        files.sort(reverse=True)
        removed = 0
        for _, path in files[keep:]:
            try:
                os.remove(path)
                removed += 1
            except Exception:
                continue
        return removed
    def list_backups(self, dest_dir: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            names = os.listdir(dest_dir)
        except Exception:
            return out
        for name in names:
            if not (name.startswith("amo_data-") and name.endswith(".db")):
                continue
            path = os.path.join(dest_dir, name)
            try:
                st = os.stat(path)
            except Exception:
                continue
            out.append({"name": name, "size": int(st.st_size),
                        "mtime": int(st.st_mtime)})
        out.sort(key=lambda item: item["name"], reverse=True)
        return out
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
            try:
                for status in ("active", "superseded", "archived", "merged"):
                    row = self.conn.execute(
                        "SELECT COUNT(*) AS c FROM memories WHERE COALESCE(status,'active')=?"
                        + (" AND bot_id=?" if where else ""),
                        ([status] + (params if where else [])),
                    ).fetchone()
                    out["memories_" + status] = int(row["c"] or 0) if row else 0
            except Exception:
                pass
        return out
