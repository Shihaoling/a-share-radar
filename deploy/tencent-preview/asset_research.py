"""Serve instrument research from observed quotes and completed daily sessions."""
from collections import OrderedDict
from contextlib import closing
from functools import lru_cache
from datetime import date, datetime
import gzip
from http.server import BaseHTTPRequestHandler
import json
import math
import os
from pathlib import Path
import re
from socketserver import UnixStreamServer
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from pipeline.calendar import is_trading_day, previous_trading_day, shanghai_now
from pipeline.stock_api import _index_code
from relay_common import STATE
from relay_quotes import publish
from research_factors import board_breadth, compute
from research_valuation import asset_metrics, collect
from factor_chart import build_daily_chart
from intraday_chart import IntradayCharts
import constituent_live
from board_history import BoardHistory

PUBLIC = Path("/srv/ashare-radar-preview/public/data")
RUNTIME = Path("/opt/ashare-radar-preview/runtime")
STOCK_CODE = re.compile(r"[0-9]{5,6}\.(?:SH|SZ|BJ|NQ|HK)")
ROUTE = re.compile(r"/data/asset-research/(stock|index|sector)/([0-9]{5,6}R?\.(?:SH|SZ|SI|TI|BJ|NQ|HK))\.json")
SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS assets(kind TEXT,code TEXT,name TEXT,PRIMARY KEY(kind,code)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS bars(kind TEXT,code TEXT,day TEXT,close REAL NOT NULL,pre_close REAL,pct_chg REAL,source TEXT NOT NULL,
 PRIMARY KEY(kind,code,day)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS index_close_evidence(code TEXT,day TEXT,close REAL NOT NULL,source TEXT NOT NULL,
 source_stamp TEXT NOT NULL,available_at TEXT NOT NULL,PRIMARY KEY(code,day)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS watched(kind TEXT,code TEXT,stamp REAL,PRIMARY KEY(kind,code)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS coverage(kind TEXT,day TEXT,source TEXT,complete INTEGER,rows INTEGER,
 PRIMARY KEY(kind,day,source)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS last_quotes(kind TEXT,code TEXT,stamp TEXT,body TEXT,PRIMARY KEY(kind,code)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS inputs(path TEXT PRIMARY KEY,version TEXT) WITHOUT ROWID;
"""
UPSERT = """INSERT INTO bars VALUES(?,?,?,?,?,?,?) ON CONFLICT(kind,code,day) DO UPDATE SET
 close=excluded.close,pre_close=COALESCE(excluded.pre_close,bars.pre_close),
 pct_chg=COALESCE(excluded.pct_chg,bars.pct_chg),source=excluded.source"""
ASSETS = """INSERT INTO assets VALUES(?,?,?) ON CONFLICT(kind,code) DO UPDATE SET
 name=COALESCE(NULLIF(excluded.name,''),assets.name)"""


def finite(value):
    return type(value) in (float, int) and math.isfinite(value)


def normalized_day(day):
    try:
        return date.fromisoformat(str(day)).isoformat()
    except ValueError:
        return None


@lru_cache(maxsize=128)
def sessions_ending(day):
    output = []
    current = date.fromisoformat(day)
    for _ in range(120):
        output.append(current.isoformat())
        current = previous_trading_day(current)
        if current is None:
            break
    return list(reversed(output))


def bar_record(kind, code, row, source):
    day = normalized_day(row.get("date") or row.get("trade_date"))
    price = row.get("close")
    if not day or not finite(price) or price <= 0:
        return None
    prev = row.get("pre_close")
    change = row.get("pct_chg", row.get("change_pct"))
    return (kind, code, day, price, prev if finite(prev) and prev > 0 else None,
            change if finite(change) else None, source)


def source_stamp(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return shanghai_now(stamp) if stamp.tzinfo is not None else None


def index_previous_closes(db, packet, now):
    # live-quotes.market contains only the collector's rt_idx_k index records.
    day = normalized_day(packet.get("trade_date"))
    available = source_stamp(packet.get("generated_at"))
    if (packet.get("source") != "sxdata_relay" or packet.get("schema_version") != 1
            or not day or is_trading_day(day) is not True or not available
            or available.date().isoformat() != day or available > now):
        return
    previous = previous_trading_day(day)
    if previous is None or not isinstance(packet.get("market"), list):
        return
    source = "sxdata_rt_idx_k.pre_close"
    for row in packet["market"]:
        if not isinstance(row, dict):
            continue
        code = _index_code(row)
        close = row.get("previous_close")
        observed = source_stamp(row.get("quote_timestamp"))
        if (not code or row.get("provider") != "stock_api" or row.get("timestamp_quality") != "source_reported"
                or row.get("quote_date") != day or not observed or observed.date().isoformat() != day
                or observed > available or not finite(close) or close <= 0):
            continue
        added = db.execute("""INSERT OR IGNORE INTO bars(kind,code,day,close,source)
            SELECT 'index',code,?,?,? FROM assets WHERE kind='index' AND code=?""",
            (previous.isoformat(), close, source, code))
        if added.rowcount:
            db.execute("INSERT OR REPLACE INTO index_close_evidence VALUES(?,?,?,?,?,?)",
                       (code, previous.isoformat(), close, source, observed.isoformat(), available.isoformat()))


class ResearchStore:
    def __init__(self, state=STATE, public=PUBLIC, runtime=RUNTIME):
        self.state, self.public, self.runtime = state, public, runtime
        self.out = state / "quote-output"
        self.private = state / "asset-research"
        self.private.mkdir(parents=True, exist_ok=True)
        self.database = self.private / "research.sqlite3"
        self.cache = {}
        self.versions = {}
        self.last_view = {}
        self.relay_covered = set()
        self.daily_charts = OrderedDict()
        with closing(self.db()) as db, db:
            db.executescript(SCHEMA)
            db.execute(constituent_live.SCHEMA)
        self.intraday = IntradayCharts(self)
        self.boards = BoardHistory(self, publish)
        self.sync_catalogs()

    def db(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def feed(self, name):
        path = self.out / (name + ".json")
        try:
            version = path.stat().st_mtime_ns
            old = self.cache.get(name)
            if old and version == old[0]:
                return old[1]
            data = json.loads(path.read_bytes())
            keys = {"live-quotes": ("market",), "stock-quotes": ("stocks", "board_estimates"),
                    "board-stock-quotes": ("stocks",),
                    "sector-hot": ("items",), "sector-quotes": ("sectors",)}.get(name, ())
            if (not isinstance(data, dict) or any(not isinstance(data.get(k, []), list)
                    or any(not isinstance(row, dict) for row in data.get(k, [])) for k in keys)):
                raise ValueError("invalid feed")
            self.cache[name] = (version, data)
            return data
        except (OSError, ValueError):
            return self.cache.get(name, (None, {}))[1]

    def ingest(self, path, callback):
        try:
            stat = path.stat()
        except FileNotFoundError:
            return
        version = f"{stat.st_mtime_ns}:{stat.st_size}"
        if path == self.public / "view.json":
            version = "history125:" + version
        if self.versions.get(path) == version:
            return
        with closing(self.db()) as db, db:
            old = db.execute("SELECT version FROM inputs WHERE path=?", (str(path),)).fetchone()
            if old and old[0] == version:
                self.versions[path] = version
                return
            try:
                stream = gzip.open if path.suffix == ".gz" else open
                with stream(path, "rb") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError("invalid snapshot")
                callback(db, data)
            except (ValueError, KeyError, TypeError, AttributeError, EOFError, gzip.BadGzipFile) as error:
                db.rollback()
                self.versions[path] = version
                print("research skipped invalid file",path.name,type(error).__name__,flush=True)
                return
            db.execute("INSERT OR REPLACE INTO inputs VALUES(?,?)", (str(path), version))
        self.versions[path] = version

    @staticmethod
    def put_bars(db, kind, code, rows, source):
        values = (bar_record(kind, code, row, source) for row in rows)
        db.executemany(UPSERT, (v for v in values if v is not None))

    def sync_catalogs(self):
        def catalog(db, data):
            db.executemany(ASSETS, (("sector", x["code"], x["name"]) for x in data.get("items", []) + data.get("sw_items", [])))
        for name in ("sector-catalog.json", "ths-sector-catalog.json"):
            self.ingest(self.public / name, catalog)
        self.ingest(self.state / "sector-directory/stock-names.json",
                    lambda db, data: db.executemany(ASSETS, (("stock", c, n) for c, n in data.items() if STOCK_CODE.fullmatch(c))))
        def snapshot(db, data):
            for kind, group in (("index", "market"), ("sector", "sectors")):
                for item in data.get(group, []):
                    code = _index_code(item) if kind == "index" else item["id"]
                    if code:
                        db.execute(ASSETS, (kind, code, item["name"]))
                        for bar in item.get("history", [])[-125:]:
                            self.put_bars(db, kind, code, [bar], str(bar.get("source") or "saved_daily"))
        self.ingest(self.public / "view.json", snapshot)

    def sync_prices(self, now=None):
        # Live quotations are read on demand. Only completed bars enter history.
        def equity(db, packet):
            day = normalized_day(packet.get("trade_date"))
            values = []
            for r in packet.get("rows", []):
                code = r.get("ts_code", "")
                bar = bar_record("stock", code, r, "equity_cache")
                if bar and STOCK_CODE.fullmatch(code) and day == bar[2]:
                    values.append(bar)
            if values:
                db.executemany("INSERT OR IGNORE INTO assets VALUES('stock',?,NULL)", ((x[1],) for x in values))
                db.executemany(UPSERT, values)
                db.execute("INSERT OR REPLACE INTO coverage VALUES('stock',?,'equity_cache',?,?)",
                           (day, int(packet.get("complete") is True and len(values) == packet.get("row_count")), len(values)))
        paths = {}
        for source in (self.runtime / "data/equity/daily", self.state / "research/equity/daily"):
            for p in source.glob("*.json.gz"):
                if re.fullmatch(r"\d{8}\.json\.gz", p.name):
                    if p.name not in paths or p.stat().st_mtime_ns > paths[p.name].stat().st_mtime_ns:
                        paths[p.name] = p
        for name in sorted(paths)[-125:]:
            self.ingest(paths[name], equity)
        def daily(db, data):
            for raw, bucket in data.get("bars", {}).items():
                index = _index_code({"id": raw})
                self.put_bars(db, "index" if index else "sector", index or raw, bucket.values(), "sxdata_daily")
        self.ingest(self.out / "daily-market.json", daily)
        self.ingest(self.out / "live-quotes.json", lambda db, packet: index_previous_closes(db, packet, shanghai_now(now)))
        def reference(db, data):
            for code, row in data.get("quotes", {}).items():
                self.put_bars(db, "sector", code, [{"date": row.get("quote_date"), "close": row.get("price"),
                                                  "pct_chg": row.get("change_pct")}], "ths_daily")
        self.ingest(self.out / "sector-reference.json", reference)
        def names(db, data):
            db.executemany(ASSETS, (("stock", r["code"], r.get("name")) for r in data.get("members", [])
                                    if STOCK_CODE.fullmatch(str(r.get("code")))))
        for path in (self.out / "sector-members").glob("*.json"):
            self.ingest(path, names)

    def watch(self, db, kind, code, now):
        stamp = now.timestamp()
        key = (kind,code)
        if stamp - self.last_view.get(key, 0) < 25:
            return
        db.execute("INSERT OR REPLACE INTO watched VALUES(?,?,?)", (kind, code, stamp))
        self.last_view[key] = stamp
        if kind == "stock" and code.endswith((".SH", ".SZ", ".BJ")):
            # The relay appends at most 24 actively viewed symbols to its normal stock batch.
            rows = db.execute("""SELECT code,stamp FROM watched WHERE kind='stock' AND stamp>=?
                               ORDER BY stamp DESC,code""", (stamp - 150,))
            active = [[r[0],r[1]] for r in rows if r[0].endswith((".SH", ".SZ", ".BJ")) and r[0] not in self.relay_covered][:24]
            publish(self.private / "live-watch.json", {"stocks": active})

        elif kind == "sector" and code.endswith(".TI"):
            rows = db.execute("""SELECT code,stamp FROM watched WHERE kind='sector' AND code LIKE '%.TI'
                          AND stamp>=? ORDER BY stamp DESC,code LIMIT 8""", (stamp - 150,))
            publish(self.private / "board-watch.json", {"sectors": [list(row) for row in rows]})

    def roster(self, code):
        path = self.out / "sector-members" / (code + ".json")
        try:
            data = json.loads(path.read_bytes())
            if (not isinstance(data, dict) or data.get("code", code) != code
                    or not isinstance(data.get("members"), list)
                    or any(not isinstance(row, dict) or not isinstance(row.get("code"), str)
                           for row in data["members"])):
                return {}
            return data
        except (OSError, ValueError):
            return {}

    def capture_members(self, now):
        self.boards.capture(now)

    def live(self, kind, code):
        if kind == "index":
            ident = code[-2:].lower() + code[:6]
            return next((r for r in self.feed("live-quotes").get("market", []) if r.get("id") == ident), {})
        if kind == "stock" or code.endswith(".SI"):
            data = self.feed("stock-quotes")
            if kind == "stock":
                all_rows = data.get("stocks", [])
                self.relay_covered = set(data.get("core_stock_ids", []))
                core = next((r for r in all_rows if r.get("id") == code), {})
                market = next((r for r in self.feed("board-stock-quotes").get("stocks", [])
                               if r.get("id") == code), {})
                return market if (market.get("quote_timestamp") or "") > (core.get("quote_timestamp") or "") else core
            estimate = next((r for r in data.get("board_estimates", []) if r.get("id") == code), {})
            return {**estimate, "basis": "estimate"} if estimate else {}
        if code.endswith(".TI"):
            data = self.feed("sector-hot")
            entry = next((r for r in data.get("items", []) if r.get("code") == code), {})
            if entry:
                return {**entry, "basis":"hot", "quote_date":data.get("trade_date"), "quote_timestamp":data.get("rank_time")}
            return {}
        return next((r for r in self.feed("sector-quotes").get("sectors", []) if r.get("id") == code), {})

    def observed(self, db, kind, code):
        quote = self.live(kind, code)
        saved = db.execute("SELECT stamp,body FROM last_quotes WHERE kind=? AND code=?", (kind, code)).fetchone()
        stamp = quote.get("quote_timestamp")
        if saved and (not stamp or saved["stamp"] > stamp):
            return json.loads(saved["body"])
        if stamp:
            body = json.dumps(quote, ensure_ascii=False, separators=(",", ":"))
            if not saved or saved["body"] != body:
                db.execute("INSERT OR REPLACE INTO last_quotes VALUES(?,?,?,?)", (kind,code,stamp,body))
        return quote

    @staticmethod
    def history(db, kind, code, through):
        rows = db.execute("""SELECT day AS date,close,pre_close,pct_chg,source FROM bars
                 WHERE kind=? AND code=? AND day<=? ORDER BY day DESC LIMIT 125""", (kind,code,through))
        return list(reversed([dict(r) for r in rows]))

    def instrument(self, kind, code, now):
        with closing(self.db()) as db, db:
            asset = db.execute("SELECT name FROM assets WHERE kind=? AND code=?", (kind,code)).fetchone()
            if asset is None:
                return None
            quote = self.observed(db, kind, code)
            benchmark = self.observed(db, "index", "000300.SH")
            self.watch(db, kind, code, now)
            finished = previous_trading_day(now.date(), include_today=now.hour >= 16)
            today = now.date().isoformat()
            bars = self.history(db, kind, code, finished.isoformat()) if finished else []
            day = bars[-1]["date"] if bars else finished.isoformat() if finished else today
            bench = self.history(db, "index", "000300.SH", day)
            intraday = self.intraday.render(db,kind,code,quote,now)
            breadth = []
            members = self.roster(code) if kind == "sector" else {}
            member_live = None
            member_close = None
            if kind == "sector" and (code.endswith((".TI", ".SI")) or members.get("status") == "ready"):
                _, observations = self.boards.quotes(now)
                member_live = constituent_live.summarize(db, code, members, observations, now, normalized=True)
                self.boards.attach_partial(db, code, member_live, now)
                count = db.execute("SELECT count(*) FROM board_history_recovered WHERE day=? AND code=?",
                                   (member_live["trade_date"],code)).fetchone()[0]
                if count:
                    member_live["historical_recovered_count"] = count
            if kind == "sector" and finished:
                stock_day = db.execute("""SELECT max(day) FROM coverage WHERE kind='stock' AND complete=1
                                        AND day<=?""", (finished.isoformat(),)).fetchone()[0]
                rows = members.get("members", [])
                member_codes = [x["code"] for x in rows]
                returns = {}
                if stock_day and member_codes:
                    query = """SELECT code,pct_chg,pre_close,close FROM bars
                               WHERE kind='stock' AND day=? AND code IN (SELECT value FROM json_each(?))"""
                    close_members = []
                    for r in db.execute(query, (stock_day,json.dumps(member_codes))):
                        value = r["pct_chg"]
                        if not finite(value) and finite(r["pre_close"]) and r["pre_close"] > 0:
                            value = (r["close"]/r["pre_close"]-1)*100
                        if finite(value):
                            returns[r["code"]] = value
                        if member_live is not None and finite(r["close"]) and r["close"] > 0:
                            close_members.append({"code": r["code"], "price": r["close"],
                                                  "change_pct": value if finite(value) else None})
                    if close_members:
                        member_close = {"trade_date": stock_day, "members": close_members}
                breadth = board_breadth(member_codes, returns, stock_day,
                            membership_as_of=members.get("as_of"), checked_at=members.get("checked_at"),
                            complete=members.get("status")=="ready")
        sessions = sessions_ending(day)
        signature = tuple(tuple(row.values()) for row in bars), tuple(tuple(row.values()) for row in bench)
        key = (kind,code)
        cached = self.daily_charts.get(key)
        if not cached or cached[0] != signature:
            cached = (signature,build_daily_chart(kind,bars,bench,sessions))
            self.daily_charts[key] = cached
        self.daily_charts.move_to_end(key)
        if len(self.daily_charts) > 128:
            self.daily_charts.popitem(last=False)
        factors = compute(kind, quote, benchmark, bars, bench, sessions=sessions) + breadth
        values = factors + asset_metrics(kind, code, finished.isoformat() if finished else today,
                                        lambda name: self.feed(name.removesuffix(".json")), members)
        close = bars[-1] if bars else {}
        usable = bool(quote.get("quote_timestamp")) and (quote.get("quote_date") or "") >= (close.get("date") or "")
        if (kind == "sector" and code.endswith(".SI") and quote.get("basis") == "estimate"
                and quote.get("quote_date") == close.get("date") and finite(close.get("pct_chg"))):
            usable = False
        live_price = usable and finite(quote.get("price")) and quote["price"] > 0
        live_change = usable and finite(quote.get("change_pct"))
        close_at = close.get("date")
        price = quote["price"] if live_price else close.get("close")
        change = quote["change_pct"] if live_change else close.get("pct_chg")
        basis = quote["basis"] if quote.get("basis") in {"hot", "estimate"} else "live"
        payload = dict(schema_version=1, source="sxdata_asset_research", type=kind, code=code,
                       name=asset["name"] or quote.get("name") or code,
                       market_session_day=today,is_trading_day=is_trading_day(now.date()),
                       as_of=quote.get("quote_timestamp") if usable else close_at,
                       price=price, change_pct=change, quote_at=quote.get("quote_timestamp") if live_price else close_at,
                       change_at=quote.get("quote_timestamp") if live_change else close_at,
                       quote_basis=basis if live_price else "close", change_basis=basis if live_change else "close",
                       daily_source=close.get("source"),
                       status="ready" if all(v["value"] is not None for v in factors) else "partial",
                       metrics=values, chart={"daily":cached[1],"intraday":intraday})
        if member_live is not None:
            payload["constituent_live"] = member_live
        if member_close is not None:
            payload["constituent_close"] = member_close
        path = self.out / "asset-research" / kind / (code + ".json")
        if path.exists():
            try:
                old = json.loads(path.read_bytes())
            except (OSError, ValueError):
                old = {}
            if isinstance(old, dict) and all(old.get(k)==v for k,v in payload.items()):
                return old
        payload["generated_at"] = now.isoformat(timespec="seconds")
        publish(path, payload)
        return payload


def main():
    from factor_history_jobs import run_one
    store = ResearchStore()
    token = (Path(os.environ["CREDENTIALS_DIRECTORY"]) / "sxdata-token").read_text().strip()
    def ingest_loop():
        while True:
            try:
                store.sync_catalogs()
                store.sync_prices()
            except (OSError,ValueError,sqlite3.Error,KeyError) as e:
                print("research cache",type(e).__name__,flush=True)
            time.sleep(100)
    def capture_loop():
        while True:
            try:
                now = shanghai_now()
                store.intraday.capture(now)
                store.capture_members(now)
            except (OSError, ValueError, sqlite3.Error, KeyError) as e:
                print("research intraday cache",type(e).__name__,flush=True)
            time.sleep(4)
    def daily_loop():
        while True:
            try:
                delay = run_one(store, token, shanghai_now())
            except (ValueError, OSError, sqlite3.Error) as e:
                print("research daily",type(e).__name__,flush=True)
                delay=60
            time.sleep(max(1,delay))
    def valuation_loop():
        attempts = {}
        while True:
            try:
                delay = collect(store.out, token, shanghai_now(), attempts=attempts)
            except (OSError, ValueError, KeyError, TypeError) as error:
                print("research valuation", type(error).__name__, flush=True)
                delay = 120
            time.sleep(delay)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            match = ROUTE.fullmatch(urlsplit(self.path).path)
            if not match:
                self.send_error(404)
                return
            try:
                payload=store.instrument(*match.groups(),shanghai_now())
            except (OSError,ValueError,sqlite3.Error,KeyError,TypeError) as e:
                print("research serve",type(e).__name__,flush=True)
                self.send_error(503)
                return
            if payload is None:
                self.send_error(404)
                return
            body=json.dumps(payload,ensure_ascii=False,allow_nan=False,separators=(",",":")).encode()
            self.send_response(200)
            self.send_header("Content-Type","application/json; charset=utf-8")
            self.send_header("Cache-Control","no-store")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self,*args):
            pass
    sock=Path(os.environ["RUNTIME_DIRECTORY"])/"research.sock"
    sock.unlink(missing_ok=True)
    with UnixStreamServer(str(sock),Handler) as server:
        os.chmod(sock,0o660)
        for fn in (ingest_loop,capture_loop,daily_loop,valuation_loop):
            threading.Thread(target=fn,daemon=True).start()
        server.serve_forever()


if __name__=="__main__":
    main()
