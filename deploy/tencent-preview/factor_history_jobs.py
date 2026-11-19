"""Fill missing official daily bars for factor research, only outside the trading day."""
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, time, timedelta
import math
import re
import sqlite3

from pipeline.calendar import is_trading_day, previous_trading_day, shanghai_now
from pipeline.data_service import gate_state
from pipeline.stock_api import FIELDS, _Failure
from research_factors import trusted_daily_source
from sector_directory import day_text, local_request

SESSIONS = 61
PACE = 25
BENCHMARK = "000300.SH"
EQUITY = re.compile(r"(?:6[0-9]{5}\.SH|[03][0-9]{5}\.SZ|[489][0-9]{5}\.BJ)")
EXCHANGE = re.compile(r"[0-9]{6}\.(?:SH|SZ)")
THS = re.compile(r"[0-9]{6}R?\.TI")
SW = re.compile(r"[0-9]{6}\.SI")
CROSS = {"daily": "stock", "ths_daily": "sector", "sw_daily": "sector"}
SECTOR_DAILY = {"ths_daily", "sw_daily"}
RECENT_DAYS = 7
RANGE_GAPS = 5
LANES = ("watched", "market", "index", "market")
FIELDS.setdefault("daily", "ts_code,trade_date,close,pre_close,pct_chg")


def eligible(now):
    traded = is_trading_day(now.date())
    local = now.time().replace(tzinfo=None)
    # Leave the client's full time budget before the 07:00 cutoff.
    return traded is False or traded is True and (local < time(6, 59, 40) or local >= time(15))


def window(now):
    session = previous_trading_day(now.date(), include_today=now.time() >= time(15, 45))
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

    @property
    def cross_section(self):
        return self.api in CROSS and "/" not in self.key

    def params(self):
        if self.cross_section:
            return {"trade_date": self.key.replace("-", "")}
        return {"ts_code": next(iter(self.allowed)), "start_date": min(self.days).replace("-", ""),
                "end_date": max(self.days).replace("-", "")}


def choose(db, days, now, blocked, turn=0):
    """Choose one date-wide or exact-code request, preferring recent sessions and watched gaps."""
    family = defaultdict(set)
    indexed = []
    for kind, code in db.execute("SELECT kind,code FROM assets"):
        api = data_api(kind, code)
        if api in CROSS:
            family[api].add(code)
        elif api == "index_daily":
            indexed.append((kind,code))
    tried = {tuple(row) for row in db.execute(
        "SELECT api,task FROM factor_history_jobs WHERE next_at>?", (now.timestamp(),))}
    covered = set()
    for kind, day, source, complete, count in db.execute(
            "SELECT kind,day,source,complete,rows FROM coverage WHERE day>=?", (days[-1],)):
        api = "daily" if source == "equity_cache" else source
        if api not in CROSS or not complete or kind != CROSS[api]:
            continue
        # A dated, explicitly complete local market file can include halted/new stocks.
        # Board membership can expand after a prior complete receipt. Count the
        # currently registered boards with trustworthy prices below instead.
        if api == "daily" and count >= max(2, math.ceil(len(family["daily"])*0.90)):
            covered.add((api, day))

    present = defaultdict(set)
    watched = {(kind, code):stamp for kind, code, stamp in db.execute(
        "SELECT kind,code,stamp FROM watched WHERE stamp>=? ORDER BY stamp DESC LIMIT 128",
        ((now-timedelta(days=RECENT_DAYS)).timestamp(),))}
    sector_counts = defaultdict(int)
    for kind, code, day, source in db.execute("""SELECT kind,code,day,source FROM bars
            WHERE kind IN ('sector','index') AND close>0 AND day BETWEEN ? AND ?""", (days[-1],days[0])):
        if not trusted_daily_source(source):
            continue
        api = data_api(kind, code)
        if api == "index_daily" or (kind, code) in watched:
            present[kind, code].add(day)
        if api in CROSS and code in family[api]:
            sector_counts[api,day] += 1
    wanted = defaultdict(float)
    jobs = defaultdict(list)
    for (kind, code), stamp in watched.items():
        api = data_api(kind, code)
        if api not in CROSS or code not in family[api]:
            continue
        if kind == "stock":
            present[kind,code].update(day for day, source in db.execute(
                "SELECT day,source FROM bars WHERE kind=? AND code=? AND close>0 AND day BETWEEN ? AND ?",
                (kind,code,days[-1],days[0])) if trusted_daily_source(source))
        gaps = set(days) - present[kind,code]
        for day in gaps:
            wanted[api,day] = max(wanted[api,day],stamp)
        key = kind+"/"+code
        if api in SECTOR_DAILY and api not in blocked and (api,key) not in tried and len(gaps) >= RANGE_GAPS:
            jobs["watched"].append(Job(api,key,kind,{code},gaps,(-stamp,code)))

    for depth, day in enumerate(days):
        for order, (api, kind) in enumerate(CROSS.items()):
            codes = family[api]
            if (not codes or api in blocked or (api,day) in tried or (api,day) in covered
                    or sector_counts[api,day] >= len(codes)):
                continue
            priority = 0 if depth < 2 else 1 if wanted[api,day] else 2
            jobs["market"].append(Job(api,day,kind,codes,{day}, (priority,depth,-wanted[api,day],order,day)))
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
            job = Job("index_daily",key,kind,{code},{days[i] for i in gaps},
                      (priority,gaps[0],-stamp,-1 if code==BENCHMARK else 4,code))
            if kind == "index" and code == BENCHMARK:
                return job
            jobs["index"].append(job)
    for offset in range(len(LANES)):
        lane = LANES[(turn + offset) % len(LANES)]
        if jobs[lane]:
            return min(jobs[lane],key=lambda job:job.priority)
    return None


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
    if job.cross_section:
        # Only verified full coverage can suppress a future retry. In particular, a
        # partial stock slice from another observer is never a market-wide receipt.
        complete = job.allowed.issubset(by_code)
        coverage_count = count
        if job.api == "daily":
            # Common provider cutoffs cannot certify a whole-market batch.
            complete = complete and len(job.allowed) >= 1000 and len(rows) not in {2000,3000,5000,6000}
        elif count and not complete:
            # Disjoint slices of the same dated official release can arrive on
            # later attempts. Individual exact-code records count for that day.
            stored = {row[0] for row in db.execute(
                "SELECT code FROM bars WHERE kind=? AND day=? AND source=? AND close>0",
                (job.kind,job.key,job.api))}
            coverage_count = len(job.allowed & stored)
            complete = coverage_count == len(job.allowed)
        db.execute("""INSERT INTO coverage(kind,day,source,complete,rows) VALUES(?,?,?,?,?)
            ON CONFLICT(kind,day,source) DO UPDATE SET complete=max(complete,excluded.complete),
            rows=max(rows,excluded.rows)""", (job.kind,job.key,job.api,int(complete),coverage_count))
    else:
        complete = count == len(job.days)
    return count, "ready" if complete else "partial" if count else "empty"


def hold(db, name, until):
    db.execute("""INSERT INTO factor_history_cooldowns(scope,until) VALUES(?,?)
        ON CONFLICT(scope) DO UPDATE SET until=max(until,excluded.until)""", (name,until))


def retry_at(now, job, status, current_missing):
    if status == "ready":
        return now.timestamp()+PACE
    if current_missing:
        # Allow the two published THS cuts and the evening SW cut time to pass.
        cuts = {"ths_daily": (time(19,5),time(21,5),time(23)),
                "sw_daily": (time(18,35),time(21,5),time(23))}
        for cut in cuts.get(job.api,(time(17,5),time(20,5),time(23))):
            if now.time() < cut:
                return datetime.combine(now.date(),cut,now.tzinfo).timestamp()
    if not job.cross_section and now.time() < time(15,45) and is_trading_day(now.date()) is True:
        # The same exact-code key gains today's session even if old, genuinely
        # absent days remain. Release it when the new date enters the window.
        return datetime.combine(now.date(),time(15,45),now.tzinfo).timestamp()
    return datetime.combine(now.date()+timedelta(days=1),time.min,now.tzinfo).timestamp()


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
        # or a restart shares the same reservation, fair rotation and cooldowns.
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS factor_history_jobs(api TEXT,task TEXT,
                attempted_day TEXT,next_at REAL,status TEXT,rows INTEGER,PRIMARY KEY(api,task)) WITHOUT ROWID""")
            db.execute("""CREATE TABLE IF NOT EXISTS factor_history_cooldowns(scope TEXT PRIMARY KEY,
                until REAL) WITHOUT ROWID""")
            db.execute("""CREATE TABLE IF NOT EXISTS factor_history_state(name TEXT PRIMARY KEY,
                value INTEGER NOT NULL) WITHOUT ROWID""")
            guarded = dict(db.execute("SELECT scope,until FROM factor_history_cooldowns WHERE until>?",(stamp,)))
            if "*" in guarded:
                db.commit()
                return max(1,math.ceil(guarded["*"]-stamp))
            cursor = db.execute("SELECT value FROM factor_history_state WHERE name='turn'").fetchone()
            turn = cursor[0] if cursor else 0
            job = choose(db,days,now,guarded,turn)
            if not job:
                db.commit()
                return min(300,max(1,math.ceil(min(guarded.values())-stamp))) if guarded else 300
            db.execute("""INSERT INTO factor_history_jobs VALUES(?,?,?,?,'claimed',0)
                ON CONFLICT(api,task) DO UPDATE SET attempted_day=excluded.attempted_day,
                next_at=excluded.next_at,status='claimed'""",(job.api,job.key,today,stamp+PACE))
            hold(db,"*",stamp+PACE)
            db.execute("""INSERT INTO factor_history_state VALUES('turn',?)
                ON CONFLICT(name) DO UPDATE SET value=excluded.value""", ((turn+1)%len(LANES),))
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
            # after the cooldown; real upstream failures receive a longer delay.
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
            current_missing = today in job.days
            if current_missing and not job.cross_section and status != "ready":
                saved = db.execute("SELECT source FROM bars WHERE kind=? AND code=? AND day=? AND close>0",
                                   (job.kind,next(iter(job.allowed)),today)).fetchone()
                current_missing = not (saved and trusted_daily_source(saved[0]))
            db.execute("UPDATE factor_history_jobs SET next_at=?,status=?,rows=? WHERE api=? AND task=?",
                       (retry_at(now,job,status,current_missing),status,count,job.api,job.key))
        return PACE
