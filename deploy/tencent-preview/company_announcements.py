"""Publish A-share disclosures and a separate, title-based model digest."""

from datetime import datetime, time as day_time, timedelta
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

from pipeline.calendar import SHANGHAI, market_session, shanghai_now
from pipeline.minimax_news import ModelFailure, infer_article
from pipeline.stock_api import FIELDS, _Failure
from relay_common import STATE, collection_due, relay_client
from relay_quotes import publish
from sector_directory import mapping

SOURCE = "tencent_announcements"
FIELDS["anns_d"] = "ann_date,ts_code,name,title,url,rec_time"
STAGE = "announcement_digest"
PROMPT = """你负责总结 A 股上市公司公告。输入每条含 id、证券代码、公司、披露日期、公告标题。
只使用这些公告标题和明确写出的信息。把数据中最值得关注的披露归纳为 1 至 3 点，优先涉及并购、监管、业绩和停复牌的事项。
摘要最多 120 个汉字，避免套话。每点最多 75 个汉字，并附上 1 至 3 个确实支持该点的输入 id。
保持事项状态准确，区分筹划、审议、正式披露和实施。不得推断公告正文、原因、金额、股价影响或提供投资建议。
只返回 JSON，结构为 {"summary":"简短摘要","items":[{"text":"事项","ids":["输入 id"]}]}。"""
HOSTS = frozenset((
    "static.cninfo.com.cn", "www.cninfo.com.cn", "disc.static.szse.cn", "www.szse.cn",
    "www.sse.com.cn", "sse.com.cn", "star.sse.com.cn", "www.bse.cn", "bse.cn",
))
CODE = re.compile(r"(?:00|30|43|60|68|83|87|88|92)\d{4}(?:\.(?:SH|SZ|BJ))?$")
MATERIAL = re.compile(r"业绩|利润|停牌|复牌|合并|并购|收购|重组|立案|处罚|退市|中标|重大合同|资产出售|股份回购|权益变动")
PAGE_SIZE = 300
MAX_PAGES = 12
TIMESTAMP_SCHEMA = 1
TIME_PRECISION = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}.*")


def publication_time(value, day):
    if not isinstance(value,str) or not TIME_PRECISION.fullmatch(value):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    stamp = stamp.astimezone(SHANGHAI) if stamp.tzinfo else stamp.replace(tzinfo=SHANGHAI)
    if abs((stamp.date()-day).days) <= 2:
        return stamp.isoformat(timespec="seconds")
    return None


def normalized(row, day):
    code = row.get("ts_code")
    url = row.get("url")
    title = row.get("title")
    if (row.get("ann_date") != day.strftime("%Y%m%d") or not isinstance(code,str) or not CODE.fullmatch(code)
            or not isinstance(title,str) or not title.strip() or not isinstance(url,str) or len(url)>1800):
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme not in {"http","https"} or parsed.netloc.lower() not in HOSTS or not parsed.path:
        return None
    url = parsed._replace(scheme="https",fragment="").geturl()
    name = row.get("name")
    return {"id":sha256(f"{day}|{code}|{url}".encode()).hexdigest()[:16], "date":day.isoformat(),
            "code":code, "name":name.strip()[:60] if isinstance(name,str) else "",
            "title":" ".join(title.split())[:240], "url":url,
            "published_at":publication_time(row.get("rec_time"),day)}


def fetch_day(day, request, *, pages=MAX_PAGES):
    items, seen = {}, set()
    for page in range(pages):
        try:
            rows = request({"ann_date":day.strftime("%Y%m%d"),"limit":PAGE_SIZE,"offset":page*PAGE_SIZE})
        except _Failure as error:
            print(f"announcements {day}: {error.code}",flush=True)
            return list(items.values()),False
        signature = sha256(json.dumps(rows,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        if signature in seen:
            break
        seen.add(signature)
        for row in rows:
            item = normalized(row,day)
            if item:
                items.setdefault(item["id"],item)
        if len(rows)<PAGE_SIZE:
            return list(items.values()),True
    return list(items.values()),False


def validate_digest(value, ids):
    def valid_text(text, limit):
        return isinstance(text,str) and 0<len(text.strip())<=limit
    if not isinstance(value,dict) or not valid_text(value.get("summary"),145):
        raise ValueError("invalid_announcement_summary")
    rows = value.get("items")
    if not isinstance(rows,list) or not 1<=len(rows)<=3:
        raise ValueError("invalid_announcement_items")
    items = []
    for row in rows:
        if not isinstance(row,dict) or not valid_text(row.get("text"),95):
            raise ValueError("invalid_announcement_point")
        refs = row.get("ids")
        if not isinstance(refs,list) or not 1<=len(refs)<=3 or any(not isinstance(ref,str) or ref not in ids for ref in refs):
            raise ValueError("invalid_announcement_references")
        items.append({"text":row["text"].strip(),"ids":list(dict.fromkeys(refs))})
    return {"summary":value["summary"].strip(),"items":items}


def refresh_digest(day, rows, model_key, now, state):
    previous = state.get("digest")
    collected = {row["id"] for row in rows}
    added = collected.difference(state.get("digest_seen_ids", ()))
    if not added or state.get("retry_at",0)>now.timestamp():
        return previous
    ranked = sorted(rows,key=lambda row:(row["id"] in added,bool(MATERIAL.search(row["title"]))),reverse=True)
    selected = [{k:row[k] for k in ("id","code","name","title","date")} for row in ranked[:45]]
    ids = {row["id"] for row in selected}
    try:
        result = infer_article({"title":f"{day} A 股上市公司公告摘要","full_text":"","digest_articles":selected,
                                "input_scope":"selected_announcement_titles"},
                               {"max_completion_tokens":8192},model_key,prompt=PROMPT,stage=STAGE,
                               validator=lambda result:validate_digest(result,ids))
    except ModelFailure as error:
        state["retry_at"] = now.timestamp()+error.retry
        print(f"announcement digest {day}: {error.code}",flush=True)
        return previous
    state["retry_at"] = 0
    state["digest_seen_ids"] = sorted(collected)
    state["digest"] = {"date":day,"generated_at":shanghai_now().isoformat(),
                       "input_count":len(selected),"summary":result["summary"],"items":result["items"]}
    return state["digest"]


def make_public(sessions, day, now):
    result = sessions.get(day) or {}
    digest = {k:v for k,v in (result.get("digest") or {}).items() if k!="fingerprint"}
    rows = result.get("rows",[])
    return {"schema_version":1,"source":SOURCE,"trade_date":day,"generated_at":now.isoformat(),
            "announcement_count":len(rows),"collection_complete":result.get("complete",False),
            "announcements":rows,"digest":digest or None}


def publish_announcements(path, sessions, latest, now):
    archive = path.with_suffix("")
    days = []
    today = now.date().isoformat()
    start = (now.date()-timedelta(days=now.weekday())).isoformat()
    week_rows = []
    for day, saved in sorted(sessions.items(),reverse=True):
        checked = datetime.fromtimestamp(saved["checked_at"],now.tzinfo)
        packet = make_public(sessions,day,checked)
        publish(archive/(day+".json"),packet)
        days.append({"date":day,"count":packet["announcement_count"],"complete":packet["collection_complete"]})
        if start<=day<=today:
            week_rows.extend(packet["announcements"])
    week_rows.sort(key=lambda row:(row.get("published_at") or row["date"],row["id"]),reverse=True)
    checked_days = {row["date"] for row in days if row["complete"]}
    week_days = [(now.date()-timedelta(days=i)).isoformat() for i in range(now.weekday()+1)]
    publish(path.with_name("company-announcements-week.json"),{
        "schema_version":1,"source":"tencent_announcements_week","week_start":start,"through":today,
        "complete":all(day in checked_days for day in week_days),"announcements":week_rows,
    })
    current = make_public(sessions,latest,now)
    if not current["digest"]:
        previous = max((day for day,saved in sessions.items() if day<latest and saved.get("digest")),default=None)
        if previous:
            prior = make_public(sessions,previous,now)
            current["digest"] = prior["digest"]
            references = {ref for item in prior["digest"]["items"] for ref in item["ids"]}
            current["digest_announcements"] = [row for row in prior["announcements"] if row["id"] in references]
    publish(path,{**current,"days":days,"today":today,"week_start":start})


def announcement_evidence(sessions, days, prefix, *, limit=8):
    items, coverage = {}, {"count":0, "missing_dates":[]}
    for day in days:
        saved = sessions.get(day,{})
        if not saved.get("complete"):
            coverage["missing_dates"].append(day)
        rows = [r for r in saved.get("rows",[]) if r.get("date")==day]
        coverage["count"] += len(rows)
        ranked = sorted(rows,key=lambda r:(bool(MATERIAL.search(r["title"])),r.get("published_at") or ""),reverse=True)
        companies = set()
        for row in ranked:
            if row["code"] in companies:
                continue
            companies.add(row["code"])
            ident = prefix+str(len(items)+1)
            items[ident] = {"id":ident,"title":row["title"],"source":row.get("name") or row["code"],
                            "code":row["code"],"disclosed_on":day,"published_at":row.get("published_at"),
                            "url":row["url"]}
            if len(companies)>=limit:
                break
    coverage["input_count"] = len(items)
    return items,coverage


def collection_days(now, sessions):
    today = now.date()
    for offset in range(today.weekday()+1):
        day = today-timedelta(days=offset)
        old = sessions.get(day.isoformat(),{})
        if offset and old.get("complete") and old.get("timestamp_schema")==TIMESTAMP_SCHEMA:
            checked = old.get("checked_at",0)
            settle = datetime.combine(day+timedelta(days=1),day_time(6),now.tzinfo).timestamp()
            if (offset>1 and checked>=settle) or now.timestamp()-checked<10800:
                continue
        yield day


def collect_announcements(private, public, credentials, now, *, quick=False):
    token = (credentials/"sxdata-token").read_text().strip()
    sessions = mapping(private)
    # Establish a baseline for existing digests before merging fresh disclosures.
    for saved in sessions.values():
        if saved.get("digest") and "digest_seen_ids" not in saved:
            saved["digest_seen_ids"] = sorted({row["id"] for row in saved["rows"]})

    def request(params):
        for attempt in range(3):
            try:
                with relay_client(token,timeout=15) as client:
                    return client.request("anns_d",params)
            except _Failure as error:
                if attempt>=2 or error.code not in {"shared_spacing","relay_busy","local_rate_limit"} or error.retry_seconds>25:
                    raise
                time.sleep(max(1,error.retry_seconds))
    for day in ([now.date()] if quick else collection_days(now,sessions)):
        key = day.isoformat()
        old = sessions.get(key,{})
        rows,complete = fetch_day(day,request,pages=1 if quick else MAX_PAGES)
        merged = {row["id"]:row for row in old.get("rows",[])}
        for row in rows:
            old_time = merged.get(row["id"],{}).get("published_at")
            merged[row["id"]] = {**row,"published_at":row.get("published_at") or old_time}
        sessions[key] = {**old,"checked_at":now.timestamp(),"complete":complete or bool(old.get("complete")),
                         "timestamp_schema":TIMESTAMP_SCHEMA if complete else old.get("timestamp_schema"),
                         "rows":sorted(merged.values(),key=lambda r:(r.get("published_at") or "",r["id"]),reverse=True)}
        publish(private,sessions)
        print(f"announcement date {key}: {len(merged)} rows, complete={complete}",flush=True)
    sessions = {k:v for k,v in sessions.items() if k>=(now.date()-timedelta(days=10)).isoformat()}
    day = max((k for k,v in sessions.items() if v.get("rows")),default=now.date().isoformat())
    publish(private,sessions)
    publish_announcements(public,sessions,day,now)
    result = sessions.get(day,{})
    print(f"announcements {day}: {len(result.get('rows',[]))} items, complete={result.get('complete',False)}",flush=True)


def summarize_announcements(private, public, credentials, now):
    model_key = (credentials/"minimax-key").read_text().strip()
    sessions = mapping(private)
    day = max((k for k,v in sessions.items() if v.get("rows")), default="")
    result = sessions.get(day,{})
    if model_key and (rows := result.get("rows")):
        prior, retry = result.get("digest"), result.get("retry_at")
        if prior and "digest_seen_ids" not in result:
            return
        digest = refresh_digest(day,rows,model_key,now,result)
        if digest is not prior or result.get("retry_at") != retry:
            with private.with_suffix(".lock").open("a") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                current = mapping(private)
                if day not in current:
                    return
                for key in ("digest","digest_seen_ids","retry_at"):
                    if key in result:
                        current[day][key] = result[key]
                publish(private,current)
                latest = max((k for k,v in current.items() if v.get("rows")), default=day)
                publish_announcements(public,current,latest,shanghai_now())


def main():
    now = shanghai_now()
    credentials = Path(os.environ["CREDENTIALS_DIRECTORY"])
    private = STATE/"news/announcements.json"
    public = STATE/"quote-output/company-announcements.json"
    if sys.argv[1:] == ["--digest"]:
        summarize_announcements(private,public,credentials,now)
        return
    scheduled = os.environ.get("RADAR_SCHEDULED_COLLECTION") == "1"
    if scheduled and not collection_due(now, announcements=True):
        return
    trading = market_session(now)["phase"] in {"morning","afternoon","closing_auction"}
    with private.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        collect_announcements(private,public,credentials,now,quick=scheduled and trading and now.minute%15!=7)
    if not scheduled:
        summarize_announcements(private,public,credentials,now)


if __name__=="__main__":
    main()
