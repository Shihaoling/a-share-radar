"""Incremental ETF close-price history from the built-in SXData fund_daily API.

One request per trading day returns the whole market snapshot; results are cached
by date so later runs only fetch the newest session. No external data source is
contacted and the shared request gate still applies to every call.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, datetime, time as clock
from pathlib import Path
from tempfile import NamedTemporaryFile

from pipeline.calendar import is_trading_day, previous_trading_day, shanghai_now
from pipeline.funds import _Client as FundClient, _Failure, _positive

STATE = Path("/var/lib/ashare-radar-preview")
FUNDS_SNAPSHOT = STATE / "request-usage/public-funds.json"
OUTPUT = STATE / "request-usage/public-fund-history.json"
HISTORY_DAYS = 40
MAX_PER_RUN = 40
ENDPOINT = "http://tus.sxdata.top/tu_share/"
CODE = re.compile(r"^\d{6}\.(?:SH|SZ)$")
CFG = {"max_requests": 1, "min_interval_seconds": 3, "budget_seconds": 50, "timeout_seconds": 12}
BACKFILL_BUDGET = 480
MAX_RATE_RETRIES = 2
NOTES = [
    "历史收盘价来自本站内置 SXData/Tushare 兼容接口 fund_daily，按交易日缓存。",
    "走势图只展示场内收盘价，不含净值、分红再投与货币时间价值。",
    "仅供站内数据整理，不构成投资建议。",
]


def publish(path, payload):
    raw = (json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_bytes() == raw:
        return
    with NamedTemporaryFile(dir=path.parent, prefix="fund-history-", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(raw)
            handle.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def read(path, fallback=None):
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return fallback if fallback is not None else {}
    return value if isinstance(value, dict) else (fallback if fallback is not None else {})


def eligible_codes(snapshot):
    """Only funds already published on the site get a history series."""
    if not isinstance(snapshot, dict):
        return set()
    return {row["code"] for row in snapshot.get("funds", [])
            if isinstance(row, dict) and isinstance(row.get("code"), str)
            and CODE.fullmatch(row["code"])}


def target_days(now=None, count=HISTORY_DAYS):
    """Return the most recent completed trading days, oldest first."""
    now = shanghai_now(now)
    today = now.date()
    completed = is_trading_day(today) is True and now.time().replace(tzinfo=None) >= clock(15, 30)
    cursor = today if completed else previous_trading_day(today)
    days = []
    while cursor and len(days) < max(1, int(count)):
        days.append(cursor.isoformat() if isinstance(cursor, date) else str(cursor))
        cursor = previous_trading_day(cursor)
    return list(reversed(days))


def _iso(value):
    value = str(value or "")
    return f"{value[:4]}-{value[4:6]}-{value[6:]}" if re.fullmatch(r"\d{8}", value) else None


def clean_day(rows, day, codes):
    """Validate a fund_daily snapshot and keep only published, positive closes."""
    if not isinstance(rows, list) or not isinstance(codes, set):
        return {}
    closes = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = row.get("ts_code")
        if not isinstance(code, str) or code not in codes or code in closes:
            continue
        if _iso(row.get("trade_date")) != day:
            continue
        price = _positive(row.get("close"))
        if price is not None:
            closes[code] = round(price, 6)
    return closes


def build_payload(days, generated_at, status, errors=None):
    ordered = sorted((row for row in days if isinstance(row, dict)
                      and isinstance(row.get("date"), str) and isinstance(row.get("close"), dict)
                      and row["close"]), key=lambda row: row["date"])[-HISTORY_DAYS:]
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "source": "sxdata_fund_api",
        "provider": "stock_api",
        "endpoint": ENDPOINT,
        "status": status,
        "days": ordered,
        "errors": list(errors or [])[:20],
        "notes": NOTES,
    }


def load_token():
    """systemd exposes the SXData key as a credential file, not an environment variable."""
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        return
    try:
        token = (Path(directory) / "sxdata-token").read_text().strip()
    except OSError:
        return
    if token:
        os.environ["STOCK_API_TOKEN"] = token


def collect(now=None, *, snapshot=None, client_factory=None, max_requests=MAX_PER_RUN, output=OUTPUT):
    """Fetch missing daily closes and atomically publish the cached history."""
    load_token()
    now = shanghai_now(now)
    stamp = now.isoformat(timespec="seconds")
    snapshot = read(FUNDS_SNAPSHOT) if snapshot is None else snapshot
    codes = eligible_codes(snapshot)
    old = read(output)
    days = [row for row in old.get("days", []) if isinstance(row, dict)
            and isinstance(row.get("date"), str) and isinstance(row.get("close"), dict)]
    existing = {row["date"]: row for row in days}
    targets = target_days(now)
    if not codes:
        print(json.dumps({"published": stamp, "status": "pending", "reason": "no_funds_snapshot"}, ensure_ascii=False), flush=True)
        return "pending"
    token = os.environ.get("STOCK_API_TOKEN", "").strip()
    if not token:
        print(json.dumps({"published": stamp, "status": "pending", "reason": "not_configured"}, ensure_ascii=False), flush=True)
        return "pending"
    missing = [day for day in targets if day not in existing]
    # Newest first: a rate limit on an older date must not block the latest chart point.
    todo = list(reversed(missing))[:max(0, int(max_requests))]
    factory = client_factory or (lambda cfg_now: FundClient(token, CFG, cfg_now))
    errors = []
    fetched = 0
    deadline = time.monotonic() + BACKFILL_BUDGET
    for day in todo:
        rows = None
        attempts = 0
        while True:
            try:
                client = factory(now)
                rows = client.request("fund_daily", {"trade_date": day.replace("-", "")})
                break
            except _Failure as exc:
                wait = min(max(0, exc.retry_seconds), 90)
                if (exc.code == "rate_limited" and attempts < MAX_RATE_RETRIES and wait > 0
                        and time.monotonic() + wait < deadline):
                    attempts += 1
                    time.sleep(wait + 2)
                    continue
                errors.append(f"{day}:{exc.code}")
                rows = None
                break
            except (OSError, ValueError, TypeError):
                errors.append(f"{day}:invalid_response")
                rows = None
                break
        if rows is None:
            # A cooldown or gate rejection applies to every later date as well.
            break
        closes = clean_day(rows, day, codes)
        if not closes:
            errors.append(f"{day}:empty_or_invalid")
            continue
        existing[day] = {"date": day, "close": closes}
        fetched += 1
    remaining = [day for day in targets if day not in existing]
    status = "success" if not remaining and not errors else "partial" if existing else "error"
    payload = build_payload(existing.values(), stamp, status, errors)
    publish(Path(output), payload)
    print(json.dumps({"published": stamp, "status": status, "fetched": fetched,
                      "days": len(payload["days"]), "errors": len(errors)}, ensure_ascii=False), flush=True)
    return status


def main():
    collect()


if __name__ == "__main__":
    main()
