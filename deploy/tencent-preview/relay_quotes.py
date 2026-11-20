"""Poll permitted SXData batches and save one received observation per symbol/minute."""

import argparse
from contextlib import closing, nullcontext
from datetime import datetime, timedelta
import gzip
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
from pipeline.data_service import gate_delay, realtime_min_gap
from pipeline.stock_api import FIELDS as API_FIELDS, _Failure, _index_code, _realtime
from relay_common import STATE, relay_client
from board_quote_watch import BoardQuoteWatch
from board_stock_market import StockUniverse, StockMarketBatches, StockBatchArchive
from quote_schedule import QuoteSchedule
from sector_quotes import IndexBatches, SectorFeed, catalog_codes

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


def watched_stocks(path, now):
    """Reuse a scheduled stock batch for a bounded set of recently viewed details."""
    try:
        rows = json.loads(path.read_bytes())["stocks"]
        if not isinstance(rows, list):
            return {}
        return {code: code for code, stamp in rows[:24] if isinstance(code, str)
                and re.fullmatch(r"[0-9]{6}\.(?:SH|SZ|BJ)", code)
                and type(stamp) in (int, float) and now.timestamp() - 150 <= stamp <= now.timestamp()}
    except (OSError, KeyError, TypeError, ValueError):
        return {}


def collect(token, api, symbols, min_gap_seconds=6, *, selector=None):
    if not symbols:
        return []
    # A full A-share snapshot contains over 5,500 rows. Let its initial
    # response arrive during a slower market turn without adding a retry.
    timeout = 9 if api == "rt_k" and selector == StockMarketBatches.full_selector else 5
    with relay_client(token, timeout=timeout, min_gap_seconds=min_gap_seconds) as client:
        received = client.request(api, {"ts_code": selector or ",".join(symbols)})
    now = shanghai_now()
    filtered = {}
    for row in received:
        code = row["ts_code"]
        if not isinstance(code, str) or code not in symbols:
            continue
        try:
            record = _realtime([row], symbols, now, api, token)[code]
            record["previous_close"] = float(row["pre_close"])
            filtered.setdefault((code, record["quote_timestamp"]), record)
        except _Failure as failure:
            if failure.code not in {"empty_realtime", "invalid_bar", "invalid_number", "invalid_timestamp"}:
                raise
    if not filtered:
        raise _Failure("empty_realtime", 60)
    return [{"id": symbols[code], "name": record["name"], **{key: record[key] for key in FIELDS},
             "previous_close": record["previous_close"],
             "volume_unit": "share" if api == "rt_k" else record["volume_unit"],
             "timestamp_quality": "source_reported"} for (code, _), record in filtered.items()]


def board_estimates(sectors, stock_quotes, day, caps=None, *, phase="intraday"):
    previous = previous_trading_day(day)
    if previous is None:
        return []
    previous = previous.isoformat()
    caps = caps or {}
    estimates = []
    for sector in sectors:
        if sector.get("taxonomy") != "SW2021":
            continue
        weighted = {}
        for stock in sector.get("stocks", []):
            code = stock.get("ts_code")
            cap = caps.get(code, stock.get("market_cap") if stock.get("market_cap_date") == previous else None)
            if type(cap) in (float, int) and math.isfinite(cap) and cap > 0:
                weighted[code] = cap
        available = []
        for code, cap in weighted.items():
            quote = stock_quotes.get(code, {})
            change = quote.get("change_pct")
            if quote.get("quote_date") == day and type(change) in (float, int) and math.isfinite(change):
                available.append((cap, quote))
        if not available:
            continue
        latest = max(datetime.fromisoformat(quote["quote_timestamp"]) for cap, quote in available)
        available = [(cap, quote) for cap, quote in available
                     if latest-datetime.fromisoformat(quote["quote_timestamp"]) <= timedelta(seconds=90)]
        cap = sum(weight for weight, quote in available)
        coverage = cap / sum(weighted.values())
        if len(available) < 3 or coverage < .6:
            continue
        change = sum(weight * quote["change_pct"] for weight, quote in available) / cap
        base = sector.get("daily_quote") or {}
        estimate = {"id": sector["id"], "quote_date": day, "quote_timestamp": latest.isoformat(),
                    "change_pct": change, "sample_count": len(available), "sample_size": len(weighted),
                    "weight_coverage": coverage, "basis_date": previous, "basis": "constituent_estimate",
                    "estimate_phase": phase}
        if base.get("quote_date") == previous and (base.get("price") or 0) > 0:
            estimate["price"] = base["price"] * (1 + change / 100)
        estimates.append(estimate)
    return estimates


def read_board_estimates(path, sectors):
    try:
        rows = json.loads(path.read_bytes()).get("board_estimates", [])
        return {row["id"]: row for row in rows if row.get("id") in sectors
                and row.get("basis") == "constituent_estimate" and isinstance(row.get("quote_timestamp"), str)}
    except (OSError, ValueError, AttributeError, TypeError):
        return {}


def completed_stock_quotes(path, day):
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        packet = json.load(stream)
    day_key = day.replace("-", "")
    if packet.get("complete") is not True or packet.get("trade_date") not in (day, day_key):
        return {}
    stamp = day + "T15:00:00+08:00"
    return {row["ts_code"]: {"quote_date": day, "quote_timestamp": stamp, "change_pct": row.get("pct_chg")}
            for row in packet["rows"] if row.get("trade_date") in (day, day_key) and isinstance(row.get("ts_code"), str)}


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
    codes = catalog_codes(SOURCE.with_name("sector-catalog.json"))
    sector_feed = SectorFeed(private, output.parent, codes, publish) if codes else None
    index_batches = IndexBatches(universe["rt_idx_k"], codes)
    if getattr(args, "catalog_snapshot", False):
        session = market_session()["phase"]
        if session not in {"midday_break", "post_close"}:
            raise SystemExit("A sector directory bootstrap requires lunch or post-close")
        if sector_feed is None:
            raise SystemExit("A verified sector catalog is required")
        fetched = collect(token, "rt_idx_k", index_batches.all, conf["min_gap_seconds"])
        path = private / "minute-observations.sqlite3"
        reader = closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) if path.exists() else nullcontext()
        with reader as core_history:
            sector_feed.update(core_history, set(INDUSTRIES), fetched, shanghai_now())
        print(json.dumps({"mode": "catalog_snapshot", "received": len(fetched),
                          "sectors": len({q["id"] for q in fetched if q["id"] in codes})}), flush=True)
        return
    intraday_output = output.with_name("today-intraday.json")
    stocks_output = output.with_name("stock-quotes.json")
    saved_estimates = read_board_estimates(stocks_output, sector_ids)
    sector_caps = {}
    next_close_check = 0
    last_close_version = None
    entity_ids = universe["rt_idx_k"].values()
    core_entity_ids = set(entity_ids)
    restored_at = shanghai_now()
    stock_watch_file = STATE / "asset-research/live-watch.json"
    board_watch = BoardQuoteWatch(stock_watch_file.with_name("board-watch.json"), output.parent / "sector-members")
    market_universe = StockUniverse(STATE / "sector-directory/stock-names.json", output.parent / "sector-members")
    market_batches = StockMarketBatches()
    batch_archive = StockBatchArchive(STATE / "asset-research/stock-batches")
    board_output = output.with_name("board-stock-quotes.json")
    requested_stocks = watched_stocks(stock_watch_file, restored_at)
    quotes = {"rt_idx_k": read_saved_quotes(output, ("market","sectors"), set(entity_ids), restored_at)}
    if "rt_k" in conf["batches"]:
        board_watch.refresh(restored_at.timestamp())
        retained = universe["rt_k"].keys() | requested_stocks.keys() | board_watch.tracked_codes
        market_universe.refresh(restored_at.timestamp())
        quotes["rt_k"] = read_saved_quotes(stocks_output, ("stocks",), retained, restored_at)
        for code, row in read_saved_quotes(board_output, ("stocks",), retained | market_universe.codes.keys(), restored_at).items():
            old = quotes["rt_k"].get(code)
            if old is None or old.get("quote_timestamp", "") < row["quote_timestamp"]:
                quotes["rt_k"][code] = row
    schedule = QuoteSchedule(conf)
    schedule.gap = max(conf["min_gap_seconds"], realtime_min_gap())
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
        watch_retry_at = 0
        sector_retry_at = 0
        while True:
            now = shanghai_now()
            day = now.date().isoformat()
            sample_changed = False
            stock_received = False
            if day != history_day:
                baseline = previous_trading_day(day)
                if baseline:
                    merge_daily(data, read_daily(), baseline.isoformat())
                    try:
                        sector_caps = json.loads((output.parent / "trend-stock-caps.json").read_bytes()).get(baseline.isoformat(), {})
                    except (OSError, ValueError, AttributeError):
                        sector_caps = {}
                if weight_file.exists():
                    weights = json.loads(weight_file.read_text())
                    universe = groups(data, weights)
                    entity_ids = universe["rt_idx_k"].values()
                    core_entity_ids = set(entity_ids)
                    index_batches = IndexBatches(universe["rt_idx_k"], codes)
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
            directory_rows = []
            if batch:
                delay = 0
                host_cooldown = False
                deferred = False
                symbols, selector = universe[batch], None
                if batch == "rt_k":
                    requested_stocks = watched_stocks(stock_watch_file, now)
                    board_watch.refresh(now.timestamp())
                    extras_allowed = now.timestamp() >= watch_retry_at
                    if extras_allowed:
                        symbols = {**symbols, **requested_stocks}
                    board_plan = board_watch.choose(symbols, now.timestamp(), allowed=extras_allowed)
                    priority_symbols = {**symbols, **board_plan.extra}
                    market_universe.refresh(now.timestamp())
                    market_plan = (market_batches.choose(market_universe.codes, symbols, board_watch.tracked_codes, now.timestamp())
                                   if close_batches is None else None)
                    symbols, selector = (market_plan.symbols, market_plan.selector) if market_plan else (priority_symbols, None)
                rotating = batch == "rt_idx_k" and sector_feed is not None and close_batches is None
                if rotating:
                    symbols, selector = index_batches.choose(now.timestamp())
                observed_count = 0
                try:
                    options = {"selector": selector} if selector else {}
                    fetched = collect(token, batch, symbols, conf["min_gap_seconds"], **options)
                    observed_ids = {q["id"] for q in fetched}
                    observed_count = len(observed_ids)
                    if batch == "rt_k":
                        stock_received = True
                        try:
                            batch_archive.record(fetched, shanghai_now())
                        except (OSError, ValueError) as error:
                            print(json.dumps({"event": "stock_batch_archive_delayed", "error": type(error).__name__}), flush=True)
                        market_batches.finish(market_plan, observed_ids, universe[batch], now.timestamp())
                        if market_plan is None:
                            board_watch.finish(now.timestamp(), board_plan, universe[batch], observed_ids)
                        if (market_plan is None and not board_plan.extra and requested_stocks and extras_allowed
                                and len(universe[batch].keys() & observed_ids) < len(universe[batch]) * .9):
                            watch_retry_at = now.timestamp() + 180
                    if batch == "rt_idx_k" and sector_feed is not None:
                        directory_rows = fetched
                        # The original publication, retry ledger and model input
                        # keep only their original index universe.
                        fetched = [q for q in fetched if q["id"] in core_entity_ids]
                    sample_changed = store_quotes(history, batch, fetched, quotes[batch], entity_ids)
                    history.commit()
                    finished_at = shanghai_now()
                    directory_slice = batch == "rt_k" and market_plan is not None and market_plan.kind != "whole"
                    observed_quotes = quotes[batch].values() if batch == "rt_idx_k" or directory_slice else fetched
                    fresh_ids = {q["id"] for q in observed_quotes
                                 if (finished_at - datetime.fromisoformat(q["quote_timestamp"])).total_seconds() <= 30}
                    expected_count = len(universe[batch])
                    if batch == "rt_k":
                        fresh_ids.intersection_update(universe[batch])
                    status[batch] = "stale" if not fresh_ids else "ok" if len(fresh_ids) == expected_count else "partial"
                except _Failure as failure:
                    # Only evidence about this batch's contents changes its size or cursor.
                    # Transport and service failures still take the normal scheduler cooldown.
                    batch_rejected = (failure.code in {"empty_realtime", "response_too_large"}
                                      or failure.code == "api_denied" and failure.provider_reason == "invalid_parameters")
                    if batch == "rt_k" and not failure.deferred:
                        if batch_rejected:
                            market_batches.finish(market_plan, set(), universe[batch], now.timestamp())
                            if market_plan is None:
                                board_watch.finish(now.timestamp(), board_plan, universe[batch], set())
                                if not board_plan.extra and requested_stocks and extras_allowed:
                                    watch_retry_at = now.timestamp() + 180
                        else:
                            market_batches.unavailable(market_plan, failure.code, now.timestamp())
                    deferred = failure.deferred
                    if deferred:
                        restore_gate_cooldowns(schedule, shanghai_now())
                    else:
                        status[batch] = failure.code
                        delay = failure.retry_seconds or 60
                        host_cooldown = failure.host or failure.code == "rate_limited"
                        queue_retry(history, batch, shanghai_now(), delay, failure.code)
                    print(json.dumps({"event": "request_deferred" if deferred else "source_failure", "api": batch,
                                      "code": failure.code, "provider_code": failure.provider_code}), flush=True)
                finally:
                    if rotating and not deferred:
                        index_batches.finish(now.timestamp(), observed_count, len(symbols))
                    schedule.finish(shanghai_now(), delay, host=host_cooldown, deferred=deferred)
                    schedule.gap = max(conf["min_gap_seconds"], realtime_min_gap())

            estimates = (board_estimates(data["sectors"], quotes.get("rt_k", {}), day, sector_caps)
                         if stock_received or day != history_day else [])
            if phase == "post_close" and now.timestamp() >= next_close_check:
                next_close_check = now.timestamp() + 30
                close_path = STATE / "research/equity/daily" / (day.replace("-", "") + ".json.gz")
                try:
                    version = (day, close_path.stat().st_mtime_ns)
                    if version != last_close_version:
                        closes = completed_stock_quotes(close_path, day)
                        if closes:
                            estimates.extend(board_estimates(data["sectors"], closes, day, sector_caps, phase="close"))
                            last_close_version = version
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            estimate_changed = False
            for estimate in estimates:
                ident = estimate["id"]
                stamp = estimate["quote_timestamp"]
                previous_estimate = saved_estimates.get(ident, {})
                rank = (estimate["quote_date"], estimate["estimate_phase"] == "close", stamp)
                prior_rank = (previous_estimate.get("quote_date", ""), previous_estimate.get("estimate_phase") == "close",
                              previous_estimate.get("quote_timestamp", ""))
                if rank < prior_rank or estimate == previous_estimate:
                    continue
                saved_estimates[ident] = estimate
                estimate_changed = True
                if "price" in estimate:
                    sample_changed |= history.execute("INSERT OR IGNORE INTO samples VALUES (?,?,?,?,?)",
                        (ident, stamp[:16], stamp, estimate["price"], estimate["change_pct"])).rowcount > 0
            if sample_changed:
                history.commit()
            if day != history_day or batch == "rt_k" or estimate_changed:
                display_stocks = set(universe.get("rt_k", {})) | set(requested_stocks) | board_watch.tracked_codes
                if "rt_k" in quotes:
                    all_stock_ids = display_stocks | market_universe.codes.keys()
                    quotes["rt_k"] = {c: q for c, q in quotes["rt_k"].items() if c in all_stock_ids}
                publish(stocks_output, {"schema_version": 1, "source": "sxdata_relay", "trade_date": day,
                                        "core_stock_ids": sorted(universe.get("rt_k", {})),
                                        "stocks": [dated_quote(q, day) for q in quotes.get("rt_k", {}).values()
                                                   if q["id"] in display_stocks],
                                        "board_estimates": list(saved_estimates.values())})
                if market_universe.codes and (stock_received or day != history_day):
                    try:
                        publish(board_output, {"schema_version": 1, "source": "sxdata_relay", "trade_date": day,
                                               "stocks": [dated_quote(q, day) for q in quotes.get("rt_k", {}).values()]})
                    except (OSError, ValueError) as error:
                        print(json.dumps({"event": "board_stock_publication_delayed", "error": type(error).__name__}), flush=True)
            if day != history_day or sample_changed:
                publish_intraday(intraday_output, history, day, set(entity_ids) | sector_ids)
                history_day = day

            index_quotes = quotes["rt_idx_k"]
            payload = {"schema_version": 1, "source": "sxdata_relay", "trade_date": day,
                       "collector": {"status": next(iter(status.values())), "session": phase,
                                     "batches": status.copy(),
                                     "min_gap_seconds": schedule.gap},
                       "market": [dated_quote(q, day) for symbol, q in index_quotes.items() if symbol not in INDUSTRIES],
                       "sectors": [{"id": code, "name": name, **dated_quote(index_quotes.get(code, {}), day),
                                    "taxonomy": "SSE", "basis": "index", "reference": weights.get(code, {})}
                                   for code, name in INDUSTRIES.items()]}
            if payload != fingerprint:
                fingerprint = payload.copy()
                payload["generated_at"] = shanghai_now().isoformat()
                publish(output, payload)
            if (sector_feed is not None and time.monotonic() >= sector_retry_at
                    and (sector_feed.day != day or directory_rows)):
                try:
                    sector_feed.update(history, set(INDUSTRIES), directory_rows, shanghai_now())
                except (OSError, sqlite3.Error, ValueError) as err:
                    # A directory projection failure cannot stop existing feeds.
                    sector_feed.day = None
                    sector_retry_at = time.monotonic() + 5
                    print(json.dumps({"event": "sector_publication_delayed",
                                      "error": type(err).__name__}), flush=True)
            time.sleep(0.25 if phase in {"morning", "afternoon", "closing_auction"} else 2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diagnostic", action="store_true")
    parser.add_argument("--close-snapshot", action="store_true")
    parser.add_argument("--catalog-snapshot", action="store_true")
    start(parser.parse_args())
