"""Private numeric counters for real provider calls; no payloads or credentials."""
from contextlib import closing
from datetime import datetime, time
import os
from pathlib import Path
import sqlite3
import sys
import time as clock

from .calendar import SHANGHAI, is_trading_day

SCHEMA = """
CREATE TABLE IF NOT EXISTS meter (
 grain TEXT,stamp INTEGER,period TEXT,provider TEXT,scope TEXT,
 requests INTEGER NOT NULL,success INTEGER NOT NULL,failed INTEGER NOT NULL,
 responses INTEGER NOT NULL,input INTEGER NOT NULL,output INTEGER NOT NULL,total INTEGER NOT NULL,
 has_input INTEGER NOT NULL,has_output INTEGER NOT NULL,has_total INTEGER NOT NULL,
 PRIMARY KEY(grain,stamp,provider,scope,period)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS coverage (provider TEXT,minute INTEGER,period TEXT,
 PRIMARY KEY(provider,minute)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS coverage_hours (provider TEXT,hour INTEGER,period TEXT,minutes INTEGER,
 PRIMARY KEY(provider,hour,period)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY,value REAL NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS receipts (seq INTEGER PRIMARY KEY,finished INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS primary_receipts (seq INTEGER PRIMARY KEY,finished INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS primary_outcomes (scope TEXT,outcome TEXT,requests INTEGER NOT NULL,
 PRIMARY KEY(scope,outcome)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS last_success (provider TEXT,scope TEXT,at REAL,
 PRIMARY KEY(provider,scope)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS model_events (
 id INTEGER PRIMARY KEY, started_at REAL NOT NULL, scope TEXT NOT NULL, accepted INTEGER,
 input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER);
CREATE INDEX IF NOT EXISTS model_events_started ON model_events(started_at);
"""
COLUMNS = ("requests","success","failed","responses","input","output","total","has_input","has_output","has_total")
UPDATE = ("INSERT INTO meter VALUES("+",".join("?" for _ in range(5+len(COLUMNS)))+
          ") ON CONFLICT DO UPDATE SET "+",".join(f"{c}={c}+excluded.{c}" for c in COLUMNS))


def db_path():
    if override := os.getenv("RADAR_REQUEST_USAGE_DB"):
        return Path(override)
    if gate := os.getenv("RADAR_SERVICE_GATE_FILE"):
        parent = Path(gate).parent
        return (parent.parent if parent.name=="relay" else parent) / "request-usage" / "metrics.sqlite3"
    return None


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    db = sqlite3.connect(path,timeout=1)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
    except BaseException:
        db.close()
        raise
    return db


def period_at(stamp):
    local = datetime.fromtimestamp(stamp,SHANGHAI)
    opened = is_trading_day(local.date())
    if opened is None:
        return "unknown"
    trading = opened and (time(9,30) <= local.time() < time(11,30) or time(13) <= local.time() < time(15))
    return "trading" if trading else "other"


def counts(db, stamp, provider, scope, *, start=False, success=None, usage=None, finished=None):
    usage = usage if isinstance(usage,dict) else {}
    values = [usage.get(k) for k in ("prompt_tokens","completion_tokens","total_tokens")]
    valid = [type(v) is int and 0<=v<=10**12 for v in values]
    numbers = [v if ok else 0 for v,ok in zip(values,valid)]
    delta = (int(start),int(success is True),int(success is False),int(any(valid)),*numbers,*map(int,valid))
    period = period_at(stamp)
    for grain, bucket in (("m",int(stamp)//60*60),("h",int(stamp)//3600*3600),("all",0)):
        db.execute(UPDATE,(grain,bucket,period,provider,scope,*delta))
    if start:
        db.execute("""INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET
                      value=min(value,excluded.value)""",(provider+"_first_event",stamp))
    if success is True:
        db.execute("""INSERT INTO last_success VALUES(?,?,?) ON CONFLICT(provider,scope)
                      DO UPDATE SET at=max(at,excluded.at)""",(provider,scope,stamp if finished is None else finished))


def model_scope(digest=False):
    return "training_labels" if Path(sys.argv[0]).stem=="training_news" else "news_digest" if digest else "news_analysis"


def model_start(scope):
    path = db_path()
    if path is None:
        return None
    stamp = clock.time()
    try:
        with closing(connect(path)) as db, db:
            event = db.execute("INSERT INTO model_events(started_at,scope) VALUES(?,?)",(stamp,scope)).lastrowid
            counts(db,stamp,"minimax",scope,start=True)
        return path,stamp,scope,event
    except (OSError,sqlite3.Error):
        return path,stamp,scope,None


def model_finish(event, success, usage):
    if event is None:
        return
    path,stamp,scope,ident = event
    usage = usage if isinstance(usage,dict) else {}
    values = [usage.get(key) for key in ("prompt_tokens","completion_tokens","total_tokens")]
    tokens = [v if type(v) is int and 0<=v<=10**12 else None for v in values]
    try:
        with closing(connect(path)) as db, db:
            if ident is None:
                db.execute("INSERT INTO model_events VALUES(NULL,?,?,?,?,?,?)",(stamp,scope,int(success),*tokens))
            else:
                changed = db.execute("""UPDATE model_events SET accepted=?,input_tokens=?,output_tokens=?,total_tokens=?
                                      WHERE id=? AND accepted IS NULL""",(int(success),*tokens,ident))
                if not changed.rowcount:
                    return
            counts(db,stamp,"minimax",scope,start=ident is None,success=success,usage=usage,finished=clock.time())
    except (OSError,sqlite3.Error):
        # Meter availability must never turn a provider response into a retried call.
        pass
