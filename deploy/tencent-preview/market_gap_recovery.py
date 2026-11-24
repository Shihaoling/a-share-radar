"""Audit the last completed market session and repair gaps with dated SXData data."""

from argparse import ArgumentParser
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, timedelta
import fcntl
import json
from itertools import zip_longest
import math
import os
from pathlib import Path
import re
import sqlite3
import time

from pipeline.calendar import is_trading_day, previous_trading_day, shanghai_now
from pipeline.stock_api import FIELDS, _Failure, _index_code
from asset_research import ResearchStore
from factor_history_jobs import Job, save_rows
from minute_recovery import SCHEMA as MINUTE_SCHEMA, elapsed_minutes, index_symbols, seen_minutes, set_meta
from relay_common import STATE, relay_client
from relay_quotes import SOURCE, publish
from research_valuation import APIS, normalized_rows, save_snapshot, snapshot_name
from sector_directory import mapping, publish_reference, reference

EXCHANGE_STOCK = re.compile(r"[0-9]{6}\.(?:SH|SZ|BJ)")
EXACT_LIMIT = 18
LONG_UNPUBLISHED_DAYS = 30


def session(now, mode):
    if mode == "session":
        return now.date() if now.hour >= 19 and is_trading_day(now.date()) is True else None
    return previous_trading_day(now.date(), include_today=now.hour >= 19)


def inspect(db, state, day):
    directory = mapping(state/"sector-directory/stock-names.json")
    equity_codes = {code for code in directory if EXCHANGE_STOCK.fullmatch(code)}
    assets = defaultdict(set)
    catalog_sectors = set()
    for kind, code in db.execute("SELECT kind,code FROM assets WHERE kind IN ('index','sector')"):
        if kind == "index":
            assets["index"].add(code)
        elif code.endswith((".SH",".SZ")):
            assets["exchange_sector"].add(code)
        elif code.endswith(".SI"):
            assets["sw"].add(code)
        elif code.endswith(".TI"):
            catalog_sectors.add(code)
    assets["ths"] = catalog_sectors
    assets["stock"] = equity_codes
    present = defaultdict(set)
    for kind, code in db.execute("SELECT kind,code FROM bars WHERE day=? AND close>0", (day,)):
        if kind != "sector":
            present[kind].add(code)
        elif code.endswith((".SH",".SZ")):
            present["exchange_sector"].add(code)
        elif code.endswith(".SI"):
            present["sw"].add(code)
        elif code.endswith(".TI"):
            present["ths"].add(code)
    absence = {key: assets[key] - present[key] for key in ("index","exchange_sector","sw","ths","stock")}
    dormant = {row[0] for row in db.execute("""SELECT a.code,max(b.day) FROM assets a JOIN bars b ON
              a.kind=b.kind AND a.code=b.code WHERE a.kind='sector' AND a.code LIKE '%.TI'
              GROUP BY a.code HAVING max(b.day) < ?""",
              ((date.fromisoformat(day)-timedelta(days=LONG_UNPUBLISHED_DAYS)).isoformat(),))} & absence["ths"]
    return assets, present, absence, dormant


def missing_valuation(output, assets, present, day):
    expected = {"stock": assets["stock"] & present["stock"], "index": assets["index"], "sector": assets["sw"]}
    missing = {}
    for kind, codes in expected.items():
        snapshot = mapping(output/snapshot_name(kind))
        day_items = snapshot.get("items", {}) if snapshot.get("trade_date") == day else {}
        missing[kind] = codes - day_items.keys()
    return missing


def confirmed_suspensions(rows, day):
    compact = day.replace("-", "")
    return {row["ts_code"] for row in (rows or ()) if
            row.get("trade_date") == compact and row.get("suspend_type") == "S"
            and row.get("suspend_timing") in (None,"") and EXCHANGE_STOCK.fullmatch(str(row.get("ts_code","")))}


def save_daily(db, kind, api, codes, day, rows):
    matched = [row for row in (rows or ()) if row.get("ts_code") in codes]
    if matched:
        with db:
            save_rows(ResearchStore, db, Job(api, kind+"/evening", kind, codes, {day}, ()), matched)


def publish_closes(db, state, source, day, now):
    output = state/"quote-output"
    path = output/"daily-market.json"
    aliases = {_index_code(row):row["id"] for row in source["market"] if _index_code(row)}
    rows = db.execute("""SELECT kind,code,close,pct_chg,pre_close FROM bars WHERE day=? AND
                    (kind='index' OR (kind='sector' AND code LIKE '%.SI'))""", (day,)).fetchall()
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        old = mapping(path)
        if old.get("trade_date", "") <= day:
            all_bars = old.setdefault("bars", {})
            additions = 0
            for kind, code, price, change, previous in rows:
                ident = aliases.get(code, code) if kind == "index" else code
                by_day = all_bars.setdefault(ident, {})
                if day in by_day or change is None or not math.isfinite(change):
                    continue
                by_day[day] = {"date":day, "close":price, "pre_close":previous, "change_pct":change,
                               "as_of":day+"T15:00:00+08:00", "source":"stock_api",
                               "provider":"stock_api", "available_at":now.isoformat(),
                               "timestamp_quality":"daily_close"}
                for old_day in sorted(by_day)[:-250]:
                    del by_day[old_day]
                additions += 1
            if additions:
                old.update(schema_version=1, source="sxdata_daily", trade_date=day)
                old.setdefault("stocks", {})
                old.setdefault("market_caps", {})
                publish(path, old)
    path = output/"sector-reference.json"
    old = mapping(path)
    published = old.get("quotes", {}) if old.get("trade_date") == day else {}
    ths = db.execute("""SELECT code,close,pct_chg FROM bars WHERE day=? AND kind='sector'
                      AND code LIKE '%.TI' AND source='ths_daily'""", (day,))
    official = [{"ts_code": code, "trade_date": day.replace("-", ""), "close": price, "pct_change": change}
                for code, price, change in ths if code not in published]
    if official:
        publish_reference(path, reference(official, day, now, {r["ts_code"] for r in official}))


def minute_status(state, source, day, now):
    path = state/"relay/minute-observations.sqlite3"
    if not path.is_file():
        return {"status":"not_ready"}
    symbols = index_symbols(source["market"])
    with closing(sqlite3.connect(path, timeout=8)) as db:
        db.executescript(MINUTE_SCHEMA)
        slots = elapsed_minutes(day, now)
        gaps = {symbol:len(slots.expected-seen_minutes(db,symbol,day)) for symbol in symbols.values()}
        missing = {code:count for code,count in gaps.items() if count}
        if missing:
            with db:
                db.executemany("INSERT OR IGNORE INTO minute_recovery_tasks(day,id,code) VALUES(?,?,?)",
                               ((day,symbol,code) for code,symbol in symbols.items() if symbol in missing))
                set_meta(db, "evening_priority_day", day)
    return {"symbols":len(symbols), "remaining_clock_minutes":sum(missing.values()), "remaining_by_symbol":missing}


def run(state, source, day, now, request):
    private = state/"asset-research"
    output = state/"quote-output"
    compact = day.replace("-", "")
    tries = Counter()
    deadline = time.monotonic()+540
    backoff = {}
    with closing(sqlite3.connect(private/"research.sqlite3", timeout=12)) as db:
        db.row_factory = sqlite3.Row
        report = mapping(private/"evening-gap-report.json")
        previous = report.get("last_exact", {}) if report.get("trade_date") == day else {}
        attempts = dict(previous)
        def fetch(api, params):
            stamp = time.monotonic()
            if stamp+26 >= deadline or stamp < max(backoff.get(api,0),backoff.get("*",0)):
                tries[api+":deferred_to_next_check"] += 1
                return None
            for retry in range(2):
                try:
                    rows = request(api, params)
                    tries[api + (":rows" if rows else ":empty")] += 1
                    return rows
                except _Failure as error:
                    tries[api + ":" + error.code] += 1
                    temporary = error.code in ("rate_limited","relay_busy","shared_spacing","budget_exhausted")
                    if not temporary:
                        return None
                    delay = max(2,error.retry_seconds)+1
                    until = time.monotonic()+delay
                    if retry or delay>75 or until+26>=deadline:
                        backoff["*" if error.host else api] = until
                        return None
                    time.sleep(delay)
            return None
        assets, present, missing, dormant = inspect(db, state, day)
        pending_before = {key:len(codes) for key,codes in missing.items()}
        suspended = set()
        suspension_path = private/"evening-stock-suspensions.json"
        stored_suspensions = mapping(suspension_path)
        if stored_suspensions.get("trade_date") == day:
            suspended = set(stored_suspensions.get("codes", []))
        if missing["stock"]-suspended or missing_valuation(output,assets,present,day)["stock"]-suspended:
            FIELDS["suspend_d"] = "ts_code,trade_date,suspend_timing,suspend_type"
            rows = fetch("suspend_d", {"trade_date":compact,"suspend_type":"S"})
            found = confirmed_suspensions(rows,day)
            if found:
                suspended.update(found)
                publish(suspension_path, {"trade_date":day,"codes":sorted(suspended)})
        for group, kind, api in (("stock","stock","daily"), ("sw","sector","sw_daily"), ("ths","sector","ths_daily")):
            gaps = missing[group]-suspended if group == "stock" else missing[group]
            if gaps:
                save_daily(db, kind, api, gaps, day, fetch(api, {"trade_date": compact}))
        assets, present, missing, dormant = inspect(db, state, day)
        choices = [("daily", "stock", code) for code in missing["stock"]-suspended]
        choices.extend(("index_daily","index",code) for code in missing["index"])
        choices.extend(("index_daily","sector",code) for code in missing["exchange_sector"])
        choices.extend(("ths_daily","sector",code) for code in missing["ths"]-dormant)
        # Long-unpublished catalogue rows are checked by the date-wide request.
        # Sample three exact codes as well, rotating across scheduled windows.
        older = sorted(dormant, key=lambda code: (attempts.get(code, ""), code))
        choices.extend(("ths_daily", "sector", code) for code in older[:3])
        lanes = defaultdict(list)
        for item in sorted(choices, key=lambda item: (item[2] in dormant, attempts.get(item[2], ""),item[2])):
            lanes[item[0]].append(item)
        turns = (item for batch in zip_longest(*(lanes[api] for api in ("daily","index_daily","ths_daily")))
                 for item in batch if item is not None)
        for _, (api, kind, code) in zip(range(EXACT_LIMIT), turns):
            rows = fetch(api, {"ts_code": code, "start_date": compact, "end_date": compact})
            if rows is not None:
                attempts[code] = now.isoformat()
            save_daily(db, kind, api, {code}, day, rows)
        assets, present, missing, dormant = inspect(db, state, day)
        valuation_before = missing_valuation(output, assets, present, day)
        for kind, codes in valuation_before.items():
            if kind == "stock":
                codes = codes - suspended
            if not codes:
                continue
            api, fields = APIS[kind]
            saved_fields = FIELDS.get(api)
            FIELDS[api] = ",".join(("ts_code", "trade_date", *fields))
            try:
                rows = fetch(api, {"trade_date":compact})
                additions = normalized_rows(kind, rows or [], day)
                # A market-wide publication occasionally omits a valid symbol.
                # Retry the remaining small set by exact code within the same quota.
                expected = sorted(codes-additions.keys(), key=lambda code: (attempts.get("valuation/"+code,""),code))
                if mapping(output/snapshot_name(kind)).get("trade_date") == day:
                    for code in expected[:3]:
                        exact = fetch(api, {"ts_code":code,"start_date":compact,"end_date":compact})
                        if exact is not None:
                            attempts["valuation/"+code] = now.isoformat()
                            valid = normalized_rows(kind, [row for row in exact if row.get("ts_code")==code], day)
                            additions.update(valid)
            finally:
                if saved_fields is None:
                    FIELDS.pop(api, None)
                else:
                    FIELDS[api] = saved_fields
            save_snapshot(output/snapshot_name(kind), kind, day, additions)
        publish_closes(db, state, source, day, now)
        minute_recovery = mapping(private/"stock-minute-recovery-report.json")
        if minute_recovery.get("trade_date") != day:
            minute_recovery = {}
        out = {"trade_date":day, "checked_at":now.isoformat(),
               "daily":{key:{"expected":len(codes),"missing":sorted(missing[key]-suspended if key=="stock" else missing[key]),
                             "new":pending_before[key]-len(missing[key])}
                        for key,codes in assets.items()},
               "stock_full_session_suspensions": sorted(suspended & assets["stock"]),
               "undated_stock_universe": not bool(assets["stock"]),
               "ths_unpublished_over_30_days":sorted(dormant),
               "valuation_missing":{key:sorted(codes-suspended if key=="stock" else codes)
                                    for key,codes in missing_valuation(output,assets,present,day).items()},
               "stock_archives":{"files":len(list((private/"stock-batches"/day).glob("*.json.gz"))),
                                 "prices_imported":db.execute("SELECT count(*) FROM board_source_files WHERE day=? AND prices_saved=1",(day,)).fetchone()[0]},
               "stock_intraday":{"observed_symbols":db.execute("SELECT count(DISTINCT code) FROM chart_points WHERE kind='stock' AND day=?",(day,)).fetchone()[0],
                                 "primary_minute_series_complete":minute_recovery.get("complete_stock_minutes")},
               "board_intraday":{"observed_symbols":db.execute("SELECT count(DISTINCT code) FROM constituent_minutes WHERE day=?",(day,)).fetchone()[0],
                                 "estimates_from_primary_minutes":minute_recovery.get("boards_recovered")},
               "index_minutes":minute_status(state,source,day,now),
               "requests":dict(tries), "last_exact":attempts}
        publish(private/"evening-gap-report.json", out)
        print(json.dumps({key:value for key,value in out.items() if key not in ("daily","last_exact","ths_unpublished_over_30_days","valuation_missing")},ensure_ascii=False),flush=True)
        print(json.dumps({key:{**data,"missing":len(data["missing"])} for key,data in out["daily"].items()}),flush=True)
    return out


def main():
    parser = ArgumentParser()
    parser.add_argument("mode", choices=("session", "latest"))
    now = shanghai_now()
    day = session(now, parser.parse_args().mode)
    if not day:
        return
    with (STATE/"asset-research/evening-gap.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("An evening recovery is already running", flush=True)
            return
        token = (Path(os.environ["CREDENTIALS_DIRECTORY"])/"sxdata-token").read_text().strip()
        FIELDS["daily"] = "ts_code,trade_date,close,pre_close,pct_chg"
        def request(api, params):
            with relay_client(token, timeout=18) as client:
                return client.request(api, params)
        run(STATE,mapping(SOURCE),day.isoformat(),now,request)


if __name__ == "__main__":
    main()
