# -*- coding: utf-8 -*-
"""我想永远记住你 —— 轻量数据存储层。

只做四件事：画像、记忆、关系、缓存。没有鉴权、没有证据链、没有哈希身份。

设计要点：
- 全部按 ``bot_id`` 隔离：同一个 QQ 号在不同 bot 下是独立画像和独立记忆；
- ``uid`` 直接用平台原始 ID，不做任何哈希或二次映射；
- 画像字段（name / alias / relation / remark）可以随时被外部改写并保存回去；
- 记忆用「BM25（单字+二元组）+ 重要度 + 遗忘曲线」检索，不依赖向量库；
  配了 embedding 时再叠加一层语义相似度（可选，默认关）；
- 关系网和日程缓存也放这里，陪伴插件只负责聊天。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import re
import sqlite3
import struct
import threading
import time
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

try:
    from astrbot.api import logger
except Exception:  # 单测/纯逻辑场景允许脱离 AstrBot 运行
    import logging

    logger = logging.getLogger("amo_data")


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



# ---------------------------------------------------------------------------
# 检索打分（BM25 + importance + 遗忘曲线）
# ---------------------------------------------------------------------------

RECALL_POOL = 400              # 候选池上限：先粗筛这么多条，再精排
RECALL_HALF_LIFE_DAYS = 45.0   # 遗忘半衰期（天）：45 天新鲜度掉一半
RECALL_W_REL = 0.60            # 相关性权重
RECALL_W_IMPORTANCE = 0.30     # 重要度权重
RECALL_W_FRESH = 0.10          # 新鲜度权重
RECALL_IMPORTANCE_DECAY = 0.10 # importance 自身也随时间软化（0=不衰减）
RECALL_MIN_REL = 0.05          # 有 query 时，相关性低于此值直接丢弃


# ---------------------------------------------------------------------------
# 本地增强（2.3.x patch）：相关性闸 / 自适应候选池 / 自动去重 / 可选向量
# ---------------------------------------------------------------------------

# 常见虚词：只参与 BM25（靠 IDF 自动降权），不单独作为"命中证据"
STOP_CHARS = set(
    "我你他她它的了吗呢啊吧呀哦嗯是在和就也都这那有没不很太会要个些么什怎样多少"
    "还记说给把被让对从到又再才只更最得着过们嘛哪谁几"
)

# 只表示时间/泛指、但没有信息量的二元组：不能单独当作"她想起来了"的证据
STOP_BIGRAMS = {
    "今天", "明天", "现在", "刚才", "最近", "时候", "事情", "东西",
    "这个", "那个", "什么", "怎么", "怎样", "多少", "是不", "不是",
}

DEFAULT_SETTINGS: Dict[str, Any] = {
    "pool_size": RECALL_POOL,          # 粗筛池；会按库大小自适应放大
    "min_matched_terms": 1,            # 至少命中几个"有信息量"的词，0=关掉这道闸
    "forget_half_life_days": RECALL_HALF_LIFE_DAYS,
    "importance_decay": RECALL_IMPORTANCE_DECAY,
    "auto_dedupe": True,               # 调用方没给 idem_key 时按内容指纹去重
    "near_duplicate_ratio": 0.92,      # 二元组 Jaccard 超过它就当同一件事
    "embedding_enabled": False,        # 语义召回开关（本地 Ollama 等）
    "embedding_base": "http://127.0.0.1:11434",
    "embedding_model": "bge-m3",
    "embedding_timeout": 3.0,
    "vec_weight": 0.45,                # 语义分在相关性里的占比
    "vec_min_cos": 0.45,               # 纯语义命中的最低余弦（bge-m3 中文普遍偏平）
    "vec_min_margin": 0.08,            # top 与第二名的余弦差；太小说明"谁都不像"
    "diversify_ratio": 0.75,        # 注入时同一件事只留一条（0=关闭多样化）
    "embedding_backfill": True,        # 启动时给旧记忆补向量
    "embedding_backfill_limit": 200,   # 每次最多补多少条
    "recall_fts_enabled": False,       # 可选：FTS5 trigram 子串粗筛（默认关，见 CHANGELOG 实测）
    "recall_recent_pool": 600,         # 「最近活跃」这一层的上限，库大了不至于把几条热门记忆全捞进来
}


def _norm_key(text: Any) -> str:
    """内容指纹：去掉空白和标点后取 sha1 前 16 位。"""
    raw = re.sub(
        r"[\s\u3000·•、,，。.!！?？~～\-—_\"'“”‘’（）()\[\]【】:：;；]+",
        "", str(text or ""),
    ).lower()
    if not raw:
        return ""
    return "auto:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _query_chars(text: Any) -> List[str]:
    """查询里的实词单字（去掉虚词），用于"短词模糊命中"的覆盖率判断。"""
    return [c for c in re.findall(r"[\u4e00-\u9fff]", str(text or "").lower())
            if c not in STOP_CHARS]


def _informative_terms(text: Any) -> List[str]:
    """命中证据：去掉虚词后的二元组 / 英文词；太短的查询退回单字。

    这是为了修掉"什么都能推一条"：只靠 BM25 相对分排序时，
    库里哪怕没有相关内容，最像的那条也会被归一化成满分。
    """
    s = str(text or "").lower()
    words = [w for w in re.findall(r"[a-z0-9_]{2,}", s) if w not in STOP_CHARS]
    chars = [c for c in re.findall(r"[\u4e00-\u9fff]", s) if c not in STOP_CHARS]
    if not chars:
        return words
    if len(chars) == 1:
        return words + chars
    return words + [
        a + b for a, b in zip(chars, chars[1:]) if (a + b) not in STOP_BIGRAMS
    ]


def _jaccard(a: List[str], b: List[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(len(sa | sb))


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (na * nb)


# 常见"会变的事实"槽位：同一个人身上出现不同值就说明旧的过期了
SLOT_PATTERNS = (
    ("home", re.compile(r"(?:住在|住到|搬到|搬家到|搬去)\s*([\u4e00-\u9fffA-Za-z0-9\u00b7\-]{2,14})")),
    ("work", re.compile(r"在\s*([\u4e00-\u9fffA-Za-z0-9\u00b7\-]{2,14})\s*(?:上班|工作|公司)")),
    ("birthday", re.compile(r"生日(?:是|在)?\s*(\d{1,2}\s*月\s*\d{1,2}\s*[日号])")),
    ("job", re.compile(r"(?:职业是|工作是|做的?是)\s*([\u4e00-\u9fffA-Za-z0-9]{2,10})")),
)


def _slot_facts(text: Any) -> Dict[str, str]:
    """抽出"住在哪/在哪上班/生日/职业"这类槽位值；抽不到就是空表。"""
    raw = str(text or "")
    out: Dict[str, str] = {}
    for key, pattern in SLOT_PATTERNS:
        match = pattern.search(raw)
        if not match:
            continue
        value = re.sub(r"\s+", "", match.group(1) or "")
        # 去掉"住了/那边/一带/附近/了/的"这类尾巴，否则同一个地点会算出两个值
        value = re.sub(r"(住了|住|那边|这边|一带|附近|旁边|了|的)+$", "", value)
        # 「记住北京的咖啡」这类会被误当成住址：带虚词的不是地名，直接不要
        if not value or re.search(r"[的了着这那]", value):
            continue
        # 只保留地名/名词部分（去掉句末标点）
        value = value.strip("，。！？、;；")
        if value:
            out[key] = value
    return out


def _to_vec(blob: Any, dim: int) -> List[float]:
    """BLOB -> list[float]；只认 float32，长度不对就当无效。"""
    try:
        raw = bytes(blob or b"")
    except Exception:
        return []
    if not raw or dim <= 0 or len(raw) != dim * 4:
        return []
    try:
        return list(struct.unpack("<%df" % dim, raw))
    except Exception:
        return []


def _from_vec(vec: List[float]) -> bytes:
    return struct.pack("<%df" % len(vec), *[float(x) for x in vec])



def _recall_tokens(text: Any) -> List[str]:
    """中文按 单字 + 二元组 切，英文数字按词切。

    中文没有空格，FTS5 的 unicode61 会把整句当一个 token，
    「两个字」「上班」这种短词永远 MATCH 不到；这里用单字+二元组，
    保证「猫」「茶」「上班」这种 1~2 字输入也能参与打分。
    """
    s = str(text or "").lower()
    tokens: List[str] = re.findall(r"[a-z0-9_]{2,}", s)
    chars = re.findall(r"[\u4e00-\u9fff]", s)
    tokens.extend(chars)
    tokens.extend(["".join(pair) for pair in zip(chars, chars[1:])])
    return tokens


def _bm25_scores(q_tokens: List[str], docs: List[List[str]], k1: float = 1.4,
                 b: float = 0.75) -> List[float]:
    """标准 BM25（Okapi），返回每个 doc 的原始分。"""
    if not docs or not q_tokens:
        return [0.0 for _ in docs]
    n = len(docs)
    avgdl = sum(len(d) for d in docs) / float(n) or 1.0
    df: Dict[str, int] = {}
    for d in docs:
        for term in set(d):
            df[term] = df.get(term, 0) + 1
    out: List[float] = []
    for d in docs:
        dl = len(d) or 1
        score = 0.0
        tf: Dict[str, int] = {}
        for term in d:
            tf[term] = tf.get(term, 0) + 1
        for term in q_tokens:
            if term not in tf:
                continue
            freq = tf[term]
            idf = (n - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5)
            idf = math.log(1.0 + idf)
            score += idf * (freq * (k1 + 1)) / (freq + k1 * (1 - b + b * dl / avgdl))
        out.append(score)
    return out


def _freshness(ts: Any, now: int, half_life_days: float) -> float:
    """遗忘曲线：0.5 ** (age/half_life)。未来的时间按 1 处理。"""
    try:
        stamp = float(ts or 0)
    except (TypeError, ValueError):
        stamp = 0.0
    if stamp <= 0:
        return 1.0
    age_days = max(0.0, (now - stamp) / 86400.0)
    hl = max(1.0, float(half_life_days or RECALL_HALF_LIFE_DAYS))
    return 0.5 ** (age_days / hl)



def _unique_labels(links) -> list:
    """同一个人可能同时有 qq / aiocqhttp 两条链接，面板只显示一次 QQ。"""
    out = []
    for item in links or []:
        label = str(item.get("label") or item.get("platform") or "")
        if label and label not in out:
            out.append(label)
    return out


class DataStore:
    """SQLite 存储。所有写操作走同一把 RLock，读操作也加锁，避免并发重开。"""

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
        # 让后台向量任务尽快退出（worker 只处理到队列空）
        try:
            self._emb_queue.put_nowait(None)
        except Exception:
            pass

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
            for item in detail.get("links", []) or []:
                rows = self.conn.execute(
                    "SELECT platform, uid, name, role, content, created_at, compressed FROM messages "
                    "WHERE bot_id=? AND pid=? AND platform=? AND uid=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (str(bot_id), int(detail["pid"]), str(item.get("platform") or ""),
                     str(item.get("uid") or ""), per_platform),
                ).fetchall()
                for row in rows:
                    gathered[(row["platform"], row["uid"], row["created_at"], row["content"])] = row
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
    # ---------- 向量（可选，默认关） ----------
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
                    "WHERE bot_id=? AND uid=? AND idem_key=? "
                    "AND COALESCE(status,'active')='active'",
                    (bot_id, uid, idem_key),
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

    def compress_memories(self, bot_id: str, apply: bool = True,
                          min_jaccard: float = 0.7, limit: int = 600) -> Dict[str, Any]:
        """周度整理：同一天的摘要合并成一条，近重复记忆标成过期（不删数据）。"""
        where, params = _bot_filter(bot_id)
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, content, kind, importance, created_at, hit_count FROM memories"
                " WHERE 1=1" + where +
                " AND COALESCE(status,'active')='active' ORDER BY created_at DESC LIMIT ?",
                list(params) + [max(1, int(limit))],
            ).fetchall()
        merged = 0
        groups: Dict[str, List[sqlite3.Row]] = {}
        for row in rows:
            if str(row["kind"] or "") != "conversation_summary":
                continue
            match = re.search(r"(\d{4}-\d{2}-\d{2})", str(row["content"] or ""))
            if match:
                groups.setdefault(match.group(1), []).append(row)
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
                    if len(candidate) > 4000:
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
        tokens = [(int(r["id"]), str(r["kind"] or ""), _recall_tokens(str(r["content"] or "")))
                  for r in rows]
        with self.lock:
            for i in range(len(tokens)):
                id_a, kind_a, tok_a = tokens[i]
                if len(tok_a) < 8:
                    continue
                for j in range(i + 1, len(tokens)):
                    id_b, kind_b, tok_b = tokens[j]
                    if kind_a != kind_b:
                        continue
                    if len(tok_b) < 8:
                        continue
                    if abs(len(tok_a) - len(tok_b)) > max(len(tok_a), len(tok_b)) * 0.5:
                        continue
                    # 双向都要像，且长度接近，避免"信息更少的短句被长句吞掉"
                    sa, sb = set(tok_a), set(tok_b)
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
        with self.lock:
            self.conn.execute(
                "UPDATE memories SET merged_count=COALESCE(merged_count,1) WHERE merged_count IS NULL"
            ) if False else None
        return {"merged_summaries": merged, "merged_near_duplicates": dedup,
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
        now = _now()
        half_life_days = float(half_life_days if half_life_days is not None
                               else self.setting("forget_half_life_days", RECALL_HALF_LIFE_DAYS) or RECALL_HALF_LIFE_DAYS)
        importance_decay = float(importance_decay if importance_decay is not None
                                 else self.setting("importance_decay", RECALL_IMPORTANCE_DECAY) or 0.0)
        pool_size = int(pool_size if pool_size is not None
                        else self.setting("pool_size", RECALL_POOL) or RECALL_POOL)

        # 解析查询里的时间词（昨天/前天/11月6号…），给对应日期的阶段记忆加权
        span = None
        try:
            from time_query import parse_time_span
            span = parse_time_span(text)
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
            return []

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
            if kind == "daily_digest" and span:
                try:
                    date_tag = "%04d-%02d-%02d" % (int(span["year"]), int(span["month"]), int(span["day"]))
                    if date_tag in str(row["content"] or ""):
                        score += 0.2
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

        if items:
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
            if cur.rowcount:
                # 向量表是独立表，不跟着删会一直堆积（AUTOINCREMENT 不复用 id，不会串数据）
                try:
                    self.conn.execute("DELETE FROM memory_vec WHERE memory_id=?", (int(memory_id),))
                except Exception:
                    pass
            self.conn.commit()
            return bool(cur.rowcount)

    # ---------- 聊天记录 ----------
    def add_message(self, bot_id: str, umo: str, uid: str = "", name: str = "",
                    role: str = "user", content: str = "", platform: str = "",
                    created_at: int = 0, message_id: str = "") -> int:
        text = _clean(content, 4000)
        if not text:
            return 0
        platform = str(platform or "").strip() or platform_of_umo(umo)
        role_n = "bot" if str(role) == "bot" else "user"
        try:
            ts = int(created_at) if created_at else _now()
        except (TypeError, ValueError):
            ts = _now()
        pid = 0
        if role_n != "bot" and uid:
            try:
                pid = self.resolve_person(bot_id, platform, uid, name)
            except Exception:
                pid = 0
        mid = _clean(str(message_id or "").strip(), 128)
        with self.lock:
            # 平台消息 id 是去重的最强证据：重连/回调重放时同一个 message_id 一定只落一条
            if mid:
                dup = self.conn.execute(
                    "SELECT id FROM messages WHERE bot_id=? AND message_id=? ORDER BY id DESC LIMIT 1",
                    (str(bot_id), mid),
                ).fetchone()
                if dup:
                    return int(dup["id"] or 0)
            # 没有 message_id 时退回旧逻辑：10 秒内完全相同的只留一条
            if not mid:
                dup = self.conn.execute(
                    "SELECT id FROM messages WHERE bot_id=? AND umo=? AND uid=? AND role=? "
                    "AND content=? AND created_at>=? ORDER BY id DESC LIMIT 1",
                    (str(bot_id), str(umo), str(uid), role_n, text, ts - 10),
                ).fetchone()
                if dup:
                    return int(dup["id"] or 0)
            cur = self.conn.execute(
                "INSERT INTO messages(bot_id, umo, uid, name, role, content, created_at, platform, pid, message_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (str(bot_id), str(umo), str(uid), _clean(name, 64),
                 role_n, text, ts, platform, pid, mid),
            )
            self.conn.commit()
            return int(cur.lastrowid or 0)

    def messages_since(self, bot_id: str, uid: str = "", since_ts: int = 0,
                       limit: int = 200, since: int = None) -> List[Dict[str, Any]]:
        """取某个 uid 在 since_ts 之后说过/收到的消息（按时间正序）。

        给陪伴插件的「每 N 轮里程碑归档」用：只按 uid，跨平台；
        时间戳相同再按 id 排，保证顺序稳定。
        """
        if since is not None:  # 兼容面板/里程碑旧调用（since=）
            since_ts = int(since)
        where, params = _bot_filter(bot_id)
        sql = ("SELECT id, umo, uid, name, role, content, created_at, platform, pid "
               "FROM messages WHERE 1=1" + where + " AND uid=?")
        params.append(str(uid))
        if since_ts:
            sql += " AND created_at>?"
            params.append(int(since_ts))
        sql += " ORDER BY created_at ASC, id ASC LIMIT ?"
        params.append(max(1, min(int(limit), 2000)))
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def messages_between(self, bot_id: str, start_ts: int, end_ts: int,
                         uid: str = "", limit: int = 2000) -> List[Dict[str, Any]]:
        """取某个时间段内的聊天（给日记用：按天汇总，跨平台）。"""
        where, params = _bot_filter(bot_id)
        sql = ("SELECT id, umo, uid, name, role, content, created_at, platform, pid "
               "FROM messages WHERE 1=1" + where +
               " AND created_at>=? AND created_at<?")
        params.extend([int(start_ts), int(end_ts)])
        if uid:
            sql += " AND uid=?"
            params.append(str(uid))
        sql += " ORDER BY created_at ASC, id ASC LIMIT ?"
        params.append(max(1, min(int(limit), 5000)))
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    # ---------- 时间回看 ----------
    _CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
               "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    _PERIODS = (
        (("凌晨",), 0, 6),
        (("早上", "早晨", "上午"), 6, 12),
        (("中午",), 11, 14),
        (("下午",), 12, 18),
        (("傍晚", "晚上", "今晚", "昨晚", "夜里", "深夜"), 18, 24),
    )

    @classmethod
    def _cn_int(cls, text: str) -> int:
        """「6 / 六 / 十一 / 二十三」→ int；认不出返回 0。"""
        raw = str(text or "").strip()
        if raw.isdigit():
            return int(raw)
        if "十" in raw:
            left, _, right = raw.partition("十")
            tens = cls._CN_NUM.get(left, 1) if left else 1
            ones = cls._CN_NUM.get(right, 0) if right else 0
            return tens * 10 + ones
        return cls._CN_NUM.get(raw, 0)

    @classmethod
    def _day_window(cls, day, text: str):
        """一天的范围；话里带「下午/晚上」就只取那一段。"""
        from datetime import timedelta as _td
        for words, start_hour, end_hour in cls._PERIODS:
            if any(word in text for word in words):
                return day + _td(hours=start_hour), day + _td(hours=end_hour)
        return day, day + _td(days=1)

    @classmethod
    def _time_window(cls, query: str) -> tuple:
        """把「昨天下午 / 前天 / 上周三 / 11月6号晚上 / 最近3天」换成秒级时间窗。

        识别不了返回 (0, 0)；调用方据此返回空列表，避免「标签写上周三、
        内容却是今天的」这种更糟的错配。
        """
        from datetime import datetime as _dt, timedelta as _td
        text = str(query or "")
        now = _dt.now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day = None
        if "大前天" in text:
            day = today - _td(days=3)
        elif "前天" in text:
            day = today - _td(days=2)
        elif "昨天" in text or "昨晚" in text:
            day = today - _td(days=1)
        elif "今天" in text or "今晚" in text:
            day = today
        if day is not None:
            start, end = cls._day_window(day, text)
            return int(start.timestamp()), int(end.timestamp())

        # 上周三 / 这周五 / 礼拜天
        m = re.search(r"(上上|上|这|本)?\s*(周|星期|礼拜)\s*([一二三四五六日天1-7])", text)
        if m:
            idx = cls._cn_int(m.group(3)) - 1  # 周一=0
            if 0 <= idx <= 6:
                monday = today - _td(days=today.weekday())
                target = monday + _td(days=idx)
                if m.group(1) == "上":
                    target -= _td(days=7)
                elif m.group(1) == "上上":
                    target -= _td(days=14)
                elif target > today:
                    target -= _td(days=7)
                start, end = cls._day_window(target, text)
                return int(start.timestamp()), int(end.timestamp())

        # 11月6号 / 11月6号晚上
        m = re.search(r"(?:(\d{4})\s*年)?\s*(\d{1,2}|[一二三四五六七八九十]{1,3})\s*月"
                      r"\s*(\d{1,2}|[一二三四五六七八九十]{1,3})\s*[号日]?", text)
        if m:
            year = int(m.group(1)) if m.group(1) else today.year
            month = cls._cn_int(m.group(2)) or 1
            day_num = cls._cn_int(m.group(3)) or 1
            try:
                target = _dt(year, month, day_num)
            except ValueError:
                target = None
            if target is not None:
                if not m.group(1) and target > today:
                    try:
                        target = _dt(year - 1, month, day_num)
                    except ValueError:
                        target = None
            if target is not None:
                start, end = cls._day_window(target, text)
                return int(start.timestamp()), int(end.timestamp())

        # 六号 / 6号（没写月份：本月还没到就算上个月）
        m = re.search(r"(?<!\d)(\d{1,2}|[一二三四五六七八九十]{1,3})\s*[号日]", text)
        if m:
            day_num = cls._cn_int(m.group(1))
            if 1 <= day_num <= 31:
                try:
                    target = today.replace(day=day_num)
                except ValueError:
                    target = None
                if target is not None and target > today:
                    prev_month_end = today.replace(day=1) - _td(days=1)
                    try:
                        target = prev_month_end.replace(day=day_num)
                    except ValueError:
                        target = None
                if target is not None:
                    start, end = cls._day_window(target, text)
                    return int(start.timestamp()), int(end.timestamp())

        if "上个月" in text or "上月" in text:
            first = today.replace(day=1)
            start = (first - _td(days=1)).replace(day=1)
            return int(start.timestamp()), int(first.timestamp())
        if "去年" in text:
            start = _dt(today.year - 1, 1, 1)
            end = _dt(today.year, 1, 1)
            return int(start.timestamp()), int(end.timestamp())
        if "今年" in text:
            return int(_dt(today.year, 1, 1).timestamp()), int((now + _td(seconds=1)).timestamp())

        m = re.search(r"最近\s*(\d+)\s*(天|日|小时|分钟)", text)
        if m:
            amount = max(1, int(m.group(1)))
            if m.group(2) in ("小时", "分钟"):
                unit = _td(hours=1) if m.group(2) == "小时" else _td(minutes=1)
                return int((now - unit * amount).timestamp()), int((now + _td(seconds=1)).timestamp())
            start = today - _td(days=amount - 1)
            return int(start.timestamp()), int((today + _td(days=1)).timestamp())
        return 0, 0

    @classmethod
    def _time_label(cls, query: str) -> str:
        """给时间回看一个标题（amo 的 render 必须有 label，否则整段丢弃）。"""
        text = str(query or "")
        for word in ("大前天", "前天", "昨天下午", "昨天晚上", "昨天",
                     "今天下午", "今天晚上", "昨天凌晨", "今天凌晨", "昨晚", "今晚", "今天"):
            if word in text:
                return word
        for pattern in (r"(上上|上|这|本)?\s*(周|星期|礼拜)\s*[一二三四五六日天1-7]",
                        r"(?:\d{4}\s*年)?\s*\d{1,2}\s*月\s*\d{1,2}\s*[号日]?",
                        r"(?<!\d)(?:\d{1,2}|[一二三四五六七八九十]{1,3})\s*[号日]"):
            m = re.search(pattern, text)
            if m:
                label = m.group(0).strip()
                if label:
                    return label
        for word in ("上个月", "上月", "去年", "今年"):
            if word in text:
                return word
        m = re.search(r"最近\s*(\d+)\s*(天|日|小时|分钟)", text)
        if m:
            return "最近" + m.group(1) + m.group(2)
        return ""

    def recall_messages(self, bot_id: str, query: str = "", umo: str = "",
                        uid: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        """按时间回看的一段聊天（时间回看 / 每日总结都调它）。

        之前 amo 调 ``recall_messages``，这边只有 ``list_messages``，两边契约对不上，
        功能静默失效（amo 侧 callable=False 直接 return）。返回按时间正序。
        """
        limit = max(1, min(int(limit or 200), 1000))
        start, end = self._time_window(query)
        if not start or not end:
            return []
        # 时间窗过滤发生在 SQL 的 LIMIT 之后，候选池太小会把整段时间筛空
        # （比如「昨天」但最近 12 条全是今天的），所以先多取一些再筛。
        pool = min(1000, max(limit * 8, 400))
        rows = self.list_messages(bot_id, umo=umo, limit=pool, uid=uid)
        rows = [r for r in rows if start <= int(r.get("created_at") or 0) < end]
        rows = sorted(rows, key=lambda r: (int(r.get("created_at") or 0), int(r.get("id") or 0)))
        return rows[-limit:]


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

    # ---------- 时间检索：年 → 月日 → 小时 → 具体消息 ----------
    @staticmethod
    def _msg_filters(bot_id: str, umo: str = "", uid: str = "", pid: int = 0) -> Tuple[str, List[Any]]:
        where, params = _bot_filter(bot_id)
        sql = " WHERE 1=1" + where
        if umo:
            sql += " AND umo=?"
            params.append(str(umo))
        if uid:
            sql += " AND uid=?"
            params.append(str(uid))
        if pid:
            sql += " AND pid=?"
            params.append(int(pid))
        return sql, params

    def time_buckets(self, bot_id: str, umo: str = "", uid: str = "",
                     year: str = "", month: str = "", day: str = "",
                     start: int = 0, end: int = 0) -> Dict[str, Any]:
        """按层级给消息分桶：只给年→返回月，给年+月→返回日，给年月日→返回小时。

        只返回"哪一层有东西、各有多少条"，不返回正文——这是分层检索的前三级。
        """
        where, params = self._msg_filters(bot_id, umo, uid)
        sql = ("SELECT strftime('{fmt}', created_at, 'unixepoch', 'localtime') AS bucket, "
               "COUNT(*) AS n FROM messages") + where
        if start:
            sql += " AND created_at>=?"
            params.append(int(start))
        if end:
            sql += " AND created_at<?"
            params.append(int(end))
        if year:
            sql += " AND strftime('%Y', created_at, 'unixepoch', 'localtime')=?"
            params.append(str(year))
        if year and month:
            sql += " AND strftime('%m', created_at, 'unixepoch', 'localtime')=?"
            params.append(str(month))
        if year and month and day:
            sql += " AND strftime('%d', created_at, 'unixepoch', 'localtime')=?"
            params.append(str(day))
        if year and month and day:
            fmt = "%H"
        elif year and month:
            fmt = "%d"
        elif year:
            fmt = "%m"
        else:
            fmt = "%Y"
        sql += " GROUP BY bucket ORDER BY bucket"
        with self.lock:
            rows = self.conn.execute(sql.format(fmt=fmt), params).fetchall()
        return {str(row["bucket"]): int(row["n"]) for row in rows}

    def messages_in_span(self, bot_id: str, start: int, end: int,
                         umo: str = "", uid: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        """取时间区间内的消息（按时间正序）；正文不带时间戳。"""
        where, params = self._msg_filters(bot_id, umo, uid)
        sql = ("SELECT id, umo, uid, name, role, content, created_at, compressed, "
               "platform, pid FROM messages" + where + " AND created_at>=? AND created_at<?")
        params += [int(start), int(end)]
        sql += " ORDER BY created_at ASC, id ASC LIMIT ?"
        params.append(max(1, min(int(limit), 2000)))
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def hierarchical_messages(self, bot_id: str, span: Dict[str, Any],
                              umo: str = "", uid: str = "",
                              limit: int = 200) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """四级漏斗：年 → 月日 → 小时 → 具体消息，返回消息和每级的命中数。

        ``span`` 来自 ``time_query.parse_time_span``。年/月日/小时只是缩小范围，
        消息正文原样返回（不带时间戳），调用方需要的话自己拿 ``created_at``。
        """
        start = int(span.get("start") or 0)
        end = int(span.get("end") or 0)
        path: Dict[str, Any] = {"label": span.get("label"), "granularity": span.get("granularity")}
        try:
            start_dt = datetime.fromtimestamp(start) if start else None
        except (ValueError, OSError, OverflowError):
            start_dt = None
        if start_dt is None:
            return [], path

        # 下面四层只用来给面板/日志看「哪一层有东西」，**不能据此提前返回**：
        # 「最近3天」这类区间会跨月/跨年，起始那几天完全可能没聊天，后面几天有；
        # 按起始月/日判空会把整段记录丢掉（表现为明明聊过却答"没聊到什么"）。
        # 1) 年
        years = self.time_buckets(bot_id, umo, uid, start=start, end=end)
        path["years"] = years
        year = "%04d" % start_dt.year

        # 2) 月
        months = self.time_buckets(bot_id, umo, uid, year=year, start=start, end=end)
        path["months"] = months
        month = "%02d" % start_dt.month

        # 3) 日
        days = self.time_buckets(bot_id, umo, uid, year=year, month=month,
                                 start=start, end=end)
        path["days"] = days
        day = "%02d" % start_dt.day

        # 4) 小时
        hours = self.time_buckets(bot_id, umo, uid, year=year, month=month, day=day,
                                  start=start, end=end)
        path["hours"] = hours

        # 5) 具体消息（真正的结果以这一步为准）
        rows = self.messages_in_span(bot_id, start, end, umo=umo, uid=uid, limit=limit)
        path["messages"] = len(rows)
        return rows, path

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
                "SELECT id, umo, uid, name, role, content, created_at, pid FROM messages "
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
                # 摘要也要归属到人：按 uid 的时间回看（amo 的【按时间回看】）否则什么都取不到
                uid_counts: Dict[str, int] = {}
                for item in items:
                    value = str(item["uid"] or "")
                    if value:
                        uid_counts[value] = uid_counts.get(value, 0) + 1
                bucket_uid = max(uid_counts, key=uid_counts.get) if uid_counts else ""
                self.conn.execute(
                    "INSERT INTO messages(bot_id, umo, uid, name, role, content, created_at, "
                    "compressed, platform, pid) VALUES(?,?,?,?,?,?,?,1,?,?)",
                    (str(bot_id), umo, bucket_uid, "摘要", "summary", summary, stamp,
                     platform_of_umo(umo), int(items[0]["pid"] or 0)),
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


    def _display_name(self, bot_id: str, uid: str) -> str:
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

    # ---------- 备份 / 导出 / 导入 ----------
    _EXPORT_TABLES = ("memories", "profiles", "relations", "persons",
                      "person_links", "identity_alias")

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
                  "aliases": 0, "skipped": 0}
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
            except Exception:
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
        stem = "amo_data-%s" % datetime.now().strftime("%Y%m%d-%H%M%S")
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
