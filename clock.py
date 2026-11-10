# -*- coding: utf-8 -*-
"""统一时区：容器/服务器经常跑在 UTC，但插件的时间判断（问候时段、日记、天气、
「现在几点」、「今天/昨天」）必须按用户所在时区算。

做法是**进程级**的：``configure()`` 会设置 ``TZ`` 并调用 ``time.tzset()``，
这样 ``datetime.now()``、``.timestamp()``、``time.localtime()``、SQLite 的
``'localtime'`` 全部自动对齐，不会出现「一半代码按北京、一半按 UTC」的错位。
没有 ``tzset``（非 Unix）或 ``TZ`` 设置失败时，退回 ``zoneinfo`` 换算。

解析顺序（越靠前优先级越高）：
    1. 插件配置 ``timezone``（"auto"/空 = 跟随系统）
    2. 环境变量 ``TZ``
    3. ``/etc/timezone``
    4. 兜底 ``Asia/Shanghai``
"""

from __future__ import annotations

import os
import time as _time
from datetime import datetime, timedelta, tzinfo as _tzinfo

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - 没有 zoneinfo 时走固定偏移兜底
    ZoneInfo = None

DEFAULT_TIMEZONE = "Asia/Shanghai"
_state = {"name": ""}
_applied = {"ok": False, "name": "", "prev": None}


class _FixedOffset(_tzinfo):
    """没有 tzdata 时的最小实现：固定偏移。"""

    def __init__(self, hours):
        self._delta = timedelta(hours=hours)

    def utcoffset(self, dt):
        return self._delta

    def dst(self, dt):
        return timedelta(0)

    def tzname(self, dt):
        return "UTC%+g" % (self._delta.total_seconds() / 3600.0)


def normalize(name):
    text = str(name or "").strip()
    if not text or text.lower() in ("auto", "system", "follow", "local"):
        return ""
    return text


def _read_etc_timezone():
    try:
        with open("/etc/timezone", encoding="utf-8") as fp:
            return fp.read().strip()
    except Exception:
        return ""


def _system_name():
    # 环境变量优先：configure() 自己也会写 TZ，不能让 /etc/timezone 把它盖回去
    return os.environ.get("TZ") or _read_etc_timezone() or ""


def resolve_name(explicit=""):
    for candidate in (
        normalize(explicit),
        _state.get("name", ""),
        normalize(_system_name()),
        DEFAULT_TIMEZONE,
    ):
        if candidate:
            return candidate
    return ""


def get_zone(name):
    if not name:
        return None
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    if name in ("Asia/Shanghai", "Asia/Chongqing", "PRC", "CST-8", "Asia/Beijing"):
        return _FixedOffset(8)
    return None


def configure(name=""):
    """插件启动时调用一次：设置进程时区。返回实际生效的时区名。"""
    _state["name"] = normalize(name)
    target = resolve_name(_state["name"])
    prev = os.environ.get("TZ")
    try:
        os.environ["TZ"] = target
        _time.tzset()
        _applied.update({"ok": True, "name": target, "prev": prev})
    except Exception:
        _applied.update({"ok": False, "name": target, "prev": prev})
    return target


def restore():
    """把进程时区还原（测试用）。"""
    prev = _applied.get("prev")
    if _applied.get("ok") and prev is not None:
        os.environ["TZ"] = prev
        try:
            _time.tzset()
        except Exception:
            pass
    _applied.update({"ok": False, "name": "", "prev": None})
    _state["name"] = ""


def current_name():
    return resolve_name()


def applied():
    return bool(_applied.get("ok"))


def now(explicit=""):
    """目标时区下的「现在」（naive）。"""
    if not explicit and _applied.get("ok"):
        return datetime.now()
    zone = get_zone(resolve_name(explicit))
    if zone is None:
        return datetime.now()
    return datetime.now(zone).replace(tzinfo=None)


def today(explicit=""):
    return now(explicit).date()


def today_str(explicit=""):
    return now(explicit).strftime("%Y-%m-%d")


def utc_offset_minutes(explicit=""):
    zone = get_zone(resolve_name(explicit))
    if zone is None:
        try:
            return int(datetime.now().astimezone().utcoffset().total_seconds() // 60)
        except Exception:
            return 0
    try:
        return int(datetime.now(zone).utcoffset().total_seconds() // 60)
    except Exception:
        return 0


def sql_offset_modifier(explicit=""):
    """SQLite 用：``"+480 minutes"``，替代依赖服务器本地时区的 ``'localtime'``。"""
    return "%+d minutes" % utc_offset_minutes(explicit)


def from_timestamp(ts, explicit=""):
    zone = get_zone(resolve_name(explicit))
    value = float(ts or 0)
    if zone is None:
        return datetime.fromtimestamp(value)
    return datetime.fromtimestamp(value, zone).replace(tzinfo=None)
