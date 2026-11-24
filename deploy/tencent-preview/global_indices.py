"""Publish completed Hong Kong and U.S. index sessions from the primary feed."""

from datetime import date, timedelta
import math
from zoneinfo import ZoneInfo

from pipeline.stock_api import _Failure

INDICES = {
    "HSI": ("恒生指数", "HK"),
    "HKTECH": ("恒生科技指数", "HK"),
    "HKAH": ("恒生 AH 股 H 指数", "HK"),
    "DJI": ("道琼斯工业指数", "US"),
    "SPX": ("标普500指数", "US"),
    "IXIC": ("纳斯达克综合指数", "US"),
}
SESSIONS = {"HK": ZoneInfo("Asia/Hong_Kong"), "US": ZoneInfo("America/New_York")}
HISTORY_DAYS = 395
MAX_BARS = 270


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def completed_bars(rows, code, now):
    _, market = INDICES[code]
    local = now.astimezone(SESSIONS[market])
    end = local.date() - timedelta(days=int(local.hour < 16))
    start = end - timedelta(days=HISTORY_DAYS)
    bars = {}
    for row in rows:
        raw, close = row.get("trade_date"), row.get("close")
        if row.get("ts_code") != code or not finite(close) or close <= 0:
            continue
        try:
            day = date.fromisoformat(str(raw))
        except ValueError:
            continue
        if not start <= day <= end or day.weekday() >= 5:
            continue
        change = row.get("pct_chg")
        previous = row.get("pre_close")
        if not finite(change):
            change = (close / previous - 1) * 100 if finite(previous) and previous > 0 else None
        iso = day.isoformat()
        bars[iso] = {"date": iso, "close": close, "change_pct": change, "complete": True}
    return bars


def collect(now, request, existing, *, on_error=print):
    updates = {}
    for code, (_, market) in INDICES.items():
        end = now.astimezone(SESSIONS[market]).date()
        cached = existing.get(code, {}).get("history", [])
        if cached and len(cached) >= 200:
            start = date.fromisoformat(cached[-1]["date"]) - timedelta(days=7)
        else:
            start = end - timedelta(days=HISTORY_DAYS)
        try:
            rows = request("index_global", {"ts_code": code, "start_date": start.strftime("%Y%m%d"),
                                            "end_date": end.strftime("%Y%m%d")})
        except _Failure as error:
            on_error(f"index_global {code}: {error.code}")
            continue
        if bars := completed_bars(rows, code, now):
            updates[code] = bars
    return updates


def merge(existing, updates):
    result = dict(existing)
    for code, bars in updates.items():
        name, market = INDICES[code]
        history = {row["date"]: row for row in existing.get(code, {}).get("history", [])}
        history.update(bars)
        result[code] = {"id": code, "name": name, "market": market, "provider": "stock_api",
                        "history": [history[day] for day in sorted(history)[-MAX_BARS:]]}
    return result
