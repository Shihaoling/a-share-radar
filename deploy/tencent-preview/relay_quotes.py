"""Poll permitted SXData batches and save one received observation per symbol/minute."""

import argparse
from datetime import datetime, timedelta
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from tempfile import NamedTemporaryFile
import time

from pipeline.calendar import market_session, previous_trading_day, shanghai_now
from pipeline.data_service import gate_delay
from pipeline.stock_api import FIELDS as API_FIELDS, _Failure, _index_code, _realtime
from relay_common import STATE, relay_client
from quote_schedule import QuoteSchedule

SOURCE = Path("/srv/ashare-radar-preview/public/data/view.json")
FIELDS = ("price", "change_pct", "quote_date", "quote_timestamp", "provider",
          "open", "high", "low", "volume", "volume_unit", "amount")
API_FIELDS["rt_k"] = API_FIELDS["rt_idx_k"]
# Shanghai Stock Exchange industry series (exchange identifiers, independent of SW2021).
INDUSTRIES = {
    "000032.SH": "上证能源", "000033.SH": "上证材料", "000034.SH": "上证工业",
    "000035.SH": "上证可选", "000036.SH": "上证消费", "000037.SH": "上证医药", "000038.SH": "上证金融",
    "000039.SH": "上证信息", "000040.SH": "上证通信", "000041.SH": "上证公用",
    "000986.SH": "全指能源", "000991.SH": "全指医药", "399392.SZ": "国证新兴",
    "399393.SZ": "国证地产", "399394.SZ": "国证医药", "399395.SZ": "国证有色",
}


def groups(data, weights):
    indices = {_index_code(item): item["id"] for item in data["market"] if _index_code(item)}
    indices.update({code: code for code in INDUSTRIES})
    stocks = {stock["ts_code"]: stock["ts_code"]
              for sector in data["sectors"] for stock in sector.get("stocks", [])
              if stock.get("provider") == "stock_api"
              and re.fullmatch(r"\d{6}\.(?:SH|SZ)", stock.get("ts_code") or "")}
    for reference in weights.values():
        stocks.update({row["ts_code"]: row["ts_code"] for row in reference.get("stocks", [])})
    return {"rt_idx_k": indices, "rt_k": stocks}


def publish(path, payload):
    raw = (json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_bytes() == raw:
        return
    with NamedTemporaryFile(dir=path.parent, prefix="relay-", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(raw)
            handle.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def collect(token, api, symbols, min_gap_seconds=6):
    if not symbols:
        return []
    with relay_client(token, timeout=5, min_gap_seconds=min_gap_seconds) as client:
        received = client.request(api, {"ts_code": ",".join(symbols)})
    now = shanghai_now()
    filtered = {}
    for row in received:
        code = row["ts_code"]
        if not isinstance(code, str) or code not in symbols:
            continue
        try:
            record = _realtime([row], symbols, now, api, token)[code]
            filtered.setdefault((code, record["quote_timestamp"]), record)
        except _Failure as failure:
            if failure.code not in {"empty_realtime", "invalid_bar", "invalid_number", "invalid_timestamp"}:
                raise
    if not filtered:
        raise _Failure("empty_realtime", 60)
    return [{"id": symbols[code], "name": record["name"], **{key: record[key] for key in FIELDS},
             "previous_close": record["price"] / (1 + record["change_pct"] / 100),
             "volume_unit": "share" if api == "rt_k" else record["volume_unit"],
             "timestamp_quality": "source_reported"} for (code, _), record in filtered.items()]


def board_estimates(sectors, stock_quotes, day):
    previous = previous_trading_day(day)
    if previous is None:
        return []
    previous = previous.isoformat()
    estimates = []
    for sector in sectors:
        weighted = [(stock, stock_quotes.get(stock.get("ts_code"))) for stock in sector.get("stocks", [])
                    if stock.get("market_cap_date") == previous and (stock.get("market_cap") or 0) > 0]
        available = [(stock, quote) for stock, quote in weighted if quote and quote["quote_date"] == day]
        if not available:
            continue
        latest = max(datetime.fromisoformat(quote["quote_timestamp"]) for stock, quote in available)
        available = [(stock, quote) for stock, quote in available
                     if latest-datetime.fromisoformat(quote["quote_timestamp"]) <= timedelta(seconds=90)]
        cap = sum(stock["market_cap"] for stock, quote in available)
        coverage = cap / sum(stock["market_cap"] for stock, quote in weighted)
        if len(available) < 3 or coverage < .6:
            continue
        change = sum(stock["market_cap"] * quote["change_pct"] for stock, quote in available) / cap
        base = sector.get("daily_quote") or {}
        estimate = {"id": sector["id"], "quote_date": day, "quote_timestamp": latest.isoformat(),
                    "change_pct": change, "sample_count": len(available), "sample_size": len(weighted),
                    "weight_coverage": coverage, "basis_date": previous, "basis": "constituent_estimate"}
        if base.get("quote_date") == previous and (base.get("price") or 0) > 0:
            estimate["price"] = base["price"] * (1 + change / 100)
        estimates.append(estimate)
    return estimates


def queue_retry(history, batch, now, delay, error):
    due = now + timedelta(seconds=max(60, delay))
    history.execute("""INSERT INTO retries VALUES (?,?,?,?, 'awaiting_retry')
                       ON CONFLICT(api, minute) DO UPDATE SET due=excluded.due, error=excluded.error,
                       status=excluded.status""", (batch, now.isoformat()[:16], due.isoformat(), error))
    history.commit()


INDEX_MINUTE_STORED = """EXISTS(SELECT 1 FROM samples s WHERE s.minute=retries.minute
                                AND s.id IN (SELECT value FROM json_each(?)))"""


def settle_retries(history, batch, minutes, index_ids):
    if not minutes:
        return
    if batch == "rt_idx_k":
        # A later failure can share a minute with an already stored index observation.
        history.execute(f"""UPDATE retries SET status='recovered' WHERE api=? AND status<>'recovered'
            AND ((status='awaiting_retry' AND minute<=?) OR minute IN (SELECT value FROM json_each(?)))
            AND {INDEX_MINUTE_STORED}""", (batch, max(minutes), json.dumps(sorted(minutes)), json.dumps(list(index_ids))))
    else:
        history.executemany("UPDATE retries SET status='observed_snapshot_only' WHERE api=? AND minute=?",
                            [(batch, minute) for minute in minutes])
    history.execute("UPDATE retries SET status='unrecoverable' WHERE api=? AND status='awaiting_retry' AND minute<?",
                    (batch, max(minutes)))


def reconcile_retry_history(history, index_ids):
    history.execute("""UPDATE retries SET status='observed_snapshot_only' WHERE api='rt_k' AND status='recovered'""")
    history.execute(f"""UPDATE retries SET status='unverified' WHERE api='rt_idx_k' AND status='recovered'
        AND NOT {INDEX_MINUTE_STORED}""", (json.dumps(list(index_ids)),))


def expire_prior_day_retries(history, day, index_ids):
    history.execute(f"""UPDATE retries SET status=CASE WHEN api='rt_idx_k' AND {INDEX_MINUTE_STORED}
        THEN 'recovered' ELSE 'unrecoverable' END WHERE status='awaiting_retry' AND minute<?""",
        (json.dumps(list(index_ids)), day))


def store_quotes(history, batch, fetched, latest, index_ids):
    sample_changed = False
    minutes = set()
    for quote in fetched:
        stamp = quote["quote_timestamp"]
        minute = stamp[:16]
        if batch == "rt_idx_k":
            inserted = history.execute("""INSERT INTO samples (id,minute,source_stamp,price,change_pct)
                VALUES (?,?,?,?,?) ON CONFLICT(id,minute) DO NOTHING""",
                (quote["id"], minute, stamp, quote["price"], quote["change_pct"]))
            sample_changed |= inserted.rowcount > 0
        minutes.add(minute)
        previous = latest.get(quote["id"])
        if not previous or stamp >= (previous.get("quote_timestamp") or ""):
            latest[quote["id"]] = quote
    settle_retries(history, batch, minutes, index_ids)
    return sample_changed


def restore_gate_cooldowns(schedule, now):
    for batch in schedule.batches:
        remaining, _ = gate_delay(batch)
        if remaining:
            until = now.timestamp() + remaining
            schedule.paused_until[batch] = max(schedule.paused_until.get(batch, 0), until)


def read_saved_quotes(path, groups, allowed, now):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("source") != "sxdata_relay":
        return {}
    result = {}
    for group in groups:
        rows = payload.get(group)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            code, stamp, price = row.get("id"), row.get("quote_timestamp"), row.get("price")
            if (not isinstance(code, str) or code not in allowed or not isinstance(stamp, str)
                    or row.get("quote_date") != stamp[:10] or row.get("provider") != "stock_api"
                    or type(price) not in (int, float) or not math.isfinite(price) or price <= 0):
                continue
            try:
                observed = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if observed.tzinfo is None or observed > now or stamp <= result.get(code, {}).get("quote_timestamp", ""):
                continue
            result[code] = {k: row[k] for k in ("id", "name", "previous_close", "timestamp_quality", *FIELDS) if k in row}
    return result


def restore_industry_samples(history, latest, now):
    # One reverse lookup per exchange index on the (id, minute) primary key at startup.
    for code in INDUSTRIES:
        stored = history.execute("""SELECT source_stamp,price,change_pct FROM samples
            WHERE id=? AND minute<=? AND source_stamp<=? AND price>0 ORDER BY minute DESC LIMIT 1""",
            (code, now.isoformat()[:16], now.isoformat())).fetchone()
        if not stored:
            continue
        stamp, price, change = stored
        if stamp <= latest.get(code, {}).get("quote_timestamp", ""):
            continue
        latest[code] = {"id": code, "price": price, "change_pct": change, "quote_timestamp": stamp,
                        "quote_date": stamp[:10], "provider": "stock_api", "timestamp_quality": "source_reported"}


def dated_quote(quote, day):
    if quote and quote["quote_date"] < day:
        return {**quote, "stale": True}
    return quote


def publish_intraday(path, history, day, entity_ids):
    series = {}
    for entity_id in sorted(entity_ids):
        rows = history.execute(
            "SELECT source_stamp, price FROM samples WHERE id = ? AND minute BETWEEN ? AND ? ORDER BY minute",
            (entity_id, f"{day}T09:30", f"{day}T15:00"),
        ).fetchall()
        if rows:
            series[entity_id] = rows
    publish(path, {"schema_version": 1, "source": "sxdata_relay", "trade_date": day,
                   "last_sample": max((rows[-1][0] for rows in series.values()), default=None),
                   "series": series})


def start(args):
    from market_daily import merge_daily, read_daily

    conf = json.loads(Path(__file__).with_name("relay-active.json").read_text())
    credential = Path(os.environ["CREDENTIALS_DIRECTORY"]) / "sxdata-token"
    token = credential.read_text(encoding="utf-8").strip()
    if not token or sha256(token.encode()).hexdigest() != os.environ["RADAR_EXPECTED_STOCK_KEY_SHA256"]:
        raise SystemExit("SXData credential verification failed")
    weight_file = STATE / "relay/industry-weights.json"
    weights = json.loads(weight_file.read_text()) if weight_file.exists() else {}
    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    baseline = previous_trading_day(shanghai_now().date())
    if baseline:
        merge_daily(data, read_daily(), baseline.isoformat())
    universe = groups(data, weights)
    sector_ids = {sector["id"] for sector in data["sectors"]}
    if args.diagnostic:
        result = collect(token, "rt_idx_k", universe["rt_idx_k"], conf["min_gap_seconds"])
        print(json.dumps({"mode": "diagnostic_no_publication", "count": len(result),
                          "source_latest": max((q["quote_timestamp"] for q in result), default=None)}), flush=True)
        return

    private = STATE / "relay"
    private.mkdir(parents=True, exist_ok=True)
    output = STATE / "quote-output/live-quotes.json"
    intraday_output = output.with_name("today-intraday.json")
    stocks_output = output.with_name("stock-quotes.json")
    entity_ids = universe["rt_idx_k"].values()
    restored_at = shanghai_now()
    quotes = {"rt_idx_k": read_saved_quotes(output, ("market","sectors"), set(entity_ids), restored_at)}
    if "rt_k" in conf["batches"]:
        quotes["rt_k"] = read_saved_quotes(stocks_output, ("stocks",), universe["rt_k"], restored_at)
    schedule = QuoteSchedule(conf)
    close_batches = iter(conf["batches"]) if args.close_snapshot else None
    if close_batches is not None and market_session()["phase"] != "post_close":
        raise SystemExit("The one-time close snapshot requires a completed trading session")
    status = {batch: "waiting" for batch in conf["batches"]}
    last_phase = None
    fingerprint = None
    history_day = None
    with sqlite3.connect(private / "minute-observations.sqlite3") as history:
        history.execute("""CREATE TABLE IF NOT EXISTS samples (
            id TEXT, minute TEXT, source_stamp TEXT, price REAL, change_pct REAL,
            PRIMARY KEY (id, minute)
        ) WITHOUT ROWID""")
        history.execute("""CREATE TABLE IF NOT EXISTS retries (
            api TEXT, minute TEXT, due TEXT, error TEXT, status TEXT, PRIMARY KEY(api, minute)
        ) WITHOUT ROWID""")
        restore_industry_samples(history, quotes["rt_idx_k"], restored_at)
        reconcile_retry_history(history, entity_ids)
        history.commit()
        for batch, due in history.execute("SELECT api, max(due) FROM retries WHERE status='awaiting_retry' GROUP BY api"):
            schedule.paused_until[batch] = datetime.fromisoformat(due).timestamp()
        restore_gate_cooldowns(schedule, shanghai_now())
        while True:
            now = shanghai_now()
            day = now.date().isoformat()
            sample_changed = False
            if day != history_day:
                baseline = previous_trading_day(day)
                if baseline:
                    merge_daily(data, read_daily(), baseline.isoformat())
                if weight_file.exists():
                    weights = json.loads(weight_file.read_text())
                    universe = groups(data, weights)
                    entity_ids = universe["rt_idx_k"].values()
                expire_prior_day_retries(history, day, entity_ids)
                history.commit()
            phase = market_session(now)["phase"]
            if last_phase != phase:
                last_phase = phase
                if phase in {"morning", "afternoon", "closing_auction"}:
                    for batch in status:
                        status[batch] = "waiting"

            batch = schedule.admit(now) if close_batches is None else next(close_batches, None)
            if close_batches is not None and batch is None:
                return
            if batch:
                delay = 0
                host_cooldown = False
                try:
                    fetched = collect(token, batch, universe[batch], conf["min_gap_seconds"])
                    sample_changed = store_quotes(history, batch, fetched, quotes[batch], entity_ids)
                    if batch == "rt_k":
                        for estimate in board_estimates(data["sectors"], quotes[batch], day):
                            stamp = estimate["quote_timestamp"]
                            if "price" in estimate:
                                sample_changed |= history.execute("INSERT OR IGNORE INTO samples VALUES (?,?,?,?,?)",
                                    (estimate["id"], stamp[:16], stamp, estimate["price"], estimate["change_pct"])).rowcount > 0
                    history.commit()
                    finished_at = shanghai_now()
                    fresh_ids = {q["id"] for q in fetched
                                 if (finished_at - datetime.fromisoformat(q["quote_timestamp"])).total_seconds() <= 30}
                    status[batch] = "stale" if not fresh_ids else "ok" if len(fresh_ids) == len(universe[batch]) else "partial"
                except _Failure as failure:
                    status[batch] = failure.code
                    if failure.deferred:
                        restore_gate_cooldowns(schedule, shanghai_now())
                    else:
                        delay = failure.retry_seconds or 60
                        host_cooldown = failure.host or failure.code == "rate_limited"
                        queue_retry(history, batch, shanghai_now(), delay, failure.code)
                    print(json.dumps({"event": "source_failure", "api": batch,
                                      "code": failure.code, "provider_code": failure.provider_code}), flush=True)
                finally:
                    schedule.finish(shanghai_now(), delay, host=host_cooldown)

            if day != history_day or batch == "rt_k":
                publish(stocks_output, {"schema_version": 1, "source": "sxdata_relay", "trade_date": day,
                                        "stocks": [dated_quote(q, day) for q in quotes.get("rt_k", {}).values()
                                                   if q["id"] in universe["rt_k"]],
                                        "board_estimates": board_estimates(data["sectors"], quotes.get("rt_k", {}), day)})
            if day != history_day or sample_changed:
                publish_intraday(intraday_output, history, day, set(entity_ids) | sector_ids)
                history_day = day

            index_quotes = quotes["rt_idx_k"]
            payload = {"schema_version": 1, "source": "sxdata_relay", "trade_date": day,
                       "collector": {"status": next(iter(status.values())), "session": phase,
                                     "batches": status.copy(),
                                     "min_gap_seconds": conf["min_gap_seconds"]},
                       "market": [dated_quote(q, day) for symbol, q in index_quotes.items() if symbol not in INDUSTRIES],
                       "sectors": [{"id": code, "name": name, **dated_quote(index_quotes.get(code, {}), day),
                                    "taxonomy": "SSE", "basis": "index", "reference": weights.get(code, {})}
                                   for code, name in INDUSTRIES.items()]}
            if payload != fingerprint:
                fingerprint = payload.copy()
                payload["generated_at"] = shanghai_now().isoformat()
                publish(output, payload)
            time.sleep(0.25 if phase in {"morning", "afternoon", "closing_auction"} else 2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diagnostic", action="store_true")
    parser.add_argument("--close-snapshot", action="store_true")
    start(parser.parse_args())
