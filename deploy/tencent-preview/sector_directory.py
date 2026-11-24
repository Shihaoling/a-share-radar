"""Publish sector heat, daily references and paginated constituent lists from SXData."""
import hashlib
from contextlib import closing
from datetime import date, datetime, timedelta
import fcntl
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

from pipeline.calendar import is_trading_day, market_session, previous_trading_day, shanghai_now
from pipeline.data_service import gate_state
from pipeline.stock_api import FIELDS, _Client, _Failure
from intraday_chart import archive_heat
from relay_common import STATE
from relay_quotes import publish

PUBLIC = Path("/srv/ashare-radar-preview/public/data")
SIZE = 2500
BATCH_CODES = 4
BATCH_MEMBER_BUDGET = 1200
PARTIAL_RETRY_SECONDS = 3600
BATCH_KEYS = {"ths_member": "ts_code", "index_weight": "index_code"}
CODE = re.compile(r"[0-9]{6}(?:\.(?:SH|SZ|SI)|R?\.TI)")
STOCK = re.compile(r"[0-9A-Z-]{1,12}\.[A-Z]{2,4}")
SPECIAL = {"@hot": "ths_hot", "@daily": "ths_daily", "@names": "stock_basic"}
DIRECTORY_FIELDS = dict(ths_hot="trade_date,data_type,ts_code,ts_name,rank,pct_change,current_price,hot,rank_time",
              ths_daily="ts_code,trade_date,close,pct_change", stock_basic="ts_code,name",
              ths_member="ts_code,con_code,con_name", index_weight="index_code,con_code,trade_date,weight",
              index_member_all="l1_code,ts_code,name,in_date,out_date,is_new")
for api, fields in DIRECTORY_FIELDS.items():
    FIELDS[api] = ",".join(dict.fromkeys([*FIELDS.get(api, "").split(","), *fields.split(",")])).lstrip(",")


def mapping(path):
    if not path.exists():
        return {}
    return json.loads(path.read_bytes())


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def api_for(code):
    if code in SPECIAL:
        return SPECIAL[code]
    return {"TI": "ths_member", "SI": "index_member_all"}.get(code.rsplit(".", 1)[-1], "index_weight")


def day_text(value):
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        return None


def latest_hot(rows, now, known):
    """Do not splice different ranking times or combine two separately ranked universes."""
    snapshots = {}
    for row in rows:
        code, rank = row.get("ts_code"), row.get("rank")
        if code not in known or row.get("data_type") != "概念板块" or type(rank) is not int or not 1 <= rank <= 10:
            continue
        try:
            stamp = datetime.fromisoformat(str(row["rank_time"]))
            stamp = stamp.astimezone(now.tzinfo) if stamp.tzinfo else stamp.replace(tzinfo=now.tzinfo)
        except (ValueError, KeyError):
            continue
        if stamp > now or day_text(row.get("trade_date")) != stamp.date().isoformat():
            continue
        hot = row.get("hot")
        if not number(hot) or hot < 0:
            continue
        record = dict(code=code, name=known[code]["name"], rank=rank, hot=hot)
        if number(row.get("pct_change")):
            record["change_pct"] = row["pct_change"]
        if number(row.get("current_price")) and row["current_price"] > 0:
            record["price"] = row["current_price"]
        table = snapshots.setdefault(stamp, {})
        if rank in table and table[rank] != record:
            table[rank] = None
        else:
            table[rank] = record
    for stamp, table in sorted(snapshots.items(), reverse=True):
        if len(table) == 10 and all(table.values()) and len({item["code"] for item in table.values()}) == 10:
            return dict(schema_version=1, source="sxdata_ths_hot", trade_date=stamp.date().isoformat(),
                        rank_time=stamp.isoformat(), items=[table[i] for i in range(1, 11)])
    return None


def reference(rows, day, now, known):
    quotes = {}
    for row in rows:
        code, price = row.get("ts_code"), row.get("close")
        if code in known and code.endswith(".TI") and day_text(row.get("trade_date")) == day and number(price) and price > 0:
            change = row.get("pct_change")
            quotes[code] = dict(price=price, change_pct=change if number(change) else None, quote_date=day)
    return dict(schema_version=1, source="sxdata_sector_reference", trade_date=day,
                generated_at=now.isoformat(timespec="seconds"), quotes=quotes)


def publish_reference(path, data):
    if not data["quotes"]:
        return False
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        old = mapping(path)
        if data["trade_date"] < old.get("trade_date", ""):
            return False
        if data["trade_date"] == old.get("trade_date"):
            data = {**data, "quotes": {**old.get("quotes", {}), **data["quotes"]}}
        publish(path, data)
    return True


def local_request(token, api, params, active):
    gate = Path(os.environ["RADAR_SERVICE_GATE_FILE"])
    with gate.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise _Failure("relay_busy", deferred=True) from None
        cool, gap, reason = gate_state(api)
        if cool:
            raise _Failure(reason, math.ceil(cool), deferred=True)
        if gap > (1.4 if active else 6):
            raise _Failure("relay_busy", deferred=True)
        timeout = 5 if active else 10
        client = _Client(token, {"max_requests": 1, "min_interval_seconds": 6,
                        "timeout_seconds": timeout, "budget_seconds": timeout+2 if active else 17}, shanghai_now())
        return client.request(api, params)


class Directory:
    def __init__(self, public=PUBLIC, state=STATE):
        self.out = state / "quote-output"
        self.members = self.out / "sector-members"
        self.members.mkdir(parents=True, exist_ok=True)
        self.private = state / "sector-directory"
        self.private.mkdir(exist_ok=True)
        self.database = self.private / "work.sqlite3"
        index = mapping(public / "sector-catalog.json")
        other = mapping(public / "ths-sector-catalog.json")
        self.known = {x["code"]: x for x in index["items"] + other["items"] + other["sw_items"]
                      if CODE.fullmatch(x["code"])}
        self.names = mapping(self.private / "stock-names.json")
        with closing(self.connect()) as db, db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (code TEXT PRIMARY KEY, due REAL DEFAULT 0,
                    requested REAL DEFAULT 0, attempted REAL DEFAULT 0, failures INTEGER DEFAULT 0,
                    cursor INTEGER DEFAULT 0, day TEXT DEFAULT '', signature TEXT DEFAULT '', reason TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS parts (board TEXT, stamp TEXT, stock TEXT, name TEXT, weight REAL,
                    PRIMARY KEY(board,stamp,stock)) WITHOUT ROWID;
            """)
            if "single_only" not in {row[1] for row in db.execute("PRAGMA table_info(jobs)")}:
                db.execute("ALTER TABLE jobs ADD COLUMN single_only INTEGER NOT NULL DEFAULT 0")
            db.executemany("INSERT OR IGNORE INTO jobs(code) VALUES(?)", ((c,) for c in [*SPECIAL, *self.known]))
        self.yield_since = None
        self.seed_partial(public, state)

    def connect(self):
        db = sqlite3.connect(self.database, timeout=4)
        db.row_factory = sqlite3.Row
        return db

    def seed_partial(self, public, state):
        weights = mapping(state / "relay" / "industry-weights.json")
        saved = mapping(state / "research" / "inputs.json").get("memberships", {})
        runtime = Path("/opt/ashare-radar-preview/runtime/web/data/dashboard.json")
        dashboard = mapping(runtime) if state == STATE else mapping(public / "dashboard.json")
        view = mapping(public / "view.json")
        sources = [(c, x.get("records", []), x.get("fetched_at"), len(x.get("records", [])), x.get("complete"))
                   for c, x in saved.items()]
        sources.extend((x.get("id"), x.get("constituents", []), x.get("membership_as_of"),
                        x.get("membership_count"), x.get("membership_complete")) for x in dashboard.get("sectors", []))
        sources.extend((x.get("id"), x.get("stocks", []), x.get("membership_as_of"),
                        x.get("membership_count"), False) for x in view.get("sectors", []))
        sources.extend((c, x.get("stocks", []), x.get("date"), None, False) for c, x in weights.items())
        for code, stocks, as_of, expected, complete in sources:
            if code not in self.known or not stocks:
                continue
            checked = day_text(str(as_of)[:10])
            listed = {s["ts_code"]: dict(code=s["ts_code"], name=s.get("name") or self.names.get(s["ts_code"]),
                       weight_pct=s.get("weight_pct")) for s in stocks if STOCK.fullmatch(str(s.get("ts_code")))}
            ready = complete is True and len(listed) == expected and checked is not None
            path = self.members / (code + ".json")
            old = mapping(path)
            if (not listed or old.get("status") == "ready" and not ready
                    or (old.get("as_of") or str(old.get("checked_at") or "")[:10]) > (checked or "")
                    or old and not ready and len(old.get("members", [])) >= len(listed)):
                continue
            publish(path, dict(schema_version=1, source="sxdata_sector_members", code=code,
                    status="ready" if ready else "partial", method=api_for(code), as_of=checked,
                    checked_at=as_of if isinstance(as_of, str) and "T" in as_of else None,
                    expected_count=expected, members=list(listed.values())))

    def demand(self, code, now):
        if code not in self.known:
            return False
        with closing(self.connect()) as db, db:
            db.execute("UPDATE jobs SET requested=? WHERE code=? AND requested=0", (now.timestamp(), code))
        return True

    def scheduling(self, db, stamp):
        jobs = db.execute("""SELECT * FROM jobs WHERE due<=? ORDER BY
            CASE WHEN code LIKE '@%' THEN 0 WHEN requested>0 THEN 1 WHEN cursor>0 THEN 2 WHEN single_only=1 THEN 3 ELSE 4 END,
            requested, attempted, code""", (stamp,))
        return next((row for row in jobs if row["code"] in SPECIAL or row["code"] in self.known), None)

    def background_batch(self, db, first, stamp):
        code = first["code"]
        api = api_for(code)
        jobs = [first]
        if api not in BATCH_KEYS or first["requested"] or first["cursor"] or first["single_only"]:
            return jobs
        # Leave headroom below the provider's row limit. Weight responses can span
        # several effective dates even for a small index, so still check the actual response.
        size = self.known[code].get("member_count") or 0
        venues = ("TI", "TI") if api == "ths_member" else ("SH", "SZ")
        candidates = db.execute("""SELECT * FROM jobs WHERE due<=? AND code!=? AND requested=0
            AND cursor=0 AND single_only=0 AND substr(code,-2) IN (?,?) ORDER BY attempted,code""",
            (stamp, code, *venues))
        for candidate in candidates:
            other = self.known.get(candidate["code"])
            if not other:
                continue
            count = other.get("member_count") or 0
            if size + count > BATCH_MEMBER_BUDGET:
                continue
            jobs.append(candidate)
            size += count
            if len(jobs) == BATCH_CODES:
                break
        return jobs

    def params(self, code, cursor, day, now):
        compact = day.replace("-", "")
        if code == "@hot":
            return {"trade_date": compact, "market": "概念板块", "is_new": "N"}
        if code == "@daily":
            return {"trade_date": compact}
        if code == "@names":
            return {"list_status": "L", "limit": 6000}
        page = {"limit": SIZE, "offset": cursor}
        if code.endswith(".TI"):
            return dict(ts_code=code, **page)
        if code.endswith(".SI"):
            return dict(l1_code=code, is_new="Y", **page)
        start = date.fromisoformat(day) - timedelta(days=72)
        return dict(index_code=code, start_date=start.strftime("%Y%m%d"), end_date=compact, **page)

    def finish_special(self, code, rows, day, now):
        if code == "@hot":
            path = self.out / "sector-hot.json"
            data = latest_hot(rows, now, self.known)
            if not data or data["rank_time"] < mapping(path).get("rank_time",""):
                return False
            publish(path, data)
            archive_heat(self.out / "sector-heat-samples.json", rows, self.known, now, publish)
            for item in data["items"]:
                self.demand(item["code"], now)
        elif code == "@daily":
            path = self.out / "sector-reference.json"
            data = reference(rows, day, now, self.known)
            return publish_reference(path, data)
        else:
            names = {x["ts_code"]: x["name"] for x in rows if STOCK.fullmatch(str(x.get("ts_code")))
                     and isinstance(x.get("name"), str) and x["name"]}
            if not names:
                return False
            self.names = {**self.names, **names}
            publish(self.private / "stock-names.json", self.names)
        return True

    def member_row(self, api, board, row, day):
        """Return one identified, eligible constituent or the reason this row was ignored."""
        if not isinstance(row, dict):
            return None, "invalid_rows"
        key = BATCH_KEYS.get(api, "l1_code")
        if row.get(key) != board:
            return None, "other_board"
        stock = row.get("ts_code" if api == "index_member_all" else "con_code")
        if not isinstance(stock, str) or not STOCK.fullmatch(stock):
            return None, "invalid_rows"
        weight, stamp = None, ""
        if api == "index_weight":
            stamp = day_text(row.get("trade_date"))
            if not stamp or stamp > day:
                return None, "invalid_rows"
            weight = row.get("weight")
            if not number(weight) or not 0 <= weight <= 100:
                return None, "invalid_rows"
            name = self.names.get(stock)
        elif api == "ths_member":
            name = row.get("con_name")
        else:
            if (row.get("is_new") == "N" or (day_text(row.get("in_date")) or "") > day
                    or (day_text(row.get("out_date")) or "9999") <= day):
                return None, "inactive_member"
            name = row.get("name")
        if name is not None and not isinstance(name, str):
            return None, "invalid_rows"
        return (board, stamp, stock, name or self.names.get(stock), weight), None

    def finish_page(self, db, job, rows, day, now, *, combined=False):
        code, cursor = job["code"], job["cursor"]
        api = api_for(code)
        seen, conflicts, issues = {}, set(), set()
        for row in rows:
            member, issue = self.member_row(api, code, row, day)
            if member is None:
                issues.add(issue)
                continue
            key = (member[1], member[2])
            if key in seen and seen[key] != member:
                conflicts.add(key)
                issues.add("invalid_rows")
            seen[key] = member
        batch = [member for key, member in seen.items() if key not in conflicts]
        signature = hashlib.sha256(repr(sorted((r[1], r[2]) for r in batch)).encode()).hexdigest()
        repeated = cursor and len(rows) >= SIZE and signature == job["signature"]
        if not cursor:
            db.execute("DELETE FROM parts WHERE board=?", (code,))
        db.executemany("INSERT OR REPLACE INTO parts VALUES(?,?,?,?,?)", batch)
        dirty = "invalid_rows" in issues or (cursor and job["reason"] == "invalid_rows")
        if len(rows) >= SIZE and not repeated and cursor < 100000:
            db.execute("""UPDATE jobs SET cursor=cursor+?,signature=?,day=?,due=?,attempted=?,reason=?
                WHERE code=?""", (SIZE, signature, day, now.timestamp()+6, now.timestamp(),
                                  "invalid_rows" if dirty else "paging", code))
            return
        stamp = db.execute("SELECT max(stamp) FROM parts WHERE board=?", (code,)).fetchone()[0]
        stocks = [dict(code=r["stock"],name=r["name"] or self.names.get(r["stock"]),weight_pct=r["weight"])
                  for r in db.execute("SELECT * FROM parts WHERE board=? AND stamp=? ORDER BY weight DESC,stock", (code,stamp))]
        expected = self.known[code].get("member_count")
        if dirty:
            reason = "invalid_rows"
        elif repeated or len(rows) >= SIZE:
            reason = "page_limit"
        elif not stocks:
            reason = "no_matching_rows" if issues or cursor else "no_rows"
        elif expected and len(stocks) < expected:
            reason = "missing_members"
        elif api == "index_weight" and not 97 <= sum(x["weight_pct"] or 0 for x in stocks) <= 103:
            reason = "partial_weights"
        else:
            reason = "ready"
        path = self.members / (code + ".json")
        old = mapping(path)
        newer = (stamp or day) >= (old.get("as_of") or "")
        if not newer:
            reason = "older_reference"
        if combined and reason != "ready":
            self.retry_singly(db, [job], now)
            return

        ready = reason == "ready"
        if newer and (ready or old.get("status") != "ready"):
            listed, as_of = stocks, stamp or None
            if not ready and len(old.get("members", [])) > len(listed):
                listed, as_of = old["members"], old.get("as_of")
            payload = dict(schema_version=1, source="sxdata_sector_members", code=code,
                           status="ready" if ready else "partial", method=api, as_of=as_of,
                           checked_at=now.isoformat(timespec="seconds"), expected_count=expected, members=listed)
            if not ready:
                payload["reason"] = "previous_members" if not stocks and listed else reason
            publish(path, payload)
        delay = (3*86400 if api == "index_weight" else 86400) if ready else PARTIAL_RETRY_SECONDS
        db.execute("DELETE FROM parts WHERE board=?", (code,))
        db.execute("""UPDATE jobs SET cursor=0,day='',signature='',attempted=?,due=?,requested=0,failures=0,
                      single_only=0,reason=? WHERE code=?""", (now.timestamp(),now.timestamp()+delay,reason,code))

    def retry_singly(self, db, jobs, now):
        db.executemany("DELETE FROM parts WHERE board=?", ((job["code"],) for job in jobs))
        db.executemany("""UPDATE jobs SET single_only=1,reason='single_check',attempted=?,due=? WHERE code=?""",
                       ((now.timestamp(), now.timestamp(), job["code"]) for job in jobs))

    def finish_batch(self, db, jobs, api, rows, day, now):
        key = BATCH_KEYS[api]
        groups = {job["code"]: [] for job in jobs}
        # A full page has no provable per-board end. Restart each code at offset zero.
        if len(rows) >= SIZE or any(not isinstance(row, dict) or not isinstance(row.get(key), str)
                                    or not CODE.fullmatch(row[key]) for row in rows):
            self.retry_singly(db, jobs, now)
            return
        for row in rows:
            if row[key] in groups:
                groups[row[key]].append(row)
        for job in jobs:
            self.finish_page(db, job, groups[job["code"]], day, now, combined=True)

    def apply(self, db, job, rows, day, now):
        code = job["code"]
        if code not in SPECIAL:
            self.finish_page(db, job, rows, day, now)
            return
        ok = self.finish_special(code, rows, day, now)
        interval = {"@hot":1800,"@daily":3600,"@names":7*86400}[code] if ok else 1800
        db.execute("UPDATE jobs SET due=?,attempted=?,reason=?,failures=0 WHERE code=?",
                   (now.timestamp()+interval, now.timestamp(),"ready" if ok else "no_rows",code))

    def work(self, token, now):
        phase = market_session(now)["phase"]
        trading = phase in {"morning","afternoon","closing_auction"}
        active = trading or phase == "opening_auction"
        with closing(self.connect()) as db:
            job = self.scheduling(db, now.timestamp())
            if job is None:
                return 12
            code = job["code"]
            day = job["day"] or now.date().isoformat()
            if code in {"@daily","@hot"}:
                target = previous_trading_day(now.date(), include_today=code=="@hot" or now.hour>=19)
                if target is None:
                    return 60
                day = target.isoformat()
            if code=="@hot" and (is_trading_day(now.date()) is not True or now.hour<8 or now.hour>22):
                with db:
                    db.execute("UPDATE jobs SET due=? WHERE code=?", (now.timestamp()+1800,code))
                return 1
            api = api_for(code)
            jobs = self.background_batch(db, job, now.timestamp())
            params = self.params(code, job["cursor"], day, now)
            if len(jobs) > 1:
                params[BATCH_KEYS[api]] = ",".join(item["code"] for item in jobs)
            try:
                yielding = trading or active and (self.yield_since is None or time.monotonic() - self.yield_since < 300)
                rows = local_request(token, api, params, yielding)
                self.yield_since = None
            except _Failure as e:
                if e.code == "relay_busy":
                    if self.yield_since is None:
                        self.yield_since = time.monotonic()
                    return 9 if active else 2
                retry = max(8, e.retry_seconds, 0 if e.deferred else min(21600,60*2**min(8,job["failures"])))
                with db:
                    db.executemany("""UPDATE jobs SET due=?,attempted=?,failures=failures+?,
                        reason=CASE WHEN cursor>0 AND reason='invalid_rows' THEN reason ELSE ? END,
                        single_only=max(single_only,?) WHERE code=?""",
                        ((now.timestamp()+retry, now.timestamp(), int(not e.deferred), e.code,
                          int(len(jobs)>1 and not e.deferred), item["code"]) for item in jobs))
                # A shared cooldown applies to all jobs using that API. Skip the queue as a group.
                if gate_state(api)[0] > 2:
                    pattern = {"index_weight":( "%.SH","%.SZ"),"index_member_all":("%.SI",),"ths_member":("%.TI",)}.get(api,())
                    with db:
                        for pat in pattern:
                            db.execute("UPDATE jobs SET due=max(due,?) WHERE code LIKE ?",(now.timestamp()+gate_state(api)[0],pat))
                return 4 if e.deferred else 18
            with db:
                observed = shanghai_now()
                if len(jobs) > 1:
                    self.finish_batch(db, jobs, api, rows, day, observed)
                else:
                    self.apply(db, job, rows, day, observed)
            print(json.dumps({"sector_job":code,"api":api,"codes":len(jobs),"rows":len(rows)}),flush=True)
        return 1 if trading else 60 if active else 8


def background_delay(now):
    if market_session(now)["phase"] not in {"morning","afternoon","closing_auction"}:
        return 0
    second = now.second + now.microsecond / 1_000_000
    return 0 if 56 <= second < 57.5 else (56 - second) % 60


def serve(directory, token):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            m = re.fullmatch(r"/data/sector-members/(" + CODE.pattern + r")\.json", path)
            if not m or m[1] not in directory.known:
                self.send_error(404)
                return
            code = m[1]
            path = directory.members / (code + ".json")
            try:
                body = path.read_bytes()
                ready = json.loads(body).get("status") == "ready"
                status = 200
            except FileNotFoundError:
                ready = False
                status = 202
                body = json.dumps(dict(schema_version=1, source="sxdata_sector_members", code=code,
                                  status="pending", method=api_for(code), members=[])).encode()
            if not ready:
                directory.demand(code, shanghai_now())
            self.send_response(status)
            self.send_header("Content-Type","application/json")
            self.send_header("Cache-Control","no-store")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass

    sock = Path(os.environ["RUNTIME_DIRECTORY"]) / "members.sock"
    sock.unlink(missing_ok=True)
    with UnixStreamServer(str(sock),Handler) as server:
        os.chmod(sock,0o660)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        while True:
            try:
                now = shanghai_now()
                delay = background_delay(now) or directory.work(token,now)
            except (OSError,ValueError,sqlite3.Error) as e:
                print(json.dumps({"sector_job_error":type(e).__name__}),flush=True)
                delay=20
            time.sleep(delay)


if __name__=="__main__":
    token=(Path(os.environ["CREDENTIALS_DIRECTORY"])/"sxdata-token").read_text().strip()
    serve(Directory(),token)
