# -*- coding: utf-8 -*-
"""时间检索：把「昨天下午」「上周三」「11月6号晚上10点」这类说法解析成时间区间，
再按「年 → 月日 → 小时 → 具体消息」四级往下缩小。

设计取舍：
- **不给每条消息打时间戳**：消息只存 ``created_at`` 时间戳本身，检索时用它做过滤；
  喂给模型的正文保持干净，时间只在必要时作为"这一天/这个小时"的标题出现一次；
- 分级是"从粗到细"的漏斗：先按年过滤，再按月日，再按小时，最后才取具体消息，
  避免一上来就全表扫 + 把所有消息都塞进上下文。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import re
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

if TYPE_CHECKING:  # 只给类型检查看：包内相对导入
    from . import clock as _clock
else:  # 运行时：包模式优先，按文件直接加载时退回绝对导入
    try:
        from . import clock as _clock
    except Exception:
        import clock as _clock

WEEKDAY_NAMES = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
    "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6,
}

# 时段名 -> (起始小时, 结束小时)，左闭右开
TIME_OF_DAY = [
    ("凌晨", 0, 5), ("半夜", 23, 24), ("清晨", 5, 8), ("早上", 5, 11),
    ("早晨", 5, 11), ("上午", 5, 11), ("中午", 11, 13), ("下午", 13, 18),
    ("傍晚", 18, 19), ("晚上", 19, 24), ("夜里", 19, 24), ("深夜", 22, 24),
]



CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_value(token: str) -> int:
    """把「六」「十一」「二十三」这种中文数字转成 int；认不出来返回 -1。"""
    token = str(token or "").strip()
    if not token:
        return -1
    if "十" not in token:
        value = 0
        for ch in token:
            if ch not in CN_DIGITS:
                return -1
            value = value * 10 + CN_DIGITS[ch]
        return value
    if token.count("十") > 1:
        return -1
    head, _, tail = token.partition("十")
    tens = CN_DIGITS.get(head, 1) if head else 1
    ones = 0
    for ch in tail:
        if ch not in CN_DIGITS:
            return -1
        ones = ones * 10 + CN_DIGITS[ch]
    return tens * 10 + ones


def normalize_numbers(text: str) -> str:
    """把「六号」「十一号」「十点」里的中文数字换成阿拉伯数字，方便后面统一解析。"""
    raw = str(text or "")

    def _sub(match):
        value = _cn_value(match.group(1))
        return str(value) if value >= 0 else match.group(0)

    return re.sub(r"([零〇一二三四五六七八九十两]{1,4})(?=\s*[月号日点时分])", _sub, raw)

def _day_range(moment: datetime) -> Tuple[datetime, datetime]:
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def parse_time_span(text: Any, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """解析查询里的时间说法。

    Returns:
        ``None`` 表示没提时间；否则返回::

            {
              "start": 起始时间戳, "end": 结束时间戳,
              "granularity": "year" | "month" | "day" | "hour",
              "label": "昨天下午", "year": 2026, "month": 11, "day": 9, "hour": 15
            }
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    now = now or _clock.now()
    # 先把「六号/十一点」这类中文数字换成阿拉伯数字，后面的正则就只认数字了
    raw = normalize_numbers(raw)

    # ---- 1) 相对天数 ----
    day_base = None
    for word, offset in (("大前天", 3), ("前天", 2), ("昨天", 1), ("今天", 0), ("今日", 0)):
        if word in raw:
            day_base = now - timedelta(days=offset)
            break

    # ---- 2) 最近 N 天 / N 小时 / N 分钟 ----
    match = re.search(r"最近\s*(\d+)\s*(天|日|小时|分钟)", raw)
    if match:
        number = int(match.group(1))
        unit = match.group(2)
        if unit in ("小时", "分钟"):
            delta = timedelta(hours=number) if unit == "小时" else timedelta(minutes=number)
            granularity = "hour" if unit == "小时" else "hour"
        else:
            delta = timedelta(days=number)
            granularity = "day"
        start = now - delta
        return _span(start, now, granularity, match.group(0), now)

    # ---- 3) 周几 ----
    match = re.search(r"(上上|上|这|本)?\s*(?:周|星期|礼拜)\s*([一二三四五六日天1-7])", raw)
    if match:
        prefix, day_name = match.group(1) or "", match.group(2)
        target_wd = WEEKDAY_NAMES.get(day_name, 0)
        week_start = now - timedelta(days=now.weekday())
        if prefix in ("上", "上上"):
            week_start -= timedelta(days=7 if prefix == "上" else 14)
        day_base = week_start + timedelta(days=target_wd)
        if not prefix and day_base.date() > now.date():
            # 周一问「周日我们聊了什么」：指的是上一个周日，不是还没到的那个，
            # 否则时间窗落在未来，回看永远空。
            day_base -= timedelta(days=7)

    # ---- 4) 年 ----
    year = None
    match = re.search(r"(前年|去年|今年|(\d{4})\s*年)", raw)
    if match:
        token = match.group(1)
        if token == "今年":
            year = now.year
        elif token == "去年":
            year = now.year - 1
        elif token == "前年":
            year = now.year - 2
        else:
            year = int(match.group(2))

    # ---- 5) 月 / 日 ----
    month = day = None
    month_only = False
    match = re.search(r"(\d{1,2})\s*月\s*(?:(\d{1,2})\s*[号日])?", raw)
    if match:
        month = int(match.group(1))
        if match.group(2):
            day = int(match.group(2))
        else:
            month_only = True  # 「11月」= 整月，不是「今天这一天」
    elif re.search(r"(\d{1,2})\s*[号日]", raw) and not re.search(r"(\d{1,2})\s*点", raw):
        day_match = re.search(r"(\d{1,2})\s*[号日]", raw)
        if day_match:
            day = int(day_match.group(1))

    if re.search(r"(上个月|上月)", raw):
        year = year or now.year
        month = (now.month - 2) % 12 + 1
        year = year if now.month > 1 else year - 1
        month_only = True
    elif re.search(r"(这个月|本月)", raw):
        year, month = year or now.year, now.month
        month_only = True

    # ---- 6) 具体小时 ----
    hour = None
    hour_next_day = False
    match = re.search(r"(\d{1,2})\s*点\s*(半)?", raw)
    if match:
        hour = int(match.group(1)) % 24

    # ---- 7) 时段 ----
    tod = None
    for word, start_h, end_h in TIME_OF_DAY:
        if word in raw:
            tod = (word, start_h, end_h)
            break
    if tod and hour is not None:
        word = str(tod[0])
        if word == "凌晨":
            # 「凌晨12点」= 0 点；「凌晨1点」就是 1 点
            hour = 0 if hour == 12 else hour
        elif word in ("深夜", "夜里"):
            # 「深夜1点」= 1 点，不能加 12；「深夜11点」= 23 点
            if hour == 12:
                hour, hour_next_day = 0, True
            elif 6 <= hour <= 11:
                hour += 12
        elif word in ("晚上", "今晚", "昨晚", "傍晚"):
            if hour == 12:
                hour, hour_next_day = 0, True
            elif hour < 12:
                hour += 12
        elif word == "中午":
            if 1 <= hour <= 2:
                hour += 12
        elif word == "下午":
            if hour < 12:
                hour += 12
        # 上午/早上/早晨：原样
    if hour is not None and tod is not None and day_base is None and not year and not month and day is None:
        # 只说了时段+几点（如「中午12点」）：默认今天
        day_base = now
    if tod and hour is None and tod[1] <= now.hour < tod[2] and (day_base is None and not year and not month):
        # 只说"下午"没说是哪天：默认今天
        day_base = now

    # ---- 组装 ----
    if year is not None or month is not None or day is not None or day_base is not None:
        base_day = day_base or now
        try:
            if month is not None:
                year = year or base_day.year
                day = day or (1 if month_only else base_day.day)
                target = base_day.replace(year=year, month=month, day=day)
            elif year is not None and day_base is None:
                if month_only:
                    start = datetime(year, month or 1, 1)
                    return _span(start, _next_month(start), "month", raw, now)
                # 只给了年：整年
                start = datetime(year, 1, 1)
                return _span(start, datetime(year + 1, 1, 1), "year", raw, now)
            else:
                target = base_day
                if day is not None:
                    target = base_day.replace(day=int(day))
        except ValueError:
            return None
    else:
        target = None

    if target is None:
        return None

    day_start, day_end = _day_range(target)
    if hour is not None:
        start = day_start + timedelta(hours=hour, days=1 if hour_next_day else 0)
        return _span(start, start + timedelta(hours=1), "hour", raw, now,
                     year=target.year, month=target.month, day=target.day, hour=hour)
    if tod:
        name, start_h, end_h = tod
        start = day_start + timedelta(hours=start_h)
        return _span(start, day_start + timedelta(hours=end_h), "hour", raw, now,
                     year=target.year, month=target.month, day=target.day,
                     hour=start_h if start_h == end_h - 1 else None)
    if month_only:
        return _span(day_start.replace(day=1), _next_month(day_start), "month", raw, now)
    return _span(day_start, day_end, "day", raw, now,
                 year=target.year, month=target.month, day=target.day)


def _next_month(moment: datetime) -> datetime:
    if moment.month == 12:
        return moment.replace(year=moment.year + 1, month=1, day=1)
    return moment.replace(month=moment.month + 1, day=1)


def _span(start: datetime, end: datetime, granularity: str, label: str,
          now: datetime, year=None, month=None, day=None, hour=None) -> Dict[str, Any]:
    return {
        "start": int(start.timestamp()),
        "end": int(end.timestamp()),
        "granularity": granularity,
        "label": label.strip() or granularity,
        "year": year, "month": month, "day": day, "hour": hour,
    }


def describe_span(span: Optional[Dict[str, Any]]) -> str:
    """把解析结果说成一句人话（面板/日志用）。"""
    if not span:
        return "没提时间"
    start = _clock.from_timestamp(span["start"])
    end = _clock.from_timestamp(span["end"])
    if span["granularity"] == "year":
        return "%d 年" % start.year
    if span["granularity"] == "month":
        return "%d 年 %d 月" % (start.year, start.month)
    if span["granularity"] == "hour":
        return "%d-%02d-%02d %02d:00-%02d:00" % (
            start.year, start.month, start.day, start.hour, end.hour % 24,
        )
    # 「最近3天」这种多日区间只给起始日期会让人以为只回看了一天
    try:
        days = (int(span["end"]) - int(span["start"])) / 86400.0
    except (KeyError, TypeError, ValueError):
        days = 1.0
    if days > 1.5:
        last = _clock.from_timestamp(int(span["end"]) - 1)
        return "%d-%02d-%02d ~ %d-%02d-%02d" % (
            start.year, start.month, start.day, last.year, last.month, last.day,
        )
    return "%d-%02d-%02d" % (start.year, start.month, start.day)
