# -*- coding: utf-8 -*-
"""记忆写入 / 去重 / 取代 / 压缩 / 召回 / 向量。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from typing import Any
from typing import Dict
from typing import List
from typing import Optional
import json
import math
import queue
import re
import sqlite3
import threading
import time
import urllib.request

if TYPE_CHECKING:  # 只给类型检查看：包内相对导入
    from .store_util import (
        RECALL_HALF_LIFE_DAYS,
        RECALL_IMPORTANCE_DECAY,
        RECALL_POOL,
        RECALL_W_FRESH,
        RECALL_W_IMPORTANCE,
        RECALL_W_REL,
        STOP_BIGRAMS,
        _bm25_scores,
        _bot_filter,
        _clean,
        _cosine,
        _freshness,
        _from_vec,
        _informative_terms,
        _jaccard,
        _norm_key,
        _now,
        _parse_time_span,
        _query_chars,
        _recall_tokens,
        _slot_facts,
        _to_vec,
        logger,
    )
else:  # 运行时：包模式优先，按文件直接加载时退回绝对导入
    try:
        from .store_util import (
            RECALL_HALF_LIFE_DAYS,
            RECALL_IMPORTANCE_DECAY,
            RECALL_POOL,
            RECALL_W_FRESH,
            RECALL_W_IMPORTANCE,
            RECALL_W_REL,
            STOP_BIGRAMS,
            _bm25_scores,
            _bot_filter,
            _clean,
            _cosine,
            _freshness,
            _from_vec,
            _informative_terms,
            _jaccard,
            _norm_key,
            _now,
            _parse_time_span,
            _query_chars,
            _recall_tokens,
            _slot_facts,
            _to_vec,
            logger,
        )
    except ImportError:  # 直接按文件加载时
        from store_util import (
            RECALL_HALF_LIFE_DAYS,
            RECALL_IMPORTANCE_DECAY,
            RECALL_POOL,
            RECALL_W_FRESH,
            RECALL_W_IMPORTANCE,
            RECALL_W_REL,
            STOP_BIGRAMS,
            _bm25_scores,
            _bot_filter,
            _clean,
            _cosine,
            _freshness,
            _from_vec,
            _informative_terms,
            _jaccard,
            _norm_key,
            _now,
            _parse_time_span,
            _query_chars,
            _recall_tokens,
            _slot_facts,
            _to_vec,
            logger,
        )


# 句子边界：中文句号/问号/叹号/分号/换行；用 lookbehind 保留分隔符，拼回去还是原文
_SENTENCE_RE = re.compile(r"(?<=[。！？!?；;\n])")

# 「你还记得我吗」这类没有实词的元问题：关键词闸门会一条都不给，
# 这里单独兜底。只匹配"整句就是这些套话"的说法，避免把「你记得我生日吗」也兜进来。
_META_PATTERNS = (
    "记得我吗", "记得我么", "还记得我吗", "记得我是谁", "记得我是谁吗",
    "记得什么", "记得啥", "记得哪些", "还记得什么", "都记得什么",
    "知道我是谁", "了解我吗", "了解我多少",
    "记得我说过什么", "我说过什么", "还记得我说过", "记得我的事",
    "记得我们", "还记得我们",
)


def _is_meta_query(text: Any) -> bool:
    body = re.sub(r"\s+", "", str(text or ""))
    if not body:
        return False
    return any(word in body for word in _META_PATTERNS)


def _split_long_text(text: Any, limit: int) -> List[str]:
    """按句子边界把长文本切成 <= limit 的块；单句超长时硬切。拼回去不丢字。"""
    body = str(text or "")
    limit = int(limit or 0)
    if limit <= 0 or len(body) <= limit:
        return [body] if body else []
    pieces: List[str] = []
    buf = ""
    for sentence in _SENTENCE_RE.split(body):
        while len(sentence) > limit:
            if buf:
                pieces.append(buf)
                buf = ""
            pieces.append(sentence[:limit])
            sentence = sentence[limit:]
        if buf and len(buf) + len(sentence) > limit:
            pieces.append(buf)
            buf = ""
        buf += sentence
    if buf:
        pieces.append(buf)
    return [piece for piece in pieces if piece]


if TYPE_CHECKING:
    from .store_types import StoreLike as _StoreBase
else:  # 运行时用 object：协议里的声明不能进 MRO，否则会挡掉别的 mixin 的真实实现
    _StoreBase = object


class MemoryMixin(_StoreBase):
    _emb_thread: Optional[threading.Thread]
    _emb_down_until: float

    def _ensure_emb_worker(self) -> None:
        if self._emb_thread is not None and self._emb_thread.is_alive():
            return
        self._emb_thread = threading.Thread(
            target=self._emb_worker_loop, name="amo-data-embed", daemon=True
        )
        self._emb_thread.start()
    def _emb_worker_loop(self) -> None:
        while True:
            try:
                item = self._emb_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                return
            memory_id, text = item
            try:
                vec = self._embed(text)
                if vec:
                    self._store_vec(memory_id, vec)
                else:
                    self._emb_failed += 1
            except Exception:
                self._emb_failed += 1
            finally:
                with self._emb_pending_lock:
                    self._emb_pending.discard(int(memory_id))
    def _enqueue_embedding(self, memory_id: int, text: str) -> None:
        if not self.setting("embedding_enabled") or not memory_id or not text:
            return
        self._ensure_emb_worker()
        mid = int(memory_id)
        with self._emb_pending_lock:
            if mid in self._emb_pending:
                return
            self._emb_pending.add(mid)
        try:
            self._emb_queue.put_nowait((mid, str(text)))
        except queue.Full:
            with self._emb_pending_lock:
                self._emb_pending.discard(mid)
    def _embed(self, text: str) -> List[float]:
        """调 embedding 接口拿向量；失败后 60 秒不再试，避免每条消息都卡超时。"""
        if not self.setting("embedding_enabled"):
            return []
        if time.time() < self._emb_down_until:
            return []
        base = str(self.setting("embedding_base", "") or "").rstrip("/")
        model = str(self.setting("embedding_model", "") or "")
        if not base or not model:
            return []
        url = base if base.endswith("/embeddings") else base + "/v1/embeddings"
        payload = json.dumps(
            {"model": model, "input": str(text or "")[:2000]}
        ).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            timeout = float(self.setting("embedding_timeout", 3.0) or 3.0)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            items = data.get("data") or []
            vec = items[0].get("embedding") if items else None
            if isinstance(vec, list) and vec:
                self._emb_total += 1
                return [float(x) for x in vec]
        except Exception:
            self._emb_down_until = time.time() + 60.0
        return []
    def _embed_cached(self, text: str) -> List[float]:
        key = str(text or "")[:200]
        if key in self._emb_cache:
            return self._emb_cache[key]
        vec = self._embed(key)
        if vec:
            if len(self._emb_cache) > 128:
                self._emb_cache.clear()
            self._emb_cache[key] = vec
        return vec
    def _store_vec(self, memory_id: int, vec: List[float]) -> None:
        try:
            dim = len(vec)
            with self.lock:
                self.conn.execute(
                    "INSERT INTO memory_vec(memory_id, model, dim, vec, updated_at) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(memory_id) DO UPDATE SET model=excluded.model, dim=excluded.dim, "
                    "vec=excluded.vec, updated_at=excluded.updated_at",
                    (int(memory_id), str(self.setting("embedding_model", "") or ""), dim,
                     _from_vec(vec), _now()),
                )
                self.conn.commit()
        except Exception:
            pass
    def _load_vecs(self, ids: List[int]) -> Dict[int, List[float]]:
        if not ids:
            return {}
        out: Dict[int, List[float]] = {}
        try:
            marks = ",".join("?" for _ in ids)
            with self.lock:
                rows = self.conn.execute(
                    "SELECT memory_id, dim, vec FROM memory_vec WHERE memory_id IN (%s)" % marks,
                    list(ids),
                ).fetchall()
            for row in rows:
                vec = _to_vec(row["vec"], int(row["dim"] or 0))
                if vec:
                    out[int(row["memory_id"])] = vec
        except Exception:
            return {}
        return out
    def backfill_embeddings(self, limit: int = 200) -> int:
        """给还没有向量的记忆排队补向量（交给后台线程，不阻塞聊天）。"""
        if not self.setting("embedding_enabled"):
            return 0
        try:
            with self.lock:
                rows = self.conn.execute(
                    "SELECT m.id, m.content FROM memories m "
                    "LEFT JOIN memory_vec v ON v.memory_id=m.id "
                    "WHERE v.memory_id IS NULL ORDER BY m.id DESC LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
        except Exception:
            return 0
        for row in rows:
            self._enqueue_embedding(int(row["id"]), str(row["content"] or ""))
        return len(rows)
    def embedding_stats(self) -> Dict[str, Any]:
        try:
            with self.lock:
                have = int(self.conn.execute("SELECT COUNT(*) AS n FROM memory_vec").fetchone()["n"])
                total = int(self.conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"])
        except Exception:
            have, total = 0, 0
        return {
            "enabled": bool(self.setting("embedding_enabled")),
            "model": str(self.setting("embedding_model", "") or ""),
            "vectors": have,
            "memories": total,
            "embedded_ok": self._emb_total,
            "failed": self._emb_failed,
        }
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
        created_at: Optional[int] = None,
        last_hit_at: Optional[int] = None,
        hit_count: int = 0,
        keep_existing_content: bool = False,
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
        # 迁移时保留原始时间：遗忘曲线要靠真实时间，不能全都算"刚刚"
        try:
            created_at = int(created_at) if created_at else now
        except (TypeError, ValueError):
            created_at = now
        try:
            last_hit_at = int(last_hit_at) if last_hit_at else created_at
        except (TypeError, ValueError):
            last_hit_at = created_at
        try:
            hit_count = max(0, int(hit_count or 0))
        except (TypeError, ValueError):
            hit_count = 0
        idem_key = _clean(idem_key, 120)
        if not idem_key and self.setting("auto_dedupe", True):
            idem_key = _norm_key(text)

        with self.lock:
            if idem_key:
                row = self.conn.execute(
                    "SELECT id, content, importance FROM memories "
                    "WHERE bot_id=? AND uid=? AND scope=? AND group_id=? AND idem_key=? "
                    "AND COALESCE(status,'active')='active'",
                    (bot_id, uid, scope, group_id, idem_key),
                ).fetchone()
                if row:
                    # 同一件事又说了一遍：抬一点重要度、刷新时间，不新增行
                    self.conn.execute(
                        "UPDATE memories SET importance=MAX(importance, ?), last_hit_at=?, "
                        "merged_count=merged_count+1 WHERE id=?",
                        (importance, now, int(row["id"])),
                    )
                    self.conn.commit()
                    return {"ok": True, "memory_id": int(row["id"]), "duplicate": True}

            # 近重复（换了标点/语序）也合并：二元组 Jaccard 超过阈值
            if self.setting("auto_dedupe", True):
                cand = self._near_duplicate(bot_id, uid, scope, group_id, text)
                if cand is not None:
                    if keep_existing_content:
                        # 备份导入时不要用旧文本覆盖现在这条更完整的记忆
                        self.conn.execute(
                            "UPDATE memories SET importance=MAX(importance, ?), "
                            "last_hit_at=?, merged_count=merged_count+1 WHERE id=?",
                            (importance, now, int(cand["id"])),
                        )
                    else:
                        self.conn.execute(
                            "UPDATE memories SET content=?, importance=MAX(importance, ?), "
                            "last_hit_at=?, merged_count=merged_count+1 WHERE id=?",
                            (text, importance, now, int(cand["id"])),
                        )
                        self._enqueue_embedding(int(cand["id"]), text)
                    self.conn.commit()
                    return {"ok": True, "memory_id": int(cand["id"]), "duplicate": True}

            cur = self.conn.execute(
                "INSERT INTO memories(bot_id, uid, scope, group_id, kind, content, "
                "importance, hit_count, created_at, last_hit_at, idem_key) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (bot_id, uid, scope, group_id, _clean(kind, 32) or "fact", text,
                 importance, hit_count, created_at, last_hit_at, idem_key),
            )
            self.conn.commit()
            memory_id = int(cur.lastrowid or 0)
        self._enqueue_embedding(memory_id, text)
        try:
            superseded = self._detect_supersede(bot_id, uid, scope, group_id, memory_id, text)
        except Exception:
            superseded = []
        return {"ok": True, "memory_id": memory_id, "duplicate": False,
                "created_at": created_at, "superseded": superseded}
    def _near_duplicate(self, bot_id: str, uid: str, scope: str, group_id: str,
                        text: str) -> Optional[sqlite3.Row]:
        """在同一个人的最近 200 条里找二元组 Jaccard 超过阈值的近重复。"""
        try:
            ratio = float(self.setting("near_duplicate_ratio", 0.92) or 0.92)
        except (TypeError, ValueError):
            ratio = 0.92
        if ratio <= 0 or ratio > 1:
            return None
        tokens = _recall_tokens(text)
        if len(tokens) < 6:
            return None
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, content FROM memories WHERE bot_id=? AND uid=? AND scope=? "
                "AND group_id=? AND COALESCE(status,'active')='active' "
                "ORDER BY last_hit_at DESC LIMIT 200",
                (bot_id, uid, scope, group_id),
            ).fetchall()
        for row in rows:
            other = _recall_tokens(str(row["content"] or ""))
            if len(other) < 6:
                continue
            if _jaccard(tokens, other) >= ratio:
                return row
        return None
    def _detect_supersede(self, bot_id: str, uid: str, scope: str, group_id: str,
                          new_id: int, text: str) -> List[int]:
        """新记忆和旧记忆在同一个"会变的槽位"上取值不同 → 旧的标 superseded。

        只处理明确陈述（住哪 / 在哪上班 / 生日 / 职业），不做语义推理；
        要求两条内容有一定词汇重叠（Jaccard>=0.2），避免把无关话题误判成冲突。
        """
        new_slots = _slot_facts(text)
        if not new_slots:
            return []
        new_tokens = _recall_tokens(text)
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, content, created_at, status FROM memories "
                "WHERE bot_id=? AND uid=? AND scope=? AND group_id=? AND id!=? "
                "AND COALESCE(status,'active')='active' "
                "ORDER BY created_at DESC LIMIT 300",
                (bot_id, uid, scope, group_id, int(new_id)),
            ).fetchall()
        superseded: List[int] = []
        for row in rows:
            old_slots = _slot_facts(str(row["content"] or ""))
            shared = set(old_slots) & set(new_slots)
            if not shared:
                continue
            if not any(old_slots[key] != new_slots[key] for key in shared):
                continue
            old_tokens = _recall_tokens(str(row["content"] or ""))
            # 至少共享一个有信息量的二元组（"主人""搬家"…），避免把无关话题误判成冲突
            shared_tokens = {t for t in (set(new_tokens) & set(old_tokens))
                             if len(t) == 2 and t not in STOP_BIGRAMS}
            if len(shared_tokens) < 1:
                continue
            superseded.append(int(row["id"]))
        if superseded:
            with self.lock:
                self.conn.executemany(
                    "UPDATE memories SET status='superseded', superseded_by=? WHERE id=?",
                    [(int(new_id), old_id) for old_id in superseded],
                )
                self.conn.commit()
        return superseded
    def mark_superseded(self, memory_id: int, by_id: int = 0) -> bool:
        """手动把一条记忆标成过期（面板/人工纠正用）。"""
        try:
            with self.lock:
                self.conn.execute(
                    "UPDATE memories SET status='superseded', superseded_by=? WHERE id=?",
                    (int(by_id or 0), int(memory_id)),
                )
                self.conn.commit()
            return True
        except Exception:
            return False
    def archive_stale(self, bot_id: str, days: int = 90, max_importance: float = 0.3,
                     apply: bool = True) -> Dict[str, Any]:
        """长期没被想起、又不重要的记忆归档（不删除，只是不再参与召回）。"""
        cutoff = _now() - max(1, int(days)) * 86400
        where, params = _bot_filter(bot_id)
        sql = ("SELECT id FROM memories WHERE 1=1" + where +
               " AND COALESCE(status,'active')='active' AND importance<=?"
               " AND COALESCE(hit_count,0)<2 AND COALESCE(last_hit_at,created_at)<?"
               " AND COALESCE(kind,'')!='pinned'")
        params = list(params) + [float(max_importance), int(cutoff)]
        with self.lock:
            ids = [int(row["id"]) for row in self.conn.execute(sql, params).fetchall()]
        if ids and apply:
            with self.lock:
                self.conn.executemany(
                    "UPDATE memories SET status='archived' WHERE id=?",
                    [(i,) for i in ids],
                )
                self.conn.commit()
        return {"candidate": len(ids), "archived": len(ids) if apply else 0,
                "ids": ids[:50]}
    def split_long_summaries(self, bot_id: str, max_chars: int = 0, apply: bool = True,
                             limit: int = 200) -> Dict[str, Any]:
        """把超过上限的摘要按句子拆成多条。

        以前「同天摘要合并」上限是 4000 字，实际合出了 4011/4009/4000 字三条，
        每条都超过 amo 的 1200 字注入预算——一条就把整轮记忆块吃光。这里按句子
        边界拆成 <= max_chars 的块，正文一个字不丢。
        """
        cap = int(max_chars or self.setting("summary_merge_max_chars", 800) or 0)
        if cap <= 0:
            return {"summaries": 0, "created": 0, "applied": bool(apply)}
        where, params = _bot_filter(bot_id)
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, bot_id, uid, content, kind, importance, created_at, last_hit_at, "
                "COALESCE(scope,'private') AS scope, COALESCE(group_id,'') AS group_id "
                "FROM memories WHERE 1=1" + where +
                " AND COALESCE(status,'active')='active'"
                " AND kind IN ('conversation_summary','daily_digest')"
                " AND length(content)>? ORDER BY length(content) DESC LIMIT ?",
                list(params) + [cap, max(1, int(limit))],
            ).fetchall()
        summaries = 0
        created = 0
        for row in rows:
            pieces = _split_long_text(row["content"], cap)
            if len(pieces) < 2:
                continue
            summaries += 1
            if not apply:
                created += len(pieces) - 1
                continue
            with self.lock:
                self.conn.execute(
                    "UPDATE memories SET content=? WHERE id=?",
                    (pieces[0], int(row["id"])),
                )
                for piece in pieces[1:]:
                    cur = self.conn.execute(
                        "INSERT INTO memories(bot_id, uid, scope, group_id, kind, content, "
                        "importance, hit_count, created_at, last_hit_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (str(row["bot_id"]), str(row["uid"]), str(row["scope"]),
                         str(row["group_id"]), str(row["kind"]), piece,
                         float(row["importance"] or 0.5), 0,
                         int(row["created_at"] or _now()), int(row["last_hit_at"] or _now())),
                    )
                    new_id = int(cur.lastrowid or 0)
                    created += 1
                    if new_id:
                        self._enqueue_embedding(new_id, piece)
                self.conn.commit()
            self._enqueue_embedding(int(row["id"]), pieces[0])
        return {"summaries": summaries, "created": created, "applied": bool(apply)}
    def compress_memories(self, bot_id: str, apply: bool = True,
                          min_jaccard: float = 0.7, limit: int = 600) -> Dict[str, Any]:
        """周度整理：长摘要拆条、同一天摘要合并、近重复记忆标成过期（不删数据）。"""
        cap = int(self.setting("summary_merge_max_chars", 800) or 0)
        split_info = self.split_long_summaries(bot_id, cap, apply=bool(apply), limit=200)
        where, params = _bot_filter(bot_id)
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, uid, content, kind, importance, created_at, hit_count, "
                "COALESCE(scope,'private') AS scope, COALESCE(group_id,'') AS group_id "
                "FROM memories"
                " WHERE 1=1" + where +
                " AND COALESCE(status,'active')='active' ORDER BY created_at DESC LIMIT ?",
                list(params) + [max(1, int(limit))],
            ).fetchall()
        merged = 0
        # 按「谁 + 哪一天 + 哪个会话」分组：私聊摘要和某群的摘要同一天不能并成一条，
        # 两个人的同一天摘要也不能。改前只按日期分，跨人、跨会话都会拼在一起——
        # 群里的原话被拼进私聊那条，私聊召回就会把群聊内容说出来。
        groups: Dict[Any, List[sqlite3.Row]] = {}
        for row in rows:
            if str(row["kind"] or "") != "conversation_summary":
                continue
            match = re.search(r"(\d{4}-\d{2}-\d{2})", str(row["content"] or ""))
            if match:
                key = (str(row["uid"] or ""), match.group(1),
                       str(row["scope"] or "private"), str(row["group_id"] or ""))
                groups.setdefault(key, []).append(row)
        for day, items in groups.items():
            if len(items) < 2:
                continue
            items.sort(key=lambda r: (len(str(r["content"] or "")), int(r["created_at"] or 0)), reverse=True)
            keep = items[0]
            drop = items[1:]
            if apply:
                # 同一批摘要各有信息：把正文拼到保留的那条上，再标记别条（不丢内容）
                merged_text = str(keep["content"] or "")
                absorbed: List[sqlite3.Row] = []
                for row in drop:
                    piece = str(row["content"] or "").strip()
                    if not piece or piece in merged_text:
                        absorbed.append(row)  # 正文已经在保留行里，标 merged 不会丢内容
                        continue
                    candidate = merged_text + "\n" + piece
                    if len(candidate) > (cap or 4000):
                        break  # 超预算：这条和后面的都保持 active，绝不带病标 merged
                    merged_text = candidate
                    absorbed.append(row)
                if absorbed:
                    with self.lock:
                        self.conn.execute(
                            "UPDATE memories SET content=?, merged_count=COALESCE(merged_count,1)+?, "
                            "importance=MAX(importance, ?) WHERE id=?",
                            (merged_text, len(absorbed),
                             max([float(r["importance"] or 0) for r in items]),
                             int(keep["id"])),
                        )
                        self.conn.executemany(
                            "UPDATE memories SET status='merged', superseded_by=? WHERE id=?",
                            [(int(keep["id"]), int(r["id"])) for r in absorbed],
                        )
                        self.conn.commit()
            merged += len(absorbed) if apply else 0

        # 通用近重复（只处理最近 limit 条，代价可控）
        dedup = 0
        tokens = [(int(r["id"]), str(r["kind"] or ""), str(r["scope"] or "private"),
                   str(r["group_id"] or ""), _recall_tokens(str(r["content"] or "")))
                  for r in rows]
        with self.lock:
            # 集合只建一次：原来每比较一对都要重新 set() 两遍，600 条时是 36 万次集合构造
            token_sets = [set(toks) for (_id, _kind, _scope, _group, toks) in tokens]
            for i in range(len(tokens)):
                id_a, kind_a, scope_a, group_a, tok_a = tokens[i]
                if len(tok_a) < 8:
                    continue
                for j in range(i + 1, len(tokens)):
                    id_b, kind_b, scope_b, group_b, tok_b = tokens[j]
                    if kind_a != kind_b:
                        continue
                    # 跨会话的近重复不合并：群里的那条接不到私聊行上
                    if scope_a != scope_b or group_a != group_b:
                        continue
                    if len(tok_b) < 8:
                        continue
                    if abs(len(tok_a) - len(tok_b)) > max(len(tok_a), len(tok_b)) * 0.5:
                        continue
                    # 双向都要像，且长度接近，避免"信息更少的短句被长句吞掉"
                    sa, sb = token_sets[i], token_sets[j]
                    jaccard = len(sa & sb) / float(len(sa | sb) or 1)
                    containment = len(sa & sb) / float(min(len(sa), len(sb)) or 1)
                    ratio = float(min(len(tok_a), len(tok_b))) / float(max(len(tok_a), len(tok_b)) or 1)
                    if jaccard < float(min_jaccard) or containment < 0.9 or ratio < 0.7:
                        continue
                    if apply:
                        self.conn.execute(
                            "UPDATE memories SET status='merged', superseded_by=? WHERE id=?",
                            (id_a, id_b),
                        )
                    dedup += 1
            if apply and dedup:
                self.conn.commit()
        return {"merged_summaries": merged, "merged_near_duplicates": dedup,
                "split_summaries": split_info.get("summaries", 0),
                "split_pieces": split_info.get("created", 0),
                "applied": bool(apply)}
    def _substring_candidates(self, base, params, text, limit):
        """trigram FTS 子串粗筛；不可用/查询太短时返回 None（调用方退回 LIKE）。"""
        if not getattr(self, "_fts_tri", False) or len(text) < 3:
            return None
        phrase = '"' + str(text).replace('"', '""') + '"'
        try:
            with self.lock:
                hits = self.conn.execute(
                    "SELECT rowid FROM memories_ftst WHERE memories_ftst MATCH ? LIMIT ?",
                    (phrase, int(limit) * 2),
                ).fetchall()
                if not hits:
                    return None
                ids = [int(row["rowid"]) for row in hits]
                marks = ",".join("?" for _ in ids)
                return self.conn.execute(
                    base + " AND id IN (%s) ORDER BY last_hit_at DESC, created_at DESC LIMIT ?" % marks,
                    list(params) + ids + [int(limit)],
                ).fetchall()
        except Exception as exc:
            logger.debug("[amo_data] trigram 粗筛失败，退回 LIKE: %s" % exc)
            self._fts_tri = False
            return None
    def recall(
        self,
        bot_id: str,
        uid: str,
        query: str = "",
        limit: int = 6,
        scope: str = "",
        group_id: str = "",
        half_life_days: Optional[float] = None,
        importance_decay: Optional[float] = None,
        pool_size: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """按「相关性 + 重要度 + 新鲜度」综合打分召回。

        相关性 = BM25（单字+二元组）。配了 embedding 时再叠一层语义余弦。
        另外两道闸防止"什么都推一条"：
        - 关键词证据：query 里去掉虚词后至少要命中一个二元组/英文词；
        - 语义证据：纯语义命中要求余弦 >= vec_min_cos。
        两者都不满足就直接返回空，交给调用方说"不记得"。
        """
        bot_id, uid = str(bot_id), str(uid)
        limit = max(1, min(int(limit or 6), 100))
        text = _clean(query, 200)
        meta_query = bool(text) and bool(self.setting("meta_query_fallback", True)) \
            and _is_meta_query(text)
        now = _now()
        half_life_days = float(half_life_days if half_life_days is not None
                               else self.setting("forget_half_life_days", RECALL_HALF_LIFE_DAYS) or RECALL_HALF_LIFE_DAYS)
        importance_decay = float(importance_decay if importance_decay is not None
                                 else self.setting("importance_decay", RECALL_IMPORTANCE_DECAY) or 0.0)
        pool_size = int(pool_size if pool_size is not None
                        else self.setting("pool_size", RECALL_POOL) or RECALL_POOL)

        # 解析查询里的时间词（昨天/前天/11月6号…），给对应日期的阶段记忆加权
        span = None
        if _parse_time_span is not None:
            try:
                span = _parse_time_span(text)
            except Exception:
                span = None

        # 自适应候选池：库小就全量精排，库大才截断（固定 400 会让老记忆永远进不了池）
        try:
            with self.lock:
                total = int(self.conn.execute(
                    "SELECT COUNT(*) AS n FROM memories WHERE bot_id=? AND uid=? "
                    "AND COALESCE(status,'active')='active'",
                    (bot_id, uid),
                ).fetchone()["n"])
        except Exception:
            total = pool_size
        pool_size = max(limit, min(max(pool_size, total), 4000))

        base = (
            "SELECT id, content, kind, importance, created_at, last_hit_at, hit_count, scope, group_id "
            "FROM memories WHERE bot_id=? AND uid=? AND COALESCE(status,'active')='active'"
        )
        params: List[Any] = [bot_id, uid]
        if scope in ("private", "group"):
            base += " AND scope=?"
            params.append(scope)
        if group_id:
            base += " AND group_id=?"
            params.append(str(group_id))

        rows: List[sqlite3.Row] = []
        # 粗筛 1：子串命中。>=3 个字时用 trigram FTS（避免全表 LIKE），否则/没命中时退回 LIKE。
        if text:
            rows = self._substring_candidates(base, params, text, pool_size)
            if rows is None:
                with self.lock:
                    rows = self.conn.execute(
                        base + " AND content LIKE ? ORDER BY last_hit_at DESC, created_at DESC LIMIT ?",
                        params + ["%" + text + "%", pool_size],
                    ).fetchall()
        # 粗筛 2：补最近活跃的，保证词序不同的记忆（「上班」vs「上夜班」）也有候选。
        # 这一层单独限流：库涨到几千条以后，按 last_hit_at 排的前 4000 条几乎全是常聊话题，
        # 会把真正相关但很久没被想起的记忆挤出候选池，也白算 BM25。
        recent_pool = max(limit, min(int(self.setting("recall_recent_pool", 600) or 600), 4000))
        with self.lock:
            seen = {int(r["id"]) for r in rows}
            more = self.conn.execute(
                base + " ORDER BY last_hit_at DESC, created_at DESC LIMIT ?",
                params + [recent_pool],
            ).fetchall()
        rows.extend([r for r in more if int(r["id"]) not in seen])

        # 问"昨天/前天"时，把对应当天的日总结直接拉进候选池（否则它可能连候选都进不了）
        if span:
            try:
                date_tag = "%04d-%02d-%02d" % (int(span["year"]), int(span["month"]), int(span["day"]))
                with self.lock:
                    digests = self.conn.execute(
                        base + " AND kind='daily_digest' AND content LIKE ? LIMIT 3",
                        params + ["%" + date_tag + "%"],
                    ).fetchall()
                seen = {int(r["id"]) for r in rows}
                rows.extend([r for r in digests if int(r["id"]) not in seen])
            except Exception:
                pass
        if not rows:
            # 「你还记得我吗」这种没有实词的元问题，关键词粗筛一条都进不来；
            # 不兜底的话她会回「记得呀」但其实手上没有任何记忆。
            return self._meta_fallback(bot_id, uid, scope, group_id, now,
                                       half_life_days, importance_decay) if meta_query else []

        contents = [str(r["content"] or "") for r in rows]
        q_tokens = _recall_tokens(text) if text else []
        docs = [_recall_tokens(c) for c in contents]
        raw = _bm25_scores(q_tokens, docs) if q_tokens else [0.0 for _ in rows]

        # 可选语义分：查询向量 + 候选向量（超时/没装 Ollama 时自然退化成纯 BM25）
        q_vec: List[float] = []
        vec_map: Dict[int, List[float]] = {}
        if text and self.setting("embedding_enabled"):
            q_vec = self._embed_cached(text)
            if q_vec:
                vec_map = self._load_vecs([int(r["id"]) for r in rows])
        vec_weight = max(0.0, min(0.9, float(self.setting("vec_weight", 0.45) or 0.0)))
        vec_min_cos = float(self.setting("vec_min_cos", 0.45) or 0.0)
        vec_min_margin = float(self.setting("vec_min_margin", 0.08) or 0.0)
        min_terms = int(self.setting("min_matched_terms", 1) or 0)
        infos = set(_informative_terms(text)) if text else set()
        qchars = set(_query_chars(text)) if text else set()

        # 语义闸：只有"明显比其它候选更像"的那条才允许在没关键词的情况下入选。
        # bge-m3 的中文余弦整体偏平（无关也能到 0.5），所以看的是 top 与第二名的差距。
        coss: Dict[int, float] = {}
        best_cos = 0.0
        vec_pass = False
        if q_vec:
            for row in rows:
                coss[int(row["id"])] = _cosine(q_vec, vec_map.get(int(row["id"]), []))
            ranked = sorted(coss.values(), reverse=True)
            best_cos = ranked[0] if ranked else 0.0
            second = ranked[1] if len(ranked) > 1 else 0.0
            margin = best_cos - second
            vec_pass = best_cos >= max(vec_min_cos, 0.45) and (
                margin >= vec_min_margin or best_cos >= 0.75
            )

        scored: List[Dict[str, Any]] = []
        top_rel = max(raw) if raw else 0.0
        info_count = len(infos) or 1
        for idx, row in enumerate(rows):
            rel = (raw[idx] / top_rel) if top_rel > 0 else 0.0
            doc_set = set(docs[idx])
            # 关键词覆盖率：命中越多查询里的词，越可信（长摘要不容易被短而准的记忆反超）
            if infos:
                covered = len(infos & doc_set) / float(info_count)
                rel = 0.65 * rel + 0.35 * covered
            cos = coss.get(int(row["id"]), 0.0) if q_vec else 0.0
            # 问"昨天"时，对应当天的日总结即使没有任何关键词命中也要保留
            date_hit = False
            if span and str(row["kind"] or "") == "daily_digest":
                try:
                    date_hit = ("%04d-%02d-%02d" % (int(span["year"]), int(span["month"]), int(span["day"]))
                                in str(row["content"] or ""))
                except Exception:
                    date_hit = False
            if text and not (rel > 0 or cos > 0 or date_hit):
                continue
            # 关键词证据：命中二元组/英文词，或短词的单字覆盖率够高
            strong_hits = len(infos & doc_set) if infos else 0
            char_hits = len(qchars & doc_set) if qchars else 0
            coverage = (char_hits / float(len(qchars))) if qchars else 0.0
            keyword_ok = (
                strong_hits >= min_terms
                or (len(qchars) == 1 and char_hits >= 1)
                or (char_hits >= 2 and coverage >= 0.75)
            )
            if q_vec:
                # 关键词已经命中时，语义只做小幅加权（否则长摘要会靠"泛相似"挤掉精准记忆）；
                # 关键词完全没命中时，才让语义分当主角。
                w = vec_weight if not keyword_ok else min(vec_weight, 0.2)
                rel = (1.0 - w) * rel + w * max(0.0, cos)
            if text and not keyword_ok and not date_hit:
                # 关键词没证据，就只认"明显更像"的那一条语义结果
                if not vec_pass or cos < best_cos - 1e-9:
                    continue
            fresh = _freshness(row["last_hit_at"] or row["created_at"], now, half_life_days)
            try:
                imp = max(0.0, min(1.0, float(row["importance"] or 0.5)))
            except (TypeError, ValueError):
                imp = 0.5
            weight = max(0.0, min(0.9, float(importance_decay or 0.0)))
            imp_eff = imp * (1.0 - weight) + imp * weight * fresh
            try:
                hits = int(row["hit_count"] or 0)
            except (TypeError, ValueError):
                hits = 0
            hit_boost = min(1.0, math.log1p(hits) / math.log1p(10)) if hits > 0 else 0.0

            if text:
                score = (
                    RECALL_W_REL * rel
                    + RECALL_W_IMPORTANCE * imp_eff
                    + RECALL_W_FRESH * (0.6 * fresh + 0.4 * hit_boost)
                )
            else:
                denom = RECALL_W_IMPORTANCE + RECALL_W_FRESH
                score = (RECALL_W_IMPORTANCE * imp_eff + RECALL_W_FRESH * (0.6 * fresh + 0.4 * hit_boost)) / denom
            # 阶段记忆（每日总结）是"整理过的知识"，同等相关性下优先；
            # 用固定小加成，不改变排序的大方向。
            kind = str(row["kind"] or "")
            # 阶段记忆是"整理过的知识"：只给固定小加成，不做时间词加权
            # （时间问题由【按时间回看】走聊天记录，不靠记忆排序抢）
            if kind == "daily_digest" and (not text or rel >= 0.35):
                score += 0.04
            # "昨天我们干了什么"这类问题：把对应那天的日总结直接顶上来
            # 0.2 不够：11-04/05 那种高频旧记忆（关灯睡了、晕碳、拍午饭）能到 0.62，
            # 日总结只有 0.56 排第 4，模型就改用旧事答「昨天」。0.5 让它稳定第一。
            if kind == "daily_digest" and span:
                try:
                    date_tag = "%04d-%02d-%02d" % (int(span["year"]), int(span["month"]), int(span["day"]))
                    if date_tag in str(row["content"] or ""):
                        # 模糊问法（「昨天我们干了什么」）日总结直接顶上来；
                        # 具体问法（「昨天中午吃的什么」）仍按相关性排，只给小幅加成，
                        # 否则精确命中那条（比如「中午吃了番茄鸡蛋面」）会被日总结挤下去。
                        vague = any(word in text for word in
                                    ("干了什么", "干什么", "干啥", "做了什么", "做了啥",
                                     "聊了什么", "说了什么", "怎么过的", "过得怎么样"))
                        score += 0.5 if vague else 0.2
                except Exception:
                    pass
            elif kind == "conversation_summary" and (not text or rel >= 0.35):
                score += 0.02
            item = dict(row)
            item["score"] = round(score, 4)
            item["matched_terms"] = max(strong_hits, char_hits)
            item["vec_score"] = round(cos, 4) if q_vec else 0.0
            scored.append(item)

        scored.sort(key=lambda x: x["score"], reverse=True)
        # 注入去重/多样化：同一件事只留最相关的一条，避免 6 条记忆里 4 条在说同一件事
        tokens_by_id = {int(row["id"]): set(docs[idx]) for idx, row in enumerate(rows)}
        div_ratio = float(self.setting("diversify_ratio", 0.75) or 0.0)
        items: List[Dict[str, Any]] = []
        selected_tokens: List[set] = []
        for item in scored:
            if len(items) >= limit:
                break
            toks = tokens_by_id.get(int(item["id"]), set())
            if div_ratio > 0 and toks and any(
                _jaccard(toks, chosen) >= div_ratio for chosen in selected_tokens
            ):
                continue
            items.append(item)
            selected_tokens.append(toks)

        if not items and meta_query:
            items = self._meta_fallback(bot_id, uid, scope, group_id, now,
                                       half_life_days, importance_decay)
        if items:
            with self.lock:
                self.conn.executemany(
                    "UPDATE memories SET hit_count=hit_count+1, last_hit_at=? WHERE id=?",
                    [(now, item["id"]) for item in items],
                )
                self.conn.commit()
        return items
    def _meta_fallback(self, bot_id: str, uid: str, scope: str, group_id: str,
                       now: int, half_life_days: float, importance_decay: float) -> List[Dict[str, Any]]:
        """元问题兜底：没有关键词可匹配时，按「重要度 + 新鲜度」给几条最值得说的。

        「你还记得我吗 / 你记得什么」这类问题本身没有实词，关键词闸门会返回 0 条，
        表现就是她嘴上说记得、其实一条记忆都没拿到。这里只在这种查询上兜底，
        正常提问（带人名/事件/时间）仍然严格走相关性，不会被兜底污染。
        """
        if not self.setting("meta_query_fallback", True):
            return []
        limit = max(1, min(int(self.setting("meta_query_limit", 3) or 3), 10))
        where = "bot_id=? AND uid=? AND COALESCE(status,'active')='active'"
        params: List[Any] = [str(bot_id), str(uid)]
        if scope in ("private", "group"):
            where += " AND scope=?"
            params.append(scope)
        if group_id:
            where += " AND group_id=?"
            params.append(str(group_id))
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, content, kind, importance, created_at, last_hit_at, hit_count, scope, group_id "
                "FROM memories WHERE " + where +
                " ORDER BY importance DESC, last_hit_at DESC LIMIT ?",
                params + [limit * 4],
            ).fetchall()
        picked: List[Dict[str, Any]] = []
        for row in rows:
            try:
                imp = max(0.0, min(1.0, float(row["importance"] or 0.5)))
            except (TypeError, ValueError):
                imp = 0.5
            fresh = _freshness(row["last_hit_at"] or row["created_at"], now, half_life_days)
            weight = max(0.0, min(0.9, float(importance_decay or 0.0)))
            imp_eff = imp * (1.0 - weight) + imp * weight * fresh
            try:
                hits = int(row["hit_count"] or 0)
            except (TypeError, ValueError):
                hits = 0
            hit_boost = min(1.0, math.log1p(hits) / math.log1p(10)) if hits > 0 else 0.0
            score = 0.65 * imp_eff + 0.35 * (0.6 * fresh + 0.4 * hit_boost)
            item = dict(row)
            item["score"] = round(score, 4)
            item["matched_terms"] = 0
            item["vec_score"] = 0.0
            item["meta_fallback"] = True
            picked.append(item)
            if len(picked) >= limit:
                break
        if picked:
            with self.lock:
                self.conn.executemany(
                    "UPDATE memories SET hit_count=hit_count+1, last_hit_at=? WHERE id=?",
                    [(now, item["id"]) for item in picked],
                )
                self.conn.commit()
        return picked
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
            if cur.rowcount:
                # 向量表是独立表，不跟着删会一直堆积（AUTOINCREMENT 不复用 id，不会串数据）
                try:
                    self.conn.execute("DELETE FROM memory_vec WHERE memory_id=?", (int(memory_id),))
                except Exception:
                    pass
            self.conn.commit()
            return bool(cur.rowcount)
