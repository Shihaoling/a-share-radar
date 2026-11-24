"""Recover exact stock minute closes and project equal-weight sector gaps."""

from argparse import ArgumentParser
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta
import fcntl
from itertools import groupby
import json
import math
from operator import itemgetter
import os
from pathlib import Path
import re
import sqlite3
import time

from pipeline.calendar import is_trading_day, shanghai_now
from pipeline.data_service import primary_token
from pipeline.stock_api import FIELDS, _Client, _Failure
from constituent_live import A_SHARE
from market_gap_recovery import session
from relay_common import STATE, relay_guard
from relay_quotes import publish
from collector_db import connect as collector_connect, retry_locked
from sector_directory import mapping

PCB = "885959.TI"
SECTOR = re.compile(r"[0-9]{6}(?:R?\.TI|\.(?:SI|SH|SZ))")
BATCH_SIZE = 24
MINUTES = tuple(f"{hour:02}:{minute:02}" for hour, minute in
                ((m//60,m%60) for m in (*range(571,691), *range(781,901))))
VALID_MINUTES = frozenset((*MINUTES, "09:30","13:00"))
SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_history_minutes(day TEXT, code TEXT, minute TEXT, close REAL,
    PRIMARY KEY(day,code,minute)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS stock_history_jobs(day TEXT, code TEXT, attempted REAL, retry_at REAL,
    coverage INTEGER, outcome TEXT, PRIMARY KEY(day,code)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS board_history_recovered(day TEXT,code TEXT,minute TEXT,
    PRIMARY KEY(day,code,minute)) WITHOUT ROWID;
"""
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:00(?:\+08:00)?$")


def read_rosters(output):
    rosters = {}
    for path in (output/"sector-members").glob("*.json"):
        if not SECTOR.fullmatch(path.stem):
            continue
        try:
            row = mapping(path)
        except (OSError,ValueError):
            continue
        if row.get("status") == "ready" and row.get("code") == path.stem:
            codes = {r["code"] for r in row.get("members",[])
                     if isinstance(r,dict) and isinstance(r.get("code"),str)}
            if any(A_SHARE.fullmatch(code) for code in codes):
                rosters[path.stem] = codes
    return rosters


def checked_minutes(rows, requested, day):
    bars, conflicts = {}, set()
    for row in rows:
        code, stamp, close = row.get("ts_code"),row.get("trade_time"),row.get("close")
        if (code not in requested or not isinstance(stamp,str) or not STAMP.fullmatch(stamp)
                or stamp[:10]!=day or stamp[11:16] not in VALID_MINUTES
                or type(close) not in (int,float) or not math.isfinite(close) or close<=0):
            continue
        key = code,stamp[11:16]
        if key in bars and bars[key] != close:
            conflicts.add(key)
        bars[key] = close
    return {key:close for key,close in bars.items() if key not in conflicts}


def store_minutes(db, day, requested, rows, now):
    bars = checked_minutes(rows,requested,day)

    def write():
        with db:
            db.executemany("INSERT OR IGNORE INTO stock_history_minutes VALUES(?,?,?,?)",
                           ((day,code,minute,close) for (code,minute),close in bars.items()))
            # A dated minute-end bar fills the missing position. Existing observed quotations retain their timestamps.
            db.executemany("INSERT OR IGNORE INTO chart_points VALUES('stock',?,?,?,?,?)", (
                (code,day,day+"T"+minute,day+"T"+minute+":00+08:00",close) for (code,minute),close in bars.items()))
            for code in requested:
                minutes={r[0] for r in db.execute(
                    "SELECT minute FROM stock_history_minutes WHERE day=? AND code=?",(day,code))}
                count=sum(minute in minutes for minute in MINUTES)
                db.execute("INSERT OR REPLACE INTO stock_history_jobs VALUES(?,?,?,?,?,?)",
                           (day,code,now, now+2100,count,
                            "complete" if count==240 else "partial" if count else "empty"))
        return len(bars)

    return retry_locked(write, attempts=5, base=0.5)


def reproject(db, day, rosters):
    if not rosters:
        return {}
    bases = {code: prior for code,prior in db.execute(
        "SELECT code,pre_close FROM bars WHERE kind='stock' AND day=? AND pre_close>0",(day,))}
    for code,prior in db.execute("SELECT code,previous_close FROM board_stock_bases WHERE day=? AND previous_close>0",(day,)):
        bases.setdefault(code,prior)
    tracked = set().union(*rosters.values()) & bases.keys()
    existing = defaultdict(set)
    for code,minute in db.execute("SELECT code,minute FROM constituent_minutes WHERE day=?",(day,)):
        if code in rosters:
            existing[code].add(minute[11:16])
    inserted = Counter()
    # Stream one cross-section at a time so a market-wide repair stays inside the worker memory limit.
    minute_rows = db.execute("SELECT code,minute,close FROM stock_history_minutes WHERE day=? ORDER BY minute,code",(day,))
    for minute, group in groupby(minute_rows,key=itemgetter(1)):
        changes = {code:(close/bases[code]-1)*100 for code,_,close in group if code in tracked}
        hits = set(changes)
        stamps = day+"T"+minute+":00+08:00"
        clock_key = day+"T"+minute
        rows = []
        for board, codes in rosters.items():
            if minute in existing[board]:
                continue
            matched = codes & hits
            if len(matched)*5 >= len(codes)*4:
                value = math.fsum(changes[code] for code in matched)/len(matched)
                rows.append((board,day,clock_key,stamps,stamps,value,len(matched),len(codes),1))
        if rows:
            with db:
                for row in rows:
                    cursor = db.execute("INSERT OR IGNORE INTO constituent_minutes VALUES(?,?,?,?,?,?,?,?,?)",row)
                    if cursor.rowcount:
                        db.execute("INSERT OR IGNORE INTO board_history_recovered VALUES(?,?,?)",(day,row[0],clock_key))
                        inserted[row[0]] += 1
    return dict(inserted)


def recovery_report(db, day, rosters, output, outcomes):
    def count(code):
        state = db.execute("""SELECT count(*),max(stamp) FROM constituent_minutes WHERE day=? AND code=?""",
                           (day,code)).fetchone()
        recovered = db.execute("SELECT count(*) FROM board_history_recovered WHERE day=? AND code=?",(day,code)).fetchone()[0]
        return {"points":state[0],"latest":state[1],"recovered":recovered}
    stats = db.execute("""SELECT count(*),sum(CASE WHEN outcome='complete' THEN 1 ELSE 0 END)
                       FROM stock_history_jobs WHERE day=?""",(day,)).fetchone()
    sector_total = db.execute("SELECT count(DISTINCT code),count(*) FROM board_history_recovered WHERE day=?",(day,)).fetchone()
    packet = {"trade_date":day,"checked_at":shanghai_now().isoformat(),"pcb":count(PCB),
              "complete_rosters":len(rosters),"stock_codes_attempted":stats[0],"complete_stock_minutes":stats[1] or 0,
              "boards_recovered":sector_total[0],"board_minutes_added":sector_total[1],"last_run":dict(outcomes)}
    publish(output,packet)
    print(json.dumps(packet,ensure_ascii=False),flush=True)


def recover(state, day, request, *, budget=1250, morning_only=False):
    output = state/"quote-output"
    private = state/"asset-research"
    with closing(collector_connect(private/"research.sqlite3")) as db:
        db.execute("PRAGMA temp_store=FILE")
        retry_locked(lambda: db.executescript(SCHEMA), attempts=3, base=0.5)

        def prune():
            with db:
                cutoff = (datetime.fromisoformat(day).date()-timedelta(days=7)).isoformat()
                db.execute("DELETE FROM stock_history_minutes WHERE day<?",(cutoff,))
                db.execute("DELETE FROM stock_history_jobs WHERE day<?",(cutoff,))
                db.execute("DELETE FROM board_history_recovered WHERE day<?",(cutoff,))
        retry_locked(prune, attempts=3, base=0.5)
        rosters = read_rosters(output)
        targets = set().union(*rosters.values()) if rosters else set()
        daily = {r[0] for r in db.execute("SELECT code FROM bars WHERE kind='stock' AND day=? AND pre_close>0",(day,))}
        if morning_only:
            daily.update(r[0] for r in db.execute(
                "SELECT code FROM board_stock_bases WHERE day=? AND previous_close>0",(day,)))
            completed = {r[0] for r in db.execute("""SELECT code FROM chart_points WHERE kind='stock'
                AND day=? AND minute BETWEEN ? AND ? GROUP BY code HAVING count(*)>=120""",
                (day,day+"T09:30",day+"T11:30"))}
            daily.difference_update(completed)
        dates = mapping(private/"evening-stock-suspensions.json")
        halted = set(dates.get("codes",[])) if dates.get("trade_date")==day else set()
        known = {code:(attempted,retry_at,coverage) for code,attempted,retry_at,coverage in db.execute(
            "SELECT code,attempted,retry_at,coverage FROM stock_history_jobs WHERE day=?",(day,))}
        deadline = time.monotonic()+budget
        pcb = rosters.get(PCB,set())
        outcomes=Counter()
        for selection in (pcb, targets-pcb):
            now=time.time()
            todo=sorted((c for c in (selection & daily)-halted
                   if A_SHARE.fullmatch(c) and known.get(c,(0,0,0))[1]<=now and known.get(c,(0,0,0))[2]<240),
                   key=lambda c:(known.get(c,(0,0,0))[0],c))
            blocks = [todo[i:i+BATCH_SIZE] for i in range(0,len(todo),BATCH_SIZE)]
            for j,codes in enumerate(blocks):
                if time.monotonic()+25>=deadline:
                    break
                result = fetch_batch(codes,day,request,outcomes,deadline,end="11:30:00" if morning_only else "15:00:59")
                if result is None:
                    break
                if result is False:
                    outcomes["deferred_batches"] += 1
                    if outcomes["deferred_batches"] >= 5:
                        break
                    continue
                if len(result) < len(codes)*(90 if morning_only else 200) and len(codes)>1:
                    represented = {row.get("ts_code") for row in result}
                    unparsed = [code for code in codes if code not in represented]
                    if unparsed and represented:
                        size = max(1,min(len(represented),BATCH_SIZE//2))
                        blocks.extend(unparsed[i:i+size] for i in range(0,len(unparsed),size))
                        codes = [code for code in codes if code in represented]
                before = time.time()
                count = store_minutes(db,day,set(codes),result,before)
                outcomes["minute_rows_observed"] += count
                outcomes["batches"] += 1
                if j and j%30==0:
                    print(json.dumps({"date":day,"batches":outcomes["batches"],
                                      "minute_rows_observed":outcomes["minute_rows_observed"]}),flush=True)
                if selection is not pcb and outcomes["batches"]%60==0:
                    reproject(db,day,rosters)
                    recovery_report(db,day,rosters,private/"stock-minute-recovery-report.json",outcomes)
            if selection is pcb:
                reproject(db,day,{PCB:pcb} if pcb else {})
                recovery_report(db,day,rosters,private/"stock-minute-recovery-report.json",outcomes)
        reproject(db,day,rosters)
        recovery_report(db,day,rosters,private/"stock-minute-recovery-report.json",outcomes)


def fetch_batch(codes, day, request, outcomes, deadline, *, end="15:00:59"):
    params={"ts_code":",".join(codes),"freq":"1min","start_date":day+" 09:30:00","end_date":day+" "+end}
    temporary = {"transport_error","http_error"}
    for attempt in range(3):
        try:
            return request(params)
        except _Failure as error:
            outcomes[error.code]+=1
            recoverable = error.code in temporary
            retry = max(2,error.retry_seconds)+1
            if error.code not in temporary | {"rate_limited","shared_spacing","relay_busy","local_rate_limit","budget_exhausted"} or retry>75 or time.monotonic()+retry+25>=deadline:
                return None
            if attempt<2:
                time.sleep(retry)
    return False if recoverable else None


def main():
    parser=ArgumentParser()
    parser.add_argument("mode",choices=("latest","session","midday"))
    mode = parser.parse_args().mode
    now = shanghai_now()
    morning_only = mode == "midday"
    date = (now.date() if is_trading_day(now.date()) is True and 11*60+30 <= now.hour*60+now.minute < 12*60+35
            else None) if morning_only else session(now,mode)
    if date is None:
        return
    private=STATE/"asset-research"
    with (private/"stock-minute-recovery.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return
        if not primary_token():
            raise SystemExit("Primary data credential unavailable")
        token=(Path(os.environ["CREDENTIALS_DIRECTORY"])/"sxdata-token").read_text().strip()
        FIELDS["stk_mins"]="ts_code,trade_time,close"
        def request(params):
            with relay_guard():
                client=_Client(token,{"max_requests":1,"timeout_seconds":14,"budget_seconds":24},shanghai_now())
                return client.request_primary("stk_mins",params,bulk=True)
        recover(STATE,date.isoformat(),request,morning_only=morning_only)


if __name__=="__main__":
    main()
