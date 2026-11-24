"""Recover missing SXData index minutes without changing observed quotes or model inputs."""
from contextlib import contextmanager
from datetime import date, timedelta
import fcntl
import json
import math
import os
from pathlib import Path
import sqlite3
import time
from typing import NamedTuple

from pipeline.calendar import is_trading_day, previous_trading_day, shanghai_now
from pipeline.data_service import credential_matches, gate_state
from pipeline.primary_minutes import minute_points
from pipeline.stock_api import _Client, _Failure, _index_code
from relay_common import STATE
from relay_quotes import SOURCE, groups, publish

API = "idx_mins"
HEADLINE_REVISIT = 15 * 60
SCHEMA = """
CREATE TABLE IF NOT EXISTS minute_recovery (
    id TEXT NOT NULL, minute TEXT NOT NULL, source_stamp TEXT NOT NULL,
    price REAL NOT NULL, code TEXT NOT NULL, available_at TEXT NOT NULL,
    PRIMARY KEY (id,minute)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS minute_recovery_tasks (
    day TEXT NOT NULL, id TEXT NOT NULL, code TEXT NOT NULL,
    attempted REAL NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0, no_progress INTEGER NOT NULL DEFAULT 0,
    complete INTEGER NOT NULL DEFAULT 0, outcome TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY (day,id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS minute_recovery_last_response (
    day TEXT NOT NULL, id TEXT NOT NULL, received_at TEXT NOT NULL,
    response_rows INTEGER NOT NULL, valid_minutes INTEGER NOT NULL, added_minutes INTEGER NOT NULL,
    PRIMARY KEY (day,id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS minute_recovery_full_bars (
    day TEXT NOT NULL, id TEXT NOT NULL, received_at TEXT NOT NULL, PRIMARY KEY(day,id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS minute_recovery_pending ON minute_recovery_tasks(attempted,day,id) WHERE complete=0;
CREATE TABLE IF NOT EXISTS minute_recovery_meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
) WITHOUT ROWID;
"""


def meta(db, key, default=""):
    row = db.execute("SELECT value FROM minute_recovery_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(db, key, value):
    db.execute("INSERT OR REPLACE INTO minute_recovery_meta VALUES (?,?)", (key, str(value)))


def index_symbols(market):
    market = [row for row in market if isinstance(row, dict) and _index_code(row)]
    if not market:
        raise ValueError("Awaiting a valid relay market index")
    return groups({"market": market, "sectors": []}, {})["rt_idx_k"]


def seed_tasks(db, symbols, today):
    """Recover days monitored by the relay and enroll new indices on their first day."""
    through = meta(db, "seeded_through")
    prior = symbols
    if through:
        first = date.fromisoformat(through) + timedelta(days=1)
        if first < today:
            last_day = db.execute("SELECT MAX(day) FROM minute_recovery_tasks WHERE day<=?", (through,)).fetchone()[0]
            monitored = {r[0] for r in db.execute("SELECT id FROM minute_recovery_tasks WHERE day=?", (last_day,))}
            prior = {code: symbol for code, symbol in symbols.items() if symbol in monitored}
    else:
        dates = [today]
        for symbol in symbols.values():
            row = db.execute("SELECT minute FROM samples WHERE id=? ORDER BY minute LIMIT 1", (symbol,)).fetchone()
            if row:
                dates.append(date.fromisoformat(row[0][:10]))
        first = min(dates)
    insert = "INSERT OR IGNORE INTO minute_recovery_tasks(day,id,code) VALUES(?,?,?)"
    while first < today:
        if is_trading_day(first) is True:
            db.executemany(insert, ((first.isoformat(), symbol, code) for code, symbol in prior.items()))
        first += timedelta(days=1)
    day = today.isoformat()
    if is_trading_day(today) is True:
        existing = {r[0] for r in db.execute("SELECT id FROM minute_recovery_tasks WHERE day=?", (day,))}
        added = [(day, symbol, code) for code, symbol in symbols.items() if symbol not in existing]
        if added:
            db.executemany(insert, added)
    if through != day:
        set_meta(db, "seeded_through", day)
    db.commit()


class MinuteWindow(NamedTuple):
    expected: set
    bar_ends: set


def elapsed_minutes(day, now):
    """Target clock labels of finished observations and real bar endpoints the source may return."""
    if day > now.date().isoformat() or is_trading_day(day) is not True:
        return MinuteWindow(set(), set())
    last = 900 if day < now.date().isoformat() else now.hour * 60 + now.minute
    def label(m):
        return f"{day}T{m // 60:02d}:{m % 60:02d}"
    starts = [m for begin, end in ((570, 690), (780, 900)) for m in range(begin, min(end, last))]
    return MinuteWindow({label(m) for m in starts}, {label(m+1) for m in starts})


def seen_minutes(db, symbol, day):
    args = (symbol, day + "T09:30", day + "T15:00")
    rows = db.execute("""SELECT minute FROM samples WHERE id=? AND minute BETWEEN ? AND ?
                         UNION ALL SELECT minute FROM minute_recovery WHERE id=? AND minute BETWEEN ? AND ?""",
                      args + args)
    return {row[0] for row in rows}


def historical_bars_finished(db, symbol, day, slots, seen):
    # Opening clock ticks and end-labelled bars are different observations. A complete
    # historical response can leave 09:30 or 13:00 empty. Never shift a source timestamp.
    if not slots.bar_ends <= seen:
        return False
    if db.execute("SELECT 1 FROM minute_recovery_full_bars WHERE day=? AND id=?", (day, symbol)).fetchone():
        return True
    # Before full-bar receipts were recorded, verified rows were counted once per exact
    # source minute. If one response accounts for ALL possible observed labels, it
    # necessarily contained every bar endpoint. A bare 240-row count is insufficient.
    row = db.execute("SELECT valid_minutes FROM minute_recovery_last_response WHERE day=? AND id=?",
                     (day, symbol)).fetchone()
    return bool(row and row[0] == len(seen & (slots.bar_ends | slots.expected)))


def next_task(db, now):
    today = now.date().isoformat()
    # Alternate current and overdue work. Oldest attempted index wins inside each lane.
    lanes = ("older", "today") if meta(db, "last_lane") == "today" else ("today", "older")
    priority_day = meta(db, "evening_priority_day")
    prior = previous_trading_day(today, include_today=is_trading_day(today) is True and now.hour >= 19)
    if not prior or priority_day != prior.isoformat():
        priority_day = ""
    slots_by_day = {}
    covered = []
    task = None
    for lane in lanes:
        op = "=" if lane == "today" else "<"
        # The headline gets one due post-close slot per 15 minutes of current-day
        # work. All intervening turns retain oldest-attempted order; history keeps its lane.
        due = now.timestamp()
        close = now.replace(hour=15, minute=0, second=0, microsecond=0).timestamp()
        prioritize = lane == "today" and due >= close
        rows = db.execute(f"""SELECT day,id,code,no_progress FROM minute_recovery_tasks
                              WHERE complete=0 AND day {op} ? AND (retry_at<=? OR
                                (? AND id='sh000001' AND attempted<? AND outcome IN ('no_new_minutes','partial')))
                              ORDER BY CASE WHEN ? AND id='sh000001' AND (attempted<? OR attempted<=?) THEN 0 ELSE 1 END,
                                  CASE WHEN day=? THEN 0 ELSE 1 END,
                                  attempted,day,CASE WHEN id='sh000001' THEN 0 WHEN id<>code THEN 1 ELSE 2 END,id""",
                          (today, due, prioritize, close, prioritize, close, due-HEADLINE_REVISIT, priority_day)).fetchall()
        for day, symbol, code, no_progress in rows:
            if day not in slots_by_day:
                slots_by_day[day] = elapsed_minutes(day, now)
            slots = slots_by_day[day]
            seen = seen_minutes(db, symbol, day) if slots.expected else set()
            if slots.expected - seen:
                if lane == "older" and historical_bars_finished(db, symbol, day, slots, seen):
                    covered.append((True, due, "full_source_bars", day, symbol))
                    continue
                task = day, symbol, code, no_progress, slots, lane
                break
            covered.append((len(slots.expected) == 240, due + 60, "covered", day, symbol))
        if task:
            break
    # Do not hold a writer lock while scanning live observations, or upgrade an
    # active read transaction over concurrent relay commits. Observations only accumulate.
    if covered:
        with db:
            db.executemany("""UPDATE minute_recovery_tasks SET complete=?,retry_at=?,outcome=?
                              WHERE day=? AND id=?""", covered)
    return task


@contextmanager
def low_priority_client(token, borrow_slot):
    if not token or not credential_matches(token):
        raise _Failure("credential_mismatch", 3600, deferred=True)
    gate = Path(os.environ["RADAR_SERVICE_GATE_FILE"])
    with gate.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise _Failure("relay_busy", deferred=True) from None
        cooldown, gap, reason = gate_state(API)
        if cooldown:
            raise _Failure(reason, math.ceil(cooldown), deferred=True)
        # Realtime batches can run at five-second intervals versus six for history.
        # Briefly hold a free slot after sustained deferrals to let the gap expire.
        if gap > (6 if borrow_slot else 1.5):
            raise _Failure("relay_busy", deferred=True)
        yield _Client(token, {"max_requests": 1, "timeout_seconds": 4, "budget_seconds": 11,
                              "min_interval_seconds": 6}, shanghai_now())


def store_points(db, task, rows, received):
    day, symbol, code, _, slots, _ = task
    allowed = slots.expected | slots.bar_ends
    labels = set()
    added = 0
    for point in minute_points((r for r in rows if isinstance(r, dict)), code, day, received, "1min"):
        stamp = point["timestamp"]
        minute = stamp[:16]
        if minute not in allowed or stamp[16:] != ":00+08:00":
            continue
        labels.add(minute)
        cursor = db.execute("""INSERT OR IGNORE INTO minute_recovery(id,minute,source_stamp,price,code,available_at)
                            SELECT ?,?,?,?,?,? WHERE NOT EXISTS (
                                SELECT 1 FROM samples WHERE id=? AND minute=?)""",
                            (symbol, minute, stamp, point["price"], code, received.isoformat(), symbol, minute))
        added += cursor.rowcount
    if len(slots.bar_ends) == 240 and slots.bar_ends <= labels:
        db.execute("INSERT OR IGNORE INTO minute_recovery_full_bars VALUES(?,?,?)",
                   (day,symbol,received.isoformat()))
    return len(labels), added


def attempt(db, now, client_factory, clock):
    task = next_task(db, now)
    if task is None:
        return "waiting"
    day, symbol, code, no_progress, slots, lane = task
    latest = max(slots.bar_ends).replace("T", " ") + ":00"
    params = {"ts_code": code, "freq": "1min", "start_date": day + " 09:30:00", "end_date": latest, "limit": 300}
    try:
        busy_since = float(meta(db, "busy_since", "0"))
        borrow_slot = bool(busy_since and now.timestamp() - busy_since >= 300)
        with client_factory(borrow_slot) as client:
            started = clock()
            # Reserve the largest permitted wait up front in case the process stops during I/O.
            set_meta(db, "next_request", started.timestamp() + 66)
            set_meta(db, "busy_since", 0)
            db.commit()
            try:
                rows = client.request(API, params)
            finally:
                # The transport records its precise monotonic start after shared admission.
                # Keep two actual starts at least 60 seconds apart including a borrowed slot.
                finished = clock().timestamp()
                actual_start = getattr(client, "last", None)
                elapsed = max(0, time.monotonic() - actual_start) if actual_start is not None else 0
                set_meta(db, "next_request", finished - elapsed + 60)
                db.commit()
    except _Failure as e:
        received = clock()
        if e.deferred:
            if e.code == "relay_busy" and not busy_since:
                set_meta(db, "busy_since", received.timestamp())
                db.commit()
            if e.retry_seconds and e.code != "relay_busy":
                set_meta(db, "pause_until", received.timestamp() + e.retry_seconds)
                db.commit()
            return "cooldown" if e.code != "relay_busy" else "waiting"
        delay = max(60, e.retry_seconds)
        if e.code in ("rate_limited", "api_denied", "redirect_blocked", "credential_mismatch") or e.host:
            set_meta(db, "pause_until", received.timestamp() + delay)
        db.execute("""UPDATE minute_recovery_tasks SET attempted=?,attempts=attempts+1,retry_at=?,outcome=?
                      WHERE day=? AND id=?""", (received.timestamp(), received.timestamp()+delay, e.code, day, symbol))
        set_meta(db, "last_lane", lane)
        db.commit()
        return "cooldown" if float(meta(db, "pause_until", "0")) > received.timestamp() else "waiting"
    received = clock()
    with db:
        valid, added = store_points(db, task, rows, received)
        db.execute("""INSERT OR REPLACE INTO minute_recovery_last_response
                      (day,id,received_at,response_rows,valid_minutes,added_minutes) VALUES(?,?,?,?,?,?)""",
                   (day, symbol, received.isoformat(), len(rows), valid, added))
        remaining = len(slots.expected - seen_minutes(db, symbol, day))
        complete = not remaining and len(slots.expected) == 240
        misses = 0 if added else no_progress + 1
        # A provider may release historical minutes only after close. Leave its empty jobs pending.
        delay = 60 if not remaining else 300 if added else min(14400, 300 * 2**min(misses-1, 6))
        if day == received.date().isoformat() and symbol == "sh000001" and received.hour >= 15:
            delay = min(delay, HEADLINE_REVISIT)
        db.execute("""UPDATE minute_recovery_tasks SET attempted=?,retry_at=?,attempts=attempts+1,
                      no_progress=?,complete=?,outcome=? WHERE day=? AND id=?""",
                   (received.timestamp(), received.timestamp()+delay, misses, complete,
                    "covered" if not remaining else "partial" if added else "no_new_minutes", day, symbol))
        set_meta(db, "last_lane", lane)
    return "partial"


def session_rows(db, table, symbol, day):
    return db.execute(f"""SELECT source_stamp,price FROM {table}
        WHERE id=? AND minute BETWEEN ? AND ? ORDER BY minute""",
        (symbol, day+"T09:30", day+"T15:00")).fetchall()


def previous_session(db, symbols, today):
    previous = previous_trading_day(today)
    if not previous:
        return None
    day = previous.isoformat()
    eligible = sorted(symbols.values())
    observations = {symbol: rows for symbol in eligible
                    if (rows := session_rows(db, "samples", symbol, day))}
    recovered = {symbol: rows for symbol in eligible
                 if (rows := session_rows(db, "minute_recovery", symbol, day))}
    if not observations and not recovered:
        return None
    return {"trade_date": day, "series": observations, "recovered": recovered}


def project(db, output, symbols, now, state):
    day = now.date().isoformat()
    slots = elapsed_minutes(day, now)
    series = {}
    missing = 0
    # A consistent read snapshot across symbols. All writes above have already committed.
    db.execute("BEGIN")
    try:
        for symbol in sorted(symbols.values()):
            if slots.expected:
                missing += len(slots.expected - seen_minutes(db, symbol, day))
            # Publish already recovered bars until the day changes. A browser may still
            # hold the previous independent relay snapshot when a live observation arrives.
            rows = session_rows(db, "minute_recovery", symbol, day)
            if rows:
                series[symbol] = rows
        expected = len(slots.expected) * len(symbols)
        recovered = sum(map(len, series.values()))
        status = "ready" if expected and missing == 0 else "cooldown" if state == "cooldown" else "partial" if recovered else "waiting"
        payload = {"schema_version": 1, "source": "sxdata_minute_recovery", "trade_date": day,
                   "generated_at": now.isoformat(), "last_sample": max((r[-1][0] for r in series.values()), default=None),
                   "series": series, "status": status,
                   "coverage": {"symbols": len(symbols), "elapsed_minutes": len(slots.expected),
                                "expected_symbol_minutes": expected, "missing_symbol_minutes": missing,
                                "recovered_symbol_minutes": recovered}}
        if is_trading_day(now.date()) is False:
            archive = previous_session(db, symbols, now.date())
            if archive:
                payload["previous_session"] = archive
    finally:
        db.commit()
    publish(output, payload)
    return payload


def run(db, output, symbols, client_factory, clock=shanghai_now):
    now = clock()
    db.executescript(SCHEMA)
    if meta(db, "coverage_policy") != "clock_minute_v1":
        # Earlier workers also accepted end-labelled coverage. Recheck their completed
        # tasks against exact minutes without changing any received price or timestamp.
        with db:
            db.execute("UPDATE minute_recovery_tasks SET complete=0,retry_at=0 WHERE complete=1")
            set_meta(db, "coverage_policy", "clock_minute_v1")
    seed_tasks(db, symbols, now.date())
    if float(meta(db, "pause_until", "0")) > now.timestamp():
        state = "cooldown"
    elif float(meta(db, "next_request", "0")) > now.timestamp():
        state = "waiting"
    else:
        state = attempt(db, now, client_factory, clock)
    return project(db, output, symbols, clock(), state)


def main():
    private = STATE / "relay"
    database = private / "minute-observations.sqlite3"
    if not database.is_file():
        print("Awaiting relay observations", flush=True)
        return
    with (private / "minute-recovery.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        data = json.loads(SOURCE.read_text(encoding="utf-8"))
        symbols = index_symbols(data["market"])
        token = (Path(os.environ["CREDENTIALS_DIRECTORY"]) / "sxdata-token").read_text().strip()
        output = STATE / "quote-output/minute-recovery.json"
        with sqlite3.connect(database, timeout=3) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='samples'").fetchone():
                print("Awaiting relay sample table", flush=True)
                return
            payload = run(db, output, symbols, lambda borrow: low_priority_client(token, borrow))
        print(json.dumps({"trade_date": payload["trade_date"], "status": payload["status"], **payload["coverage"]}), flush=True)


if __name__ == "__main__":
    main()
