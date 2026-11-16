"""Conservative Shanghai exchange calendar; unknown years are never guessed.

Source: https://www.sse.com.cn/disclosure/dealinstruc/closed/ (2026).
Naive datetimes are interpreted as Asia/Shanghai, never in the host timezone.
"""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
KNOWN_YEARS = {2026}
_HOLIDAY_RANGES = (
    ("2026-01-01", "2026-01-03"), ("2026-02-15", "2026-02-23"),
    ("2026-04-04", "2026-04-06"), ("2026-05-01", "2026-05-05"),
    ("2026-06-19", "2026-06-21"), ("2026-09-25", "2026-09-27"),
    ("2026-10-01", "2026-10-07"),
)
HOLIDAYS = set()
for _start, _end in _HOLIDAY_RANGES:
    _day, _last = date.fromisoformat(_start), date.fromisoformat(_end)
    while _day <= _last:
        HOLIDAYS.add(_day)
        _day += timedelta(days=1)


def shanghai_now(now=None):
    now = now or datetime.now(SHANGHAI)
    return now.replace(tzinfo=SHANGHAI) if now.tzinfo is None else now.astimezone(SHANGHAI)


def is_trading_day(day):
    """Return None when the published calendar does not cover the year."""
    if isinstance(day, datetime):
        day = shanghai_now(day).date()
    if isinstance(day, str):
        day = date.fromisoformat(day)
    if day.year not in KNOWN_YEARS:
        return None
    return day.weekday() < 5 and day not in HOLIDAYS


def _adjacent(day, step, include_today=False):
    if isinstance(day, str):
        day = date.fromisoformat(day)
    if not include_today:
        day += timedelta(days=step)
    for _ in range(40):
        available = is_trading_day(day)
        if available is None:
            return None
        if available:
            return day
        day += timedelta(days=step)
    return None


def next_trading_day(day, include_today=False):
    return _adjacent(day, 1, include_today)


def previous_trading_day(day, include_today=False):
    return _adjacent(day, -1, include_today)


def market_session(now=None):
    now = shanghai_now(now)
    day, clock = now.date(), now.time().replace(tzinfo=None)
    opened = is_trading_day(day)
    if opened is None:
        return {"phase": "unknown", "label": "该年度交易日历尚未核实，暂停预测", "target_date": None, "target_kind": "next_open", "is_trading_day": None}
    if not opened:
        phase, label, target = "closed", "休市", next_trading_day(day)
        kind = "next_open"
    elif clock < time(9, 25):
        phase = "pre_open" if clock < time(9, 15) else "opening_auction"
        label = "盘前" if phase == "pre_open" else "开盘集合竞价"
        target, kind = day, "next_open"
    elif clock < time(15):
        target, kind = day, "today_close"
        if clock < time(9, 30):
            phase, label = "auction_complete", "开盘竞价结束，等待连续交易"
        elif clock < time(11, 30):
            phase, label = "morning", "上午交易中"
        elif clock < time(13):
            phase, label = "midday_break", "午间休市，仍预测今日收盘"
        elif clock < time(14, 57):
            phase, label = "afternoon", "下午交易中"
        else:
            phase, label = "closing_auction", "收盘集合竞价"
    else:
        phase, label, target, kind = "post_close", "已收盘", next_trading_day(day), "next_open"
    if target is None:
        label += "；下一交易日历未核实，暂停预测"
    return {"phase": phase, "label": label, "target_date": target.isoformat() if target else None, "target_kind": kind, "is_trading_day": opened}
