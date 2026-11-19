"""Fill missing official daily bars for factor research, only outside the trading day."""
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import time
import math
import re
import sqlite3

from pipeline.calendar import is_trading_day, previous_trading_day, shanghai_now
from pipeline.data_service import gate_state
from pipeline.stock_api import FIELDS, _Failure
from sector_directory import day_text, local_request

SESSIONS = 61
PACE = 25
BENCHMARK = "000300.SH"
EQUITY = re.compile(r"(?:6[0-9]{5}\.SH|[03][0-9]{5}\.SZ|[489][0-9]{5}\.BJ)")
EXCHANGE = re.compile(r"[0-9]{6}\.(?:SH|SZ)")
THS = re.compile(r"[0-9]{6}R?\.TI")
SW = re.compile(r"[0-9]{6}\.SI")
CROSS = {"daily": "stock", "ths_daily": "sector", "sw_daily": "sector"}
FIELDS.setdefault("daily", "ts_code,trade_date,close,pre_close,pct_chg")


def eligible(now):
    traded = is_trading_day(now.date())
    local = now.time().replace(tzinfo=None)
    # Leave the client's full time budget before the 07:00 cutoff.
    return traded is False or traded is True and (local < time(6, 59, 40) or local >= time(15, 45))


def window(now):
    session = previous_trading_day(now.date(), include_today=now.hour >= 15)
    days = []
    while session is not None and len(days) < SESSIONS:
        days.append(session.isoformat())
        session = previous_trading_day(session)
    return days


def data_api(kind, code):
    if kind == "stock" and EQUITY.fullmatch(code):
        return "daily"
    if kind == "sector":
        if THS.fullmatch(code):
            return "ths_daily"
        if SW.fullmatch(code):
            return "sw_daily"
    if kind in {"index", "sector"} and EXCHANGE.fullmatch(code):
        return "index_daily"
    return None


@dataclass
class Job:
    api: str
    key: str
    kind: str
    allowed: set
    days: set
    priority: tuple

    def params(self):
        if self.api in CROSS:
            return {"trade_date": self.key.replace("-", "")}
        return {"ts_code": next(iter(self.allowed)), "start_date": min(self.days).replace("-", ""),
                "end_date": max(self.days).replace("-", "")}


def choose(db, days, now, blocked):
    """Choose one date-wide or exact-code request, preferring recent sessions and watched gaps."""
    family = defaultdict(set)
    indexed = []
    for kind, code in db.execute("SELECT kind,code FROM assets"):
        api = data_api(kind, code)
        if api in CROSS:
            family[api].add(code)
        elif api == "index_daily":
            indexed.append((kind,code))
    tried = {(api, key) for api, key, attempted_day, next_at in db.execute(
        "SELECT api,task,attempted_day,next_at FROM factor_history_jobs")
        if attempted_day == now.date().isoformat() or next_at > now.timestamp()}
    covered = set()
    for kind, day, source, complete, count in db.execute(
            "SELECT kind,day,source,complete,rows FROM coverage WHERE day>=?", (days[-1],)):
        api = "daily" if source == "equity_cache" else source
        if api not in CROSS or not complete or kind != CROSS[api]:
            continue
        # A dated, explicitly complete local market file can include halted/new stocks.
        if api != "daily" or count >= max(2, math.ceil(len(family["daily"])*0.90)):
            covered.add((api, day))

    present = defaultdict(set)
    watched = {(kind, code):stamp for kind, code, stamp in db.execute(
        "SELECT kind,code,stamp FROM watched ORDER BY stamp DESC LIMIT 128")}
    sector_counts = defaultdict(int)
    for kind, code, day in db.execute("""SELECT kind,code,day FROM bars
            WHERE kind IN ('sector','index') AND day BETWEEN ? AND ?""", (days[-1],days[0])):
        api = data_api(kind, code)
        if api == "index_daily" or (kind, code) in watched:
            present[kind, code].add(day)
        if api in CROSS and code in family[api]:
            sector_counts[api,day] += 1
    wanted = defaultdict(float)
    for (kind, code), stamp in watched.items():
        api = data_api(kind, code)
        if api not in CROSS or code not in family[api]:
            continue
        if kind == "stock":
            present[kind,code].update(row[0] for row in db.execute(
                "SELECT day FROM bars WHERE kind=? AND code=? AND day BETWEEN ? AND ?",
                (kind,code,days[-1],days[0])))
        for day in days:
            if day not in present[kind,code]:
                wanted[api,day] = max(wanted[api,day],stamp)

    jobs = []
    for depth, day in enumerate(days):
        for order, (api, kind) in enumerate(CROSS.items()):
            codes = family[api]
            if (not codes or api in blocked or (api,day) in tried or (api,day) in covered
                    or sector_counts[api,day] >= len(codes)):
                continue
            priority = 0 if depth < 2 else 1 if wanted[api,day] else 2
            jobs.append(Job(api,day,kind,codes,{day}, (priority,depth,-wanted[api,day],order,day)))
    if "index_daily" not in blocked:
        for kind, code in indexed:
            key = kind+"/"+code
            if ("index_daily",key) in tried:
                continue
            gaps = [i for i, day in enumerate(days) if day not in present[kind,code]]
            if not gaps:
                continue
            stamp = watched.get((kind,code),0)
            priority = 0 if code == BENCHMARK else 1 if stamp else 3
            jobs.append(Job("index_daily",key,kind,{code},{days[i] for i in gaps},
                            (priority,gaps[0],-stamp,-1 if code==BENCHMARK else 4,code)))
    return min(jobs,key=lambda job:job.priority) if jobs else None


def save_rows(store, db, job, rows):
    accepted = {}
    conflicts = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        code, day, close = row.get("ts_code"), day_text(row.get("trade_date")), row.get("close")
        if not isinstance(code,str) or day not in job.days:
            continue
        if job.api == "daily":
            if not EQUITY.fullmatch(code):
                continue
        elif code not in job.allowed:
            continue
        if type(close) not in (int,float) or not math.isfinite(close) or close <= 0:
            continue
        bar = {"date":day,"close":close}
        for field, value in (("pre_close",row.get("pre_close")),("pct_chg",row.get("pct_chg",row.get("pct_change")))):
            if type(value) in (int,float) and math.isfinite(value) and (field!="pre_close" or value>0):
                bar[field] = value
        key = code, day
        if key in accepted and accepted[key] != bar:
            conflicts.add(key)
        accepted[key] = bar
    by_code = defaultdict(list)
    for key, bar in accepted.items():
        if key not in conflicts:
            by_code[key[0]].append(bar)
    if job.api == "daily":
        db.executemany("INSERT OR IGNORE INTO assets(kind,code,name) VALUES('stock',?,NULL)",
                       ((code,) for code in by_code))
    for code, bars in by_code.items():
        store.put_bars(db,job.kind,code,bars,job.api)
    count = sum(map(len,by_code.values()))
    if job.api in CROSS:
        # Only verified full coverage can suppress a future retry. In particular, a
        # partial stock slice from another observer is never a market-wide receipt.
        complete = job.allowed.issubset(by_code)
        if job.api == "daily":
            # Common provider cutoffs cannot certify a whole-market batch.
            complete = complete and len(job.allowed) >= 1000 and len(rows) not in {2000,3000,5000,6000}
        db.execute("""INSERT INTO coverage(kind,day,source,complete,rows) VALUES(?,?,?,?,?)
            ON CONFLICT(kind,day,source) DO UPDATE SET complete=max(complete,excluded.complete),
            rows=max(rows,excluded.rows)""", (job.kind,job.key,job.api,int(complete),count))
    else:
        complete = count == len(job.days)
    return count, "ready" if complete else "partial" if count else "empty"


def hold(db, name, until):
    db.execute("""INSERT INTO factor_history_cooldowns(scope,until) VALUES(?,?)
        ON CONFLICT(scope) DO UPDATE SET until=max(until,excluded.until)""", (name,until))


def run_one(store, token, now):
    now = shanghai_now(now)
    if not eligible(now):
        return 60
    days = window(now)
    if not days:
        return 3600
    stamp, today = now.timestamp(), now.date().isoformat()
    with closing(store.db()) as db:
        # Claim a request before releasing SQLite for network I/O. Two service calls
        # or a restart cannot start the same request twice on the same local date.
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS factor_history_jobs(api TEXT,task TEXT,
                attempted_day TEXT,next_at REAL,status TEXT,rows INTEGER,PRIMARY KEY(api,task)) WITHOUT ROWID""")
            db.execute("""CREATE TABLE IF NOT EXISTS factor_history_cooldowns(scope TEXT PRIMARY KEY,
                until REAL) WITHOUT ROWID""")
            guarded = dict(db.execute("SELECT scope,until FROM factor_history_cooldowns WHERE until>?",(stamp,)))
            if "*" in guarded:
                db.commit()
                return max(1,math.ceil(guarded["*"]-stamp))
            job = choose(db,days,now,guarded)
            if not job:
                db.commit()
                return min(300,max(1,math.ceil(min(guarded.values())-stamp))) if guarded else 300
            db.execute("""INSERT INTO factor_history_jobs VALUES(?,?,?,?,'claimed',0)
                ON CONFLICT(api,task) DO UPDATE SET attempted_day=excluded.attempted_day,
                next_at=excluded.next_at,status='claimed'""",(job.api,job.key,today,stamp+PACE))
            hold(db,"*",stamp+PACE)
            db.commit()
        except sqlite3.OperationalError as error:
            db.rollback()
            if "locked" in str(error).lower() or "busy" in str(error).lower():
                return PACE
            raise
        try:
            rows = local_request(token,job.api,job.params(),False)
        except _Failure as error:
            # A local lock/cooldown did not start a relay request. It may be tried
            # after the cooldown; actual upstream requests wait until another date.
            wait = max(PACE, error.retry_seconds, 0 if error.deferred else 1800)
            host_cool = gate_state("__factor_host__")[0]
            host_failure = error.host or error.code in {"local_rate_limit","gate_unavailable","relay_busy"}
            with db:
                if host_failure:
                    hold(db,"*",stamp+max(wait,host_cool))
                else:
                    hold(db,job.api,stamp+wait)
                    if host_cool:
                        hold(db,"*",stamp+host_cool)
                db.execute("""UPDATE factor_history_jobs SET attempted_day=?,next_at=?,status=?
                    WHERE api=? AND task=?""",("" if error.deferred else today,stamp+wait,error.code,job.api,job.key))
            return max(wait if host_failure else PACE,math.ceil(host_cool))
        with db:
            count, status = save_rows(store,db,job,rows)
            db.execute("UPDATE factor_history_jobs SET next_at=?,status=?,rows=? WHERE api=? AND task=?",
                       (stamp+(PACE if status=="ready" else 3600),status,count,job.api,job.key))
        return PACE
