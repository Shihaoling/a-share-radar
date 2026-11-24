"""Publish timestamped index observations from the site's existing quote fallbacks."""

from datetime import date, datetime, time, timedelta
import fcntl
import json
import re
import sys
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from global_indices import INDICES, SESSIONS, finite
from market_daily import OUTPUT, read_daily
from pipeline.calendar import shanghai_now
from relay_quotes import publish

TENCENT_SYMBOLS = {"HSI": "hkHSI", "HKTECH": "hkHSTECH"}
SINA_SYMBOLS = {"HKAH": "AHXH", "DJI": ".DJI", "SPX": ".INX", "IXIC": ".IXIC"}
TENCENT = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
SINA = {
    "HK": "https://quotes.sina.cn/hk/api/openapi.php/HK_MinlineService.getMinline",
    "US": "https://quotes.sina.cn/us/api/openapi.php/US_MinlineNService.getMinline",
}

# HKEX securities schedule CT/075/25 and NYSE's published holiday calendar.
# On an uncovered weekday the quote date still has to be confirmed by the source.
HOLIDAYS = {
    "HK": frozenset(("2026-01-01", "2026-02-17", "2026-02-18", "2026-02-19", "2026-04-03",
                     "2026-04-06", "2026-04-07", "2026-05-01", "2026-05-25", "2026-06-19",
                     "2026-07-01", "2026-10-01", "2026-10-19", "2026-12-25")),
    "US": frozenset(("2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
                     "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
                     "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
                     "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24")),
}
SHORT_DAYS = {
    "HK": frozenset(("2026-02-16", "2026-12-24", "2026-12-31", "2027-02-05",
                     "2027-12-24", "2027-12-31")),
    "US": frozenset(("2026-11-27", "2026-12-24", "2027-11-26")),
}


def session(market, day):
    if day.weekday() >= 5 or day.isoformat() in HOLIDAYS[market]:
        return ()
    short = day.isoformat() in SHORT_DAYS[market]
    if market == "US":
        return ((time(9, 30), time(13 if short else 16)),)
    if short:
        return ((time(9, 30), time(12, 10)),)
    return ((time(9, 30), time(12)), (time(13), time(16, 10)))


def due(market, now):
    local = now.astimezone(SESSIONS[market])
    clock = local.time()
    windows = session(market, local.date())
    if not windows:
        return False
    # A short post-close grace captures delayed feeds and the final tick.
    end = datetime.combine(local.date(), windows[-1][1], tzinfo=local.tzinfo)
    return any(start <= clock <= finish for start, finish in windows) or end <= local <= end + timedelta(minutes=20)


def series(market, provider, day_value, rows, now):
    try:
        day = date.fromisoformat(str(day_value))
    except ValueError:
        return None
    local = now.astimezone(SESSIONS[market])
    windows = session(market, day)
    if not windows or day > local.date() or day < local.date() - timedelta(days=14):
        return None
    points = {}
    for stamp, price in rows:
        if not isinstance(stamp, str):
            continue
        try:
            clock = time.fromisoformat(stamp)
            price = float(price)
        except (ValueError, TypeError):
            continue
        if clock.tzinfo is not None or not finite(price) or price <= 0:
            continue
        if not any(start <= clock <= end for start, end in windows):
            continue
        observed = datetime.combine(day, clock, tzinfo=local.tzinfo)
        if observed <= local:
            points[observed.isoformat()] = price
    if not points or (len(points) < 2 and day != local.date()):
        return None
    return {
        "date": day.isoformat(), "provider": provider,
        "windows": [[datetime.combine(day, edge, tzinfo=local.tzinfo).isoformat() for edge in pair] for pair in windows],
        "points": [[stamp, points[stamp]] for stamp in sorted(points)],
    }


def tencent_series(payload, symbol, now):
    if payload.get("code") != 0:
        return None
    entry = (payload.get("data") or {}).get(symbol, {}).get("data") or {}
    rows = []
    for line in entry.get("data", []):
        if isinstance(line, str) and (match := re.match(r"^(\d{2})(\d{2}) (\S+)", line)):
            rows.append((f"{match[1]}:{match[2]}", match[3]))
    return series("HK", "tencent", entry.get("date"), rows, now)


def sina_series(payload, market, now):
    result = payload.get("result") or {}
    if (result.get("status") or {}).get("code") != 0:
        return None
    entries = []
    for rows in result.get("data") or []:
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            continue
        points = ((row.get("m"), row.get("price")) for row in rows if isinstance(row, dict))
        if entry := series(market, "sina", rows[0].get("date"), points, now):
            try:
                previous = float(rows[0].get("prevclose"))
            except (TypeError, ValueError):
                previous = None
            if finite(previous) and previous > 0:
                entry["previous_close"] = previous
            entries.append(entry)
    return max(entries, key=lambda row: row["date"], default=None)


def fetch(url, key, symbol, referer):
    query = urlencode({key: symbol})
    request = Request(f"{url}?{query}", headers={"User-Agent": "Mozilla/5.0", "Referer": referer})
    with urlopen(request, timeout=7) as response:
        raw = response.read(800_001)
    payload = json.loads(raw) if len(raw) <= 800_000 else None
    if not isinstance(payload, dict):
        raise ValueError("unexpected quote payload")
    return payload


def read_series(code, now):
    if symbol := TENCENT_SYMBOLS.get(code):
        return tencent_series(fetch(TENCENT, "code", symbol, "https://finance.qq.com/"), symbol, now)
    symbol = SINA_SYMBOLS[code]
    market = INDICES[code][1]
    return sina_series(fetch(SINA[market], "symbol", symbol, "https://finance.sina.com.cn/"), market, now)


def merge_series(old, new):
    if not old or new["date"] > old["date"]:
        return new
    if new["date"] < old["date"] or new["provider"] != old["provider"]:
        return old
    points = dict(old["points"])
    points.update(new["points"])
    return {**old, **new, "points": [[stamp, points[stamp]] for stamp in sorted(points)]}


def with_previous_close(entry, market, history):
    if finite(entry.get("previous_close")) and entry["previous_close"] > 0:
        return entry
    previous = date.fromisoformat(entry["date"]) - timedelta(days=1)
    while not session(market, previous):
        previous -= timedelta(days=1)
    stamp = previous.isoformat()
    for bar in reversed(history):
        close = bar.get("close")
        if bar.get("date") == stamp and bar.get("complete") is not False and finite(close) and close > 0:
            return {**entry, "previous_close": close}
    return entry


def run(now, seed=False):
    markets = {market for market in SESSIONS if seed or due(market, now)}
    updates = {}
    for code in (*TENCENT_SYMBOLS, *SINA_SYMBOLS):
        if INDICES[code][1] not in markets:
            continue
        try:
            if entry := read_series(code, now):
                updates[code] = entry
        except (URLError, OSError, ValueError, TypeError, AttributeError) as error:
            print(f"{code} 分时暂不可用 ({type(error).__name__})", flush=True)
    if updates:
        with OUTPUT.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            daily = read_daily()
            intraday = daily.setdefault("global_intraday", {})
            for code, saved in {**intraday, **updates}.items():
                if code not in INDICES:
                    continue
                entry = merge_series(intraday.get(code), saved)
                history = daily.get("global_indices", {}).get(code, {}).get("history", [])
                intraday[code] = with_previous_close(entry, INDICES[code][1], history)
            publish(OUTPUT, daily)
    if markets:
        print(json.dumps({"global_intraday": {code:[row["date"],len(row["points"])] for code,row in updates.items()}}))


if __name__ == "__main__":
    run(shanghai_now(), seed=sys.argv[1:] == ["--seed"])
