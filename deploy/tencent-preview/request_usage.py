"""Aggregate the shared gate locally and expose privacy-filtered administrator totals."""
from contextlib import closing
from datetime import date, datetime
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

from pipeline.calendar import SHANGHAI, market_session, shanghai_now
from pipeline.data_service import METERED_APIS, request_usage as gate_usage, gate_state
from pipeline.minimax_prompts import LEGACY_ASSISTANT_STAGE, STAGES as CONFIGURABLE_MODEL_STAGES
from pipeline.public_fields import remove_usage
from pipeline.request_policy import read_policy, RELAY_EXPIRES_AT
from pipeline.usage_meter import COLUMNS, connect, counts, db_path, period_at
from relay_common import STATE

MINUTE_RETENTION = 8*86400
HOUR_RETENTION = 366*86400
MODEL_TRANSITION = 550  # allow already-running pre-deployment model jobs to finish
STAGES = ("trading","other")
MODEL_STAGES = {**CONFIGURABLE_MODEL_STAGES, LEGACY_ASSISTANT_STAGE: ("研究助手 · 历史合计", "")}
KEYS = ("requests","success","failed","usage_responses","input_tokens","output_tokens","total_tokens")


def path_for(state):
    return db_path() or Path(state)/"request-usage"/"metrics.sqlite3"


def iso(stamp):
    return datetime.fromtimestamp(stamp,SHANGHAI).isoformat(timespec="seconds") if stamp is not None else None


def state_value(db,key):
    row = db.execute("SELECT value FROM state WHERE key=?",(key,)).fetchone()
    return row[0] if row else None


def set_value(db,key,value):
    db.execute("INSERT OR REPLACE INTO state VALUES(?,?)",(key,value))


def intervals(db,provider,start,end,previous):
    # Only complete wall-clock minutes between two observations are comparable.
    left = max(math.ceil(start/60)*60, int(previous or 0)//60*60, int(end-HOUR_RETENTION)//60*60)
    for minute in range(left,int(end)//60*60,60):
        period = period_at(minute)
        inserted = db.execute("INSERT OR IGNORE INTO coverage VALUES(?,?,?)",(provider,minute,period))
        if inserted.rowcount:
            db.execute("""INSERT INTO coverage_hours VALUES(?,?,?,1)
                        ON CONFLICT DO UPDATE SET minutes=minutes+1""",(provider,minute//3600*3600,period))


def sample_relay(db, events, generation, now, *, primary=False):
    provider = "relay_primary" if primary else "relay"
    receipts = "primary_receipts" if primary else "receipts"
    first = events[0] if events else None
    visible = [row for row in events if row.get("route") == "primary"] if primary else events
    last = state_value(db,provider+"_seq") or 0
    previous = state_value(db,provider+"_through")
    segment = state_value(db,provider+"_segment")
    reset = state_value(db,provider+"_generation") != generation
    if reset or (first and first["id"]>last+1):
        db.execute("DELETE FROM "+receipts)
        segment = generation if not first or first["id"]==1 else first["started_at"]
        if primary and previous is None:
            segment = visible[0]["started_at"] if visible else None
        set_value(db,provider+"_generation",generation)
    if segment is None and visible:
        segment = visible[0]["started_at"]
    if segment is not None:
        set_value(db,provider+"_segment",segment)
    for row in visible:
        seq, stamp = row["id"],row["started_at"]
        scope = row["api"] if row["api"] in METERED_APIS else "other"
        outcome = row.get("outcome")
        done = outcome not in ("in_flight", "legacy_unobserved")
        old = db.execute("SELECT finished FROM "+receipts+" WHERE seq=?",(seq,)).fetchone()
        if old and (old[0] or not done):
            continue
        counts(db,stamp,provider,scope,start=old is None,success=outcome=="ok" if done else None,
               finished=row.get("finished_at"))
        db.execute("INSERT OR REPLACE INTO "+receipts+" VALUES(?,?)",(seq,int(done)))
        if primary and done:
            db.execute("""INSERT INTO primary_outcomes VALUES(?,?,1)
                        ON CONFLICT DO UPDATE SET requests=requests+1""",(scope,outcome))
    if first:
        db.execute("DELETE FROM "+receipts+" WHERE seq<?",(first["id"],))
        set_value(db,provider+"_seq",events[-1]["id"])
    if segment is not None and now>=segment:
        intervals(db,provider,segment,now,previous)
        if state_value(db,provider+"_since") is None:
            set_value(db,provider+"_since",segment)
        set_value(db,provider+"_through",now)


def sample(path, gate, now):
    with closing(connect(path)) as db, db:
        if state_value(db,"model_window_since") is None:
            first, = db.execute("SELECT min(started_at) FROM model_events").fetchone()
            set_value(db,"model_window_since",now if first is None else min(now,first))
        model_since = state_value(db,"model_since")
        if model_since is None:
            model_since = now+MODEL_TRANSITION
            set_value(db,"model_since",model_since)
        intervals(db,"minimax",model_since,now,state_value(db,"model_through"))
        set_value(db,"model_through",now)

        if gate and isinstance(gate.get("since"),(float,int)):
            generation = gate["since"]
            events = sorted(gate.get("recent",()),key=lambda row:row["id"])
            sample_relay(db,events,generation,now)
            sample_relay(db,events,generation,now,primary=True)

        if (state_value(db,"pruned") or 0)<now-3600:
            db.execute("DELETE FROM meter WHERE grain='m' AND stamp<?",(now-MINUTE_RETENTION,))
            db.execute("DELETE FROM meter WHERE grain='h' AND stamp<?",(now-HOUR_RETENTION,))
            db.execute("DELETE FROM coverage WHERE minute<?",(now-MINUTE_RETENTION,))
            db.execute("DELETE FROM coverage_hours WHERE hour<?",(now-HOUR_RETENTION,))
            db.execute("DELETE FROM model_events WHERE started_at<?",(now-2*86400,))
            set_value(db,"pruned",now)


def readonly(path,immutable=False):
    uri = Path(path).resolve().as_uri()+"?mode=ro"+("&immutable=1" if immutable else "")
    return sqlite3.connect(uri,uri=True,timeout=1)


def file_version(path):
    stat = path.stat()
    return stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns


def stable_main_file(path):
    if any(Path(str(path)+suffix).exists() for suffix in ("-wal","-shm","-journal")):
        return None
    return file_version(path)


def aggregate_row(path,query):
    for _ in range(2):
        try:
            with closing(readonly(path)) as db:
                return db.execute(query).fetchone()
        except sqlite3.OperationalError as error:
            if getattr(error,"sqlite_errorcode",0)&255 not in (sqlite3.SQLITE_READONLY,sqlite3.SQLITE_CANTOPEN):
                raise
            # A read-only mount cannot create SQLite's WAL-side files for a
            # checkpointed, idle database. Only consult the stable main file.
            version = stable_main_file(path)
            if version is not None:
                with closing(readonly(path,immutable=True)) as db:
                    row = db.execute(query).fetchone()
                if stable_main_file(path)==version:
                    return row
    with closing(readonly(path)) as db:
        return db.execute(query).fetchone()


def historical(state):
    sources = (
        ("news_analysis",state/"news/news.sqlite3","model_usage","1","1"),
        ("training_labels",state/"training-news/dataset.sqlite3","usage",
         "outcome NOT IN ('shared_model_cooldown','input_over_budget')","outcome IN ('ok','partial')"))
    result = {"note":"历史新闻表只记录成功回执。训练表按调用批次统计。历史小计与分时统计时间可重叠，应分别阅读。"}
    for scope,path,table,where,accepted in sources:
        result[scope] = {k:None for k in (*KEYS,"since","through","last_success_at")}
        if not path.is_file():
            continue
        try:
            row = aggregate_row(path,f"""SELECT count(*),coalesce(sum({accepted}),0),sum(input_tokens),
                       sum(output_tokens),sum(total_tokens),count(CASE WHEN input_tokens IS NOT NULL
                       OR output_tokens IS NOT NULL OR total_tokens IS NOT NULL THEN 1 END),min(at),max(at),
                       max(CASE WHEN {accepted} THEN at END) FROM {table} WHERE {where}""")
            seen,ok,inp,out,total,measured,since,through,last_ok = row
            result[scope] = dict(requests=seen,success=ok,failed=seen-ok if scope=="training_labels" else None,
                                 input_tokens=inp,output_tokens=out,total_tokens=total,usage_responses=measured,
                                 since=since,through=through,last_success_at=last_ok)
        except (sqlite3.Error,OSError):
            pass
    return result


def live_gate():
    try:
        return gate_usage()
    except (OSError,ValueError,TypeError):
        return None


def metrics(values, minutes=None):
    n,ok,failed,measured,inp,out,total,has_in,has_out,has_total = values
    result = dict(requests=n,success=ok,failed=failed,usage_responses=measured,
                  input_tokens=inp if has_in else None,output_tokens=out if has_out else None,
                  total_tokens=total if has_total else None)
    if minutes is not None:
        if not minutes:
            result = dict.fromkeys(KEYS)
        result.update(minutes=minutes,avg_requests_per_minute=round(n/minutes,3) if minutes else None)
    return result


def statistics(db,provider,now,scope_ids):
    end = int(now)//60*60
    since = end-86400
    lengths = dict(db.execute("""SELECT period,count(*) FROM coverage WHERE provider=? AND
                  minute>=? AND minute<? GROUP BY period""",(provider,since,end)))
    cols = ",".join("sum(b."+c+")" for c in COLUMNS)
    minute_rows = db.execute("SELECT b.period,b.scope,"+cols+""" FROM meter b
         JOIN coverage c ON c.provider=b.provider AND c.minute=b.stamp AND c.period=b.period
         WHERE b.grain='m' AND b.provider=? AND b.stamp>=? AND b.stamp<?
         GROUP BY b.period,b.scope""",(provider,since,end))
    active = {(period,scope):vals for period,scope,*vals in minute_rows}
    lifetime = {scope:vals for scope,*vals in db.execute("SELECT b.scope,"+cols+""" FROM meter b
                WHERE b.grain='all' AND b.provider=? GROUP BY b.scope""",(provider,))}
    scope_ids = set(scope_ids)|set(lifetime)
    stages = (*STAGES,"unknown")
    zero = (0,)*len(COLUMNS)
    def combined(items):
        return tuple(map(sum,zip(zero,*items)))
    periods = {p: metrics(combined(v for (stage,_),v in active.items() if stage==p),lengths.get(p,0))
               for p in stages}
    scopes = {s: {**metrics(lifetime.get(s,zero)),
                  "periods":{p:metrics(active.get((p,s),zero),lengths.get(p,0)) for p in stages}}
              for s in sorted(scope_ids)}
    observed = {**metrics(combined(lifetime.values())),"since":iso(state_value(db,provider+"_first_event"))}
    return periods,scopes,observed


def rolling_model(db,now):
    since = state_value(db,"model_window_since")
    if since is None:
        return {}
    result = {}
    for seconds in (60,3600,86400):
        start = max(now-seconds,since)
        observed = min(seconds,max(0,int(now-since)))
        zero = (0,)+(None,)*3+(0,)
        def usage(row):
            requests, inp, out, total, receipts = row
            return dict(requests=requests,input_tokens=0 if not requests else inp,output_tokens=0 if not requests else out,
                        total_tokens=0 if not requests else total,token_receipts=receipts)

        rows = dict((scope,values) for scope,*values in db.execute("""SELECT scope,count(*),sum(input_tokens),
            sum(output_tokens),sum(total_tokens),count(total_tokens) FROM model_events
            WHERE started_at>=? AND started_at<=? GROUP BY scope""",(start,now)))
        values = list(rows.values())
        total = tuple(sum(v for v in column if v is not None) if any(v is not None for v in column) else None
                      for column in zip(*values)) if values else zero
        result[str(seconds)] = {**usage(total),"observed_seconds":observed,
                                "scopes":{scope:usage(rows.get(scope,zero)) for scope in MODEL_STAGES}}
    return result


def sources_for(state,gate,last,history,now):
    sources = []
    apis = (gate or {}).get("apis",{})
    if "relay_primary" in last:
        cooling = (gate or {}).get("primary_host_until",0)>now if gate else None
        sources.append(dict(id="sxdata",route="primary",name="行情与资讯主接口",label="行情与资讯主接口",
                            connected=True,status="cooling_down" if cooling else "connected",
                            cooling_down=cooling,last_success_at=iso(last["relay_primary"]),expires_at=None))
    elif any(api.get("outcomes",{}).get("ok",0)>0 for api in apis.values()) or "relay" in last:
        cooling = gate_state("__request_usage_host__")[0]>0 if gate else None
        sources.append(dict(id="sxdata",name="行情与资讯接口",label="行情与资讯接口",
                            connected=True,status="cooling_down" if cooling else "connected",
                            cooling_down=cooling,last_success_at=iso(last.get("relay")),expires_at=RELAY_EXPIRES_AT))
    known = [entry["last_success_at"] for entry in (history["news_analysis"],history["training_labels"])
             if entry.get("last_success_at")]
    if "minimax" in last:
        known.append(iso(last["minimax"]))
    if known:
        cooling = None
        path = state/"news/minimax-cooldown.sqlite3"
        if path.is_file():
            try:
                row = aggregate_row(path,"SELECT until FROM cooldown WHERE id=1")
                cooling = bool(row and row[0]>now)
            except (OSError,sqlite3.Error):
                pass
        sources.append(dict(id="minimax",name="专属模型",label="专属模型",connected=True,
                            status="cooling_down" if cooling else "connected",cooling_down=cooling,
                            last_success_at=max(known),expires_at=None))
    return sources


def dashboard(now=None,state=STATE):
    now = time.time() if now is None else now
    state = Path(state)
    path = path_for(state)
    gate = live_gate()
    history = historical(state)
    coverage = dict(since=None,through=None,relay_since=None,minimax_since=None,window_hours=24,
                    note="最近24小时仅统计完整覆盖分钟，含零请求。盘中为交易日09:30—11:30与13:00—15:00。旧记录只保留可核实小计。")
    empty = {k:dict.fromkeys((*KEYS,"avg_requests_per_minute"),None)|{"minutes":0} for k in (*STAGES,"unknown")}
    relay = dict(periods=empty,scopes={})
    primary = dict(periods=empty,scopes={},since=None,through=None,observed=None)
    minimax = dict(periods=empty,scopes={},historical=history,observed=None,rolling={},
                   scope_labels={stage:entry[0] for stage,entry in MODEL_STAGES.items()})
    api_primary = {key:value.get("routes",{}).get("primary",0) for key,value in (gate or {}).get("apis",{}).items()
                   if value.get("routes",{}).get("primary",0)}
    primary_outcomes = {}
    last = {}
    if path.is_file():
        try:
            with closing(readonly(path)) as db:
                db.execute("BEGIN")
                relay["periods"],relay["scopes"],relay["observed"] = statistics(db,"relay",now,
                    (gate or {}).get("apis",{}))
                primary["periods"],primary["scopes"],primary["observed"] = statistics(db,"relay_primary",now,api_primary)
                primary.update(since=iso(state_value(db,"relay_primary_since")),
                               through=iso(state_value(db,"relay_primary_through")))
                for scope,outcome,requests in db.execute("SELECT scope,outcome,requests FROM primary_outcomes"):
                    primary_outcomes.setdefault(scope,{})[outcome] = requests
                minimax["periods"],minimax["scopes"],minimax["observed"] = statistics(db,"minimax",now,
                    MODEL_STAGES)
                minimax["rolling"] = rolling_model(db,now)
                for provider,prefix in (("relay","relay"),("minimax","model")):
                    since = state_value(db,prefix+"_since")
                    through = state_value(db,prefix+"_through")
                    coverage[provider+"_since"] = iso(since)
                    (relay if provider=="relay" else minimax).update(since=iso(since),through=iso(through))
                coverage["since"] = coverage["relay_since"] or coverage["minimax_since"]
                coverage["through"] = iso(state_value(db,"relay_through") or state_value(db,"model_through"))
                last = dict(db.execute("SELECT provider,max(at) FROM last_success GROUP BY provider"))
        except (OSError,sqlite3.Error):
            coverage["note"] += " 统计库暂不可读。"
    try:
        limit = read_policy()["limit_60s"]
    except (ValueError,OSError):
        limit = None
    apis = (gate or {}).get("apis",{})
    cum = None
    if gate and gate.get("since") is not None:
        all_ok = sum(api.get("outcomes",{}).get("ok",0) for api in apis.values())
        outcomes = sum(sum(api.get("outcomes",{}).values()) for api in apis.values())
        cum = dict(since=iso(gate["since"]),requests=sum(api.get("starts",0) for api in apis.values()),
                   success=all_ok,failed=outcomes-all_ok,peak_60s=gate["peak_60s"])
    relay.update(expires_at=RELAY_EXPIRES_AT,rolling_60s=gate["starts_last_60s"] if gate and gate.get("since") is not None else None,
                 limit_per_minute=limit,cumulative=cum,
                 apis={key:{**{name:value[name] for name in ("starts","legacy","outcomes","deferred","transport","routes")
                                   if name in value},
                            "periods":relay["scopes"].get(key,{}).get("periods",empty)}
                       for key,value in apis.items() if key in METERED_APIS or key=="other"})
    metered = gate is not None and gate.get("since") is not None
    primary.update(cumulative={"requests":sum(api_primary.values())} if metered else None,
                   rolling_60s=sum(row.get("route")=="primary" and now-60<row["started_at"]<=now
                                   for row in gate["recent"]) if metered else None,
                   limit_per_minute=limit,
                   apis={key:{"starts":api_primary.get(key) if metered else None,
                              "outcomes":primary_outcomes.get(key),
                              "periods":primary["scopes"].get(key,{}).get("periods",empty)}
                         for key in api_primary.keys()|primary_outcomes.keys()})
    return dict(schema_version=1,as_of=iso(now),coverage=coverage,relay=relay,primary=primary,minimax=minimax,
                sources=sources_for(state,gate,last,history,now))


def write_snapshot(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w",dir=path.parent,delete=False) as stream:
        try:
            json.dump(data,stream,ensure_ascii=False,allow_nan=False,separators=(",",":"))
            stream.flush()
            os.replace(stream.name,path)
        finally:
            Path(stream.name).unlink(missing_ok=True)


def publish_sources(state,data):
    public = dict(schema_version=1,as_of=data["as_of"],
                  sources=[{k:s[k] for k in ("id","name","last_success_at","cooling_down")} for s in data["sources"]])
    write_snapshot(Path(state)/"request-usage"/"connected-sources.json",public)


def progress_counts(row,fields):
    if not isinstance(row,dict):
        raise ValueError("invalid training progress row")
    return {k:row[k] for k in fields if type(row.get(k)) is int and row[k]>=0}


def progress_stamp(value):
    if not isinstance(value,str) or not value:
        return None
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("missing training progress timezone")
    return stamp.isoformat(timespec="seconds")


def public_training_progress(raw):
    if not isinstance(raw,dict) or raw.get("schema_version")!=1 or not isinstance(raw.get("days"),list):
        raise ValueError("invalid training progress")
    top = progress_counts(raw,("total_days","queried_days","audited_days","days_with_news","pending_late_checks",
                               "collected","labelled","missing_body","exhausted"))
    required = {"total_days","audited_days","collected","labelled"}
    if not required.issubset(top) or not top["total_days"] or top["total_days"]!=len(raw["days"]):
        raise ValueError("invalid training progress totals")
    begin,end = (date.fromisoformat(raw[k]).isoformat() for k in ("start_day","end_day_exclusive"))
    days = []
    for item in raw["days"]:
        row = progress_counts(item,("windows","complete_windows","incomplete_windows","limited_windows",
              "verification_retry_windows","verified_windows","collection_retry_windows","returned","audited_windows",
              "valid_rows","same_time_copies","collected","labelled","label_first_pending","label_retry_pending",
              "label_missing_body","label_review_pending","occurrences"))
        day = date.fromisoformat(item["day"]).isoformat()
        if not row.get("windows") or not begin<=day<end:
            raise ValueError("invalid training progress day")
        days.append(dict(row,day=day,updated=progress_stamp(item.get("updated"))))
    if len({row["day"] for row in days})!=len(days):
        raise ValueError("duplicate training progress day")
    result = dict(top,schema_version=1,start_day=begin,end_day_exclusive=end,days=days,
                  source="SXData / 财联社快讯" if raw.get("source")=="SXData / 财联社快讯" else "历史快讯")
    for k in ("generated_at","collection_retry_at","collection_inflight_until","label_retry_at","export_at","final_archived_at"):
        result[k] = progress_stamp(raw.get(k))
    if not result["generated_at"]:
        raise ValueError("missing training progress time")
    if raw.get("collection_inflight_day"):
        result["collection_inflight_day"] = date.fromisoformat(raw["collection_inflight_day"]).isoformat()
    if raw.get("collection_pause_reason"):
        result["collection_pause_reason"] = "rate_limited" if raw["collection_pause_reason"]=="rate_limited" else "retry"
    if raw.get("label_error"):
        result["label_error"] = "retry"
    if isinstance(raw.get("latest_collection"),dict):
        result["latest_collection"] = {"checked_at":progress_stamp(raw["latest_collection"].get("checked_at"))}
    if isinstance(raw.get("label_rate"),dict):
        rate = progress_counts(raw["label_rate"],("articles","minutes"))
        if len(rate)==2:
            result["label_rate"] = rate
    return result


def project_file(source,target,transform,previous,dependency=None):
    try:
        version = file_version(source)
    except FileNotFoundError:
        return previous
    if dependency is not None:
        version = (version,dependency)
    if version!=previous or not target.is_file():
        write_snapshot(target,transform(json.loads(source.read_bytes())))
    return version


def publish_training_progress(state,previous=None):
    return project_file(Path(state)/"training-status/progress.json",
                        Path(state)/"request-usage/public-training-progress.json",public_training_progress,previous)


SOURCE_NAMES = {"sxdata":"行情与资讯接口","minimax":"专属模型"}


def verified_source_ids(state,previous):
    try:
        directory = json.loads((state/"request-usage/connected-sources.json").read_bytes())
        if directory.get("schema_version")!=1 or not isinstance(directory.get("sources"),list):
            return previous
        found = {row["id"] for row in directory["sources"] if isinstance(row,dict)
                 and isinstance(row.get("id"),str)}
        return tuple(key for key in SOURCE_NAMES if key in found)
    except (OSError,ValueError,AttributeError):
        return previous


def public_feed(raw,required,source_ids=None):
    if not isinstance(raw,dict) or any(not isinstance(raw.get(k),kind) for k,kind in required):
        raise ValueError("invalid public feed")
    if source_ids is not None:
        raw["sources"] = [{"id":key,"name":SOURCE_NAMES[key]} for key in source_ids]
        raw["sources_url"] = "/data/connected-sources.json"
    return remove_usage(raw)


def live_health(state):
    """Build the public health snapshot from current collector state, not a stale file."""
    now = shanghai_now()
    session = market_session(now) or {}
    gate = live_gate() or {}
    endpoints = {}
    for api, row in (gate.get("apis") or {}).items():
        if not isinstance(row, dict):
            continue
        cooldown, _gap, reason = gate_state(api)
        endpoints[api] = {
            "status": "cooldown" if cooldown else "ready",
            "error_code": reason if cooldown else None,
            "last_success": None,
            "starts": row.get("starts", 0),
            "outcomes": row.get("outcomes", {}),
            "deferred": row.get("deferred", {}),
        }
    try:
        directory = json.loads((state / "request-usage/connected-sources.json").read_bytes())
        sources = directory.get("sources", []) if directory.get("schema_version") == 1 else []
    except (OSError, ValueError, AttributeError):
        sources = []
    payload = {
        "schema_version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "status": "ready" if gate else "pending",
        "market_session": session,
        "target_trade_date": session.get("target_date"),
        "cooldown_until": None,
        "endpoints": endpoints,
        "sources": sources,
        "classification": {"note": "由云端采集器实时汇总，非历史静态快照"},
        "shared_gate": {
            "limit_60s": gate.get("limit_60s"),
            "starts_last_60s": gate.get("starts_last_60s"),
            "peak_60s": gate.get("peak_60s"),
            "recent_rt_429": len(gate.get("recent_rt_429", [])),
        },
    }
    try:
        from recovery_priority import GapLedger
        payload["recovery_priority"] = GapLedger(state=state).summary()
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        payload["recovery_priority"] = None
    return remove_usage(payload)


def publish_feeds(state,versions,site=Path("/srv/ashare-radar-preview/public/data"),strict=False):
    # Current provider status and timestamps live in the small source directory.
    # A new success timestamp must not invalidate the large market snapshot.
    verified = verified_source_ids(state,versions.get("verified_source_ids",()))
    versions["verified_source_ids"] = verified
    feeds = (("training-progress",state/"training-status/progress.json",public_training_progress),
             ("news",state/"news-output/news.json",(("articles",list),("news_digest",dict))),
             ("research",state/"research-output/research.json",(("entities",dict),)),
             ("view",site/"view.json",(("market",list),("sectors",list))))
    failed = []
    for name,source,transform in feeds:
        try:
            target = state/"request-usage"/("public-"+name+".json")
            source_ids = verified if name == "view" else None
            project = transform if callable(transform) else lambda raw: public_feed(raw,transform,source_ids)
            versions[name] = project_file(source,target,project,versions.get(name),source_ids)
            if strict and versions[name] is None:
                failed.append(name)
        except (OSError,ValueError,TypeError,KeyError):
            failed.append(name)
            print("public snapshot unavailable:",name,file=sys.stderr,flush=True)
    try:
        write_snapshot(state/"request-usage/public-health.json",live_health(state))
        versions["health"] = "live"
    except (OSError,ValueError,TypeError,KeyError,sqlite3.Error):
        failed.append("health")
        print("public snapshot unavailable: health",file=sys.stderr,flush=True)
    if strict and failed:
        raise RuntimeError("public snapshots unavailable: "+", ".join(failed))


def main():
    state = STATE
    os.environ.setdefault("RADAR_SERVICE_GATE_FILE",str(state/"relay/rate-gate.json"))
    path = path_for(state)
    next_publish = 0
    versions = {}
    if sys.argv[1:]==["--publish-public"]:
        publish_feeds(state,versions,strict=True)
        return
    while True:
        publish_feeds(state,versions)
        try:
            now = time.time()
            sample(path,live_gate(),now)
            data = dashboard(now,state)
            write_snapshot(state/"request-usage/admin-dashboard.json",data)
            if now >= next_publish:
                publish_sources(state,data)
                next_publish = now+60
        except (OSError,ValueError,sqlite3.Error,TypeError,KeyError):
            print("request usage snapshot unavailable",file=sys.stderr,flush=True)
        time.sleep(10)


if __name__=="__main__":
    main()
