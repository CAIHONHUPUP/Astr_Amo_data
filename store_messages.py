# -*- coding: utf-8 -*-
"""消息 / 时间窗 / 分层总结 / 清理。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple
import re
import sqlite3

if TYPE_CHECKING:  # 只给类型检查看：包内相对导入
    from .store_util import (
        _bot_filter,
        _clean,
        _clock,
        _now,
        platform_label,
        platform_of_umo,
    )
else:  # 运行时：包模式优先，按文件直接加载时退回绝对导入
    try:
        from .store_util import (
            _bot_filter,
            _clean,
            _clock,
            _now,
            platform_label,
            platform_of_umo,
        )
    except ImportError:  # 直接按文件加载时
        from store_util import (
            _bot_filter,
            _clean,
            _clock,
            _now,
            platform_label,
            platform_of_umo,
        )


if TYPE_CHECKING:
    from .store_types import StoreLike as _StoreBase
else:  # 运行时用 object：协议里的声明不能进 MRO，否则会挡掉别的 mixin 的真实实现
    _StoreBase = object


class MessagesMixin(_StoreBase):
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
        elif role_n == "bot":
            # 她的话也要挂到这个人身上：跨平台「她在另一边说过什么」是按 pid 找的，
            # 以前 bot 行 pid=0，person_context 永远取不到她的回复。
            try:
                if ":FriendMessage:" in str(umo):
                    tail = str(umo).rsplit(":", 1)[-1].strip()
                    if tail and tail != "bot":
                        pid = self.resolve_person(bot_id, platform, tail)
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
                       limit: int = 200, since: Optional[int] = None,
                       umo: str = "") -> List[Dict[str, Any]]:
        """取 since_ts 之后的消息（按时间正序）。

        给陪伴插件的「每 N 轮里程碑归档」用：
        - 只给 ``uid``：这个人跨平台说过的话（旧行为）；
        - 给 ``umo``：**这个会话**的双方对话。机器人自己的行 uid='bot'，
          按 uid 过滤会把她的回复整段丢掉，所以里程碑要按会话取。
        两个都不给时返回该 bot 全部消息（调用方自己负责别跨人）。
        """
        if since is not None:  # 兼容面板/里程碑旧调用（since=）
            since_ts = int(since)
        where, params = self._msg_filters(bot_id, umo, uid)
        sql = ("SELECT id, umo, uid, name, role, content, created_at, platform, pid "
               "FROM messages" + where)
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
    @staticmethod
    def _epoch(dt):
        """naive 本地时间 → 秒；按插件配置的时区算，不跟服务器时区跑。"""
        try:
            zone = _clock.get_zone(_clock.current_name())
        except Exception:
            zone = None
        if zone is not None:
            try:
                return int(dt.replace(tzinfo=zone).timestamp())
            except Exception:
                pass
        try:
            return int(dt.timestamp())
        except Exception:
            return 0

    @classmethod
    def _time_window(cls, query: str) -> tuple:
        """把「昨天下午 / 前天 / 上周三 / 11月6号晚上 / 最近3天」换成秒级时间窗。

        识别不了返回 (0, 0)；调用方据此返回空列表，避免「标签写上周三、
        内容却是今天的」这种更糟的错配。
        """
        from datetime import datetime as _dt, timedelta as _td
        text = str(query or "")
        now = _clock.now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day = None
        target: Optional[_dt] = None
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
            return cls._epoch(start), cls._epoch(end)

        # 上周三 / 这周五 / 礼拜天
        m = re.search(r"(上上|上|这|本)?\s*(周|星期|礼拜)\s*([一二三四五六日天1-7])", text)
        if m:
            raw = m.group(3)
            # 周日/周天/礼拜天：_cn_int 只认数字和「一二三…」，这里补上「日/天 = 7」
            idx = (7 if raw in ("日", "天") else cls._cn_int(raw)) - 1  # 周一=0
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
                return cls._epoch(start), cls._epoch(end)

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
                return cls._epoch(start), cls._epoch(end)

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
                    return cls._epoch(start), cls._epoch(end)

        if "上个月" in text or "上月" in text:
            first = today.replace(day=1)
            start = (first - _td(days=1)).replace(day=1)
            return cls._epoch(start), cls._epoch(first)
        if "去年" in text:
            start = _dt(today.year - 1, 1, 1)
            end = _dt(today.year, 1, 1)
            return cls._epoch(start), cls._epoch(end)
        if "今年" in text:
            return cls._epoch(_dt(today.year, 1, 1)), cls._epoch(now + _td(seconds=1))

        m = re.search(r"最近\s*(\d+)\s*(天|日|小时|分钟)", text)
        if m:
            amount = max(1, int(m.group(1)))
            if m.group(2) in ("小时", "分钟"):
                unit = _td(hours=1) if m.group(2) == "小时" else _td(minutes=1)
                return cls._epoch(now - unit * amount), cls._epoch(now + _td(seconds=1))
            start = today - _td(days=amount - 1)
            return cls._epoch(start), cls._epoch(today + _td(days=1))
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
        """聊天 + 摘要混排，按真实聊天时间倒序（时间回看/召回的候选池用）。"""
        return self._list_rows(bot_id, umo, limit, pid, platform, uid, role=None)

    def list_messages_grouped(self, bot_id: str, umo: str = "", limit: int = 200,
                              pid: int = 0, platform: str = "", uid: str = "",
                              summary_limit: int = 100) -> List[Dict[str, Any]]:
        """面板「聊天」页用：聊天按时间倒序在前，摘要按时间倒序统一放最后。

        以前摘要和聊天混在一个 LIMIT 里按 created_at 排，摘要都是几天前的，
        最近 200/300 条全是聊天 → 摘要被挤出窗口，面板上几乎看不到摘要。
        现在分两次查：聊天取 limit 条，摘要另取 summary_limit 条拼在后面。
        """
        chats = self._list_rows(bot_id, umo, limit, pid, platform, uid, role="chat")
        summaries = self._list_rows(bot_id, umo, summary_limit, pid, platform, uid, role="summary")
        return chats + summaries

    def _list_rows(self, bot_id: str, umo: str, limit: int, pid: int, platform: str,
                   uid: str, role: str | None) -> List[Dict[str, Any]]:
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
        if role == "summary":
            sql += " AND role='summary'"
        elif role == "chat":
            sql += " AND role<>'summary'"
        # 按**真实聊天时间**倒序：摘要行是事后补写的（id 更大但 created_at 更早），
        # 只按 id 排会把「2026-11-08 的摘要」顶到最新聊天的上面，面板看着像时间乱序。
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["label"] = platform_label(str(item.get("platform") or ""))
            out.append(item)
        return out
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
        sql = ("SELECT strftime('{fmt}', created_at, 'unixepoch', '{tz}') AS bucket, "
               "COUNT(*) AS n FROM messages") + where
        if start:
            sql += " AND created_at>=?"
            params.append(int(start))
        if end:
            sql += " AND created_at<?"
            params.append(int(end))
        if year:
            sql += " AND strftime('%Y', created_at, 'unixepoch', '{tz}')=?"
            params.append(str(year))
        if year and month:
            sql += " AND strftime('%m', created_at, 'unixepoch', '{tz}')=?"
            params.append(str(month))
        if year and month and day:
            sql += " AND strftime('%d', created_at, 'unixepoch', '{tz}')=?"
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
            rows = self.conn.execute(sql.format(fmt=fmt, tz=_clock.sql_offset_modifier()), params).fetchall()
        return {str(row["bucket"]): int(row["n"]) for row in rows}
    def messages_in_span(self, bot_id: str, start: int, end: int,
                         umo: str = "", uid: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        """取时间区间内的消息（按时间正序）；正文不带时间戳。"""
        where, params = self._msg_filters(bot_id, umo, uid)
        sql = ("SELECT id, umo, uid, name, role, content, created_at, compressed, "
               "platform, pid FROM messages" + where + " AND created_at>=? AND created_at<?")
        params += [int(start), int(end)]
        # 超 limit 时要保留**最近**那段：ASC+LIMIT 返回窗口最早的一批，
        # 「昨天」40+ 条时晚上整段看不到（时间回看块只剩凌晨/上午）。
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 2000)))
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in reversed(rows)]
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
            start_dt = _clock.from_timestamp(start) if start else None
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
                day = _clock.from_timestamp(row["created_at"]).strftime("%Y-%m-%d")
                buckets.setdefault((str(row["umo"]), day), []).append(row)
            for (umo, day), items in buckets.items():
                speakers = []
                for item in items:
                    label = "她" if str(item["role"]) == "bot" else str(item["name"] or item["uid"] or "对方")
                    if label not in speakers:
                        speakers.append(label)
                # 以前只取前 40 条再截到 2000 字，然后把当天全部原文删掉：
                # 一天 140 条时至少 100 条正文永久丢失。现在按字数切多段摘要，一条都不丢。
                chunks: List[List[sqlite3.Row]] = []
                current: List[sqlite3.Row] = []
                used = 0
                for item in items:
                    piece = _clean(item["content"], 120)
                    if current and used + len(piece) > 1800:
                        chunks.append(current)
                        current, used = [], 0
                    current.append(item)
                    used += len(piece)
                if current:
                    chunks.append(current)
                # 摘要也要归属到人：按 uid 的时间回看（amo 的【按时间回看】）否则什么都取不到
                uid_counts: Dict[str, int] = {}
                for item in items:
                    value = str(item["uid"] or "")
                    if value:
                        uid_counts[value] = uid_counts.get(value, 0) + 1
                bucket_uid = max(uid_counts, key=lambda k: uid_counts[k]) if uid_counts else ""
                for chunk in chunks:
                    body = " / ".join(
                        ("她：" if str(item["role"]) == "bot" else str(item["name"] or "对方") + "：")
                        + _clean(item["content"], 120)
                        for item in chunk
                    )
                    summary = _clean(
                        "[%s 共 %d 条 · %s] %s"
                        % (day, len(items), "、".join(speakers), body), 4000
                    )
                    self.conn.execute(
                        "INSERT INTO messages(bot_id, umo, uid, name, role, content, created_at, "
                        "compressed, platform, pid) VALUES(?,?,?,?,?,?,?,1,?,?)",
                        (str(bot_id), umo, bucket_uid, "摘要", "summary", summary,
                         int(chunk[0]["created_at"]), platform_of_umo(umo),
                         int(chunk[0]["pid"] or 0)),
                    )
                ids = [int(item["id"]) for item in items]
                self.conn.executemany(
                    "DELETE FROM messages WHERE bot_id=? AND id=?",
                    [(str(bot_id), item_id) for item_id in ids],
                )
                counts["compressed"] += len(chunks)
            cur = self.conn.execute(
                "DELETE FROM messages WHERE bot_id=? AND created_at<?",
                (str(bot_id), expire),
            )
            counts["purged"] = max(0, cur.rowcount or 0)
            self.conn.commit()
        return counts
