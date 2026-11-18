"""Resumable historical news collection, evidence-checked labelling and export."""

import argparse
from contextlib import closing
from datetime import date, datetime, time, timedelta
from hashlib import sha256
import fcntl
import gzip
import json
import os
from pathlib import Path
import shutil
import sqlite3
from tempfile import NamedTemporaryFile, TemporaryDirectory
import unicodedata

from pipeline.calendar import SHANGHAI, shanghai_now
from pipeline.minimax_news import (
    DEFAULT_MODEL, PROMPT_VERSION, ModelFailure, evidence_segments, infer_article,
    resolve_evidence_references, system_prompt, validate_analysis,
)
from pipeline.stock_api import _Failure, _news_records
from relay_common import STATE, relay_client
from relay_quotes import publish

ROOT = STATE / "training-news"
SOURCE = "SXData / 财联社快讯"
OUTPUT = STATE / "training-status/progress.json"
VERSION = "historical-news-v2"
MIN_LABEL_BATCH_GAP = timedelta(seconds=60)
LABEL_TRANSPORT_RETRY_DELAY = timedelta(minutes=5)
BATCH_PROMPT = system_prompt() + """
这次输入是多篇独立快讯。每项按上面的单篇结构分析，增加该项输入 id。
整体输出 {"articles":[{"id":"N1",...}, {"id":"N2",...}]}。每篇须单独判断，
证据片段 ID 只引用同一篇快讯，不将其他文章内容作为证据。每个给定 id 恰好输出一次。"""


def now_text():
    return shanghai_now().isoformat(timespec="seconds")


def open_dataset():
    ROOT.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(ROOT / "dataset.sqlite3", timeout=20)
    db.row_factory = sqlite3.Row
    db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS windows (
            begin_at TEXT PRIMARY KEY, end_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0, retry_at TEXT NOT NULL DEFAULT '',
            rows_received INTEGER, outcome TEXT, checked_at TEXT
        );
        CREATE TABLE IF NOT EXISTS articles (
            id TEXT PRIMARY KEY, day TEXT NOT NULL, published_at TEXT NOT NULL, received_at TEXT NOT NULL,
            title TEXT NOT NULL, body TEXT NOT NULL, channels TEXT, body_sha256 TEXT NOT NULL,
            label TEXT, labelled_at TEXT, tries INTEGER NOT NULL DEFAULT 0,
            retry_at TEXT NOT NULL DEFAULT '', error TEXT
        );
        CREATE INDEX IF NOT EXISTS article_days ON articles(day, published_at);
        CREATE INDEX IF NOT EXISTS article_queue ON articles(label, retry_at, tries, published_at);
        CREATE TABLE IF NOT EXISTS article_occurrences (
            article_id TEXT NOT NULL, published_at TEXT NOT NULL, received_at TEXT NOT NULL, channels TEXT,
            PRIMARY KEY(article_id, published_at)
        );
        CREATE TABLE IF NOT EXISTS window_audit (
            begin_at TEXT PRIMARY KEY, raw_rows INTEGER NOT NULL, parsed_rows INTEGER NOT NULL,
            in_window_rows INTEGER NOT NULL, same_time_copies INTEGER NOT NULL, checked_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS usage (
            id INTEGER PRIMARY KEY, at TEXT NOT NULL, attempted INTEGER NOT NULL, accepted INTEGER NOT NULL,
            input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER, outcome TEXT NOT NULL
        );
    """)
    with db:
        if not metadata(db,"occurrence_migration"):
            db.execute("""INSERT OR IGNORE INTO article_occurrences
                          SELECT id,published_at,received_at,channels FROM articles""")
            db.execute("""UPDATE articles SET label=json_set(label,'$.input_published_at',published_at)
                          WHERE label IS NOT NULL AND json_extract(label,'$.input_published_at') IS NULL""")
            set_meta(db,"occurrence_migration",now_text())
    return db


def metadata(db, key, default=""):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(db, key, value):
    db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))


def final_directory(start_day, end_day_exclusive):
    start = date.fromisoformat(start_day)
    end = date.fromisoformat(end_day_exclusive) - timedelta(days=1)
    return ROOT / "final" / f"{start.isoformat()}_to_{end.isoformat()}"


def file_checksum(path):
    digest = sha256()
    with path.open("rb") as reader:
        while block := reader.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def publish_final(folder, manifest):
    if not manifest["archive_complete"]:
        return
    target = final_directory(manifest["start_day"], manifest["end_day_exclusive"])
    target.parent.mkdir(parents=True, exist_ok=True)
    with (target.parent / ".publish.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.is_dir():
            existing = json.loads((target / "manifest.json").read_text())
            if not existing["archive_complete"] or file_checksum(target / "news.jsonl.gz") != existing["sha256"]:
                raise ValueError("final training archive checksum mismatch")
            return
        with TemporaryDirectory(dir=target.parent, prefix=".publishing-") as staging:
            staged = Path(staging) / "archive"
            staged.mkdir(mode=0o700)
            archive = staged / "news.jsonl.gz"
            shutil.copyfile(folder / "news.jsonl.gz", archive)
            if file_checksum(archive) != manifest["sha256"]:
                raise ValueError("training archive changed while publishing")
            checked = 0
            with gzip.open(archive, "rt", encoding="utf-8") as reader:
                for line in reader:
                    entry = json.loads(line)
                    if entry["label"] is None or not entry["source_occurrences"]:
                        raise ValueError("incomplete record in final training archive")
                    checked += 1
            if checked != manifest["collected"]:
                raise ValueError("training archive row count mismatch")
            publish(staged / "manifest.json", {**manifest, "verified_at": now_text(), "verified_records": checked})
            staged.rename(target)


def initialize(db, day):
    with db:
        if metadata(db, "start_day"):
            return
        start = day - timedelta(days=180)
        set_meta(db, "start_day", start.isoformat())
        set_meta(db, "end_day", day.isoformat())
        for offset in range(180):
            begin = datetime.combine(start + timedelta(days=offset), time(), SHANGHAI)
            db.execute("INSERT INTO windows(begin_at,end_at) VALUES (?,?)",
                       (begin.isoformat(), (begin + timedelta(days=1)).isoformat()))


def credential(name):
    return (Path(os.environ["CREDENTIALS_DIRECTORY"]) / name).read_text().strip()


def content_id(stamp, title, body):
    normalized_title = " ".join(unicodedata.normalize("NFKC", title).split())
    normalized_body = " ".join(unicodedata.normalize("NFKC", body).split())
    identity = sha256(f"cls\0{stamp.date()}\0{normalized_title}\0{normalized_body}".encode()).hexdigest()
    return "sxcls-"+identity


def add_article(db, row, begin, end, received):
    stamp = datetime.fromisoformat(row["published_at"])
    if not begin <= stamp < end:
        return False
    title, body = row["title"], row["full_text"]
    if not (title or body):
        return False
    body_hash = sha256(body.encode()).hexdigest()
    identity = content_id(stamp,title,body)
    cursor = db.execute("""INSERT OR IGNORE INTO articles
        (id,day,published_at,received_at,title,body,channels,body_sha256,error)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (identity, stamp.date().isoformat(), stamp.isoformat(), received, title, body, row.get("channels"),
         body_hash, "missing_body" if not body else None))
    db.execute("INSERT OR IGNORE INTO article_occurrences VALUES (?,?,?,?)",
               (identity,stamp.isoformat(),received,row.get("channels")))
    db.execute("UPDATE articles SET published_at=? WHERE id=? AND published_at>?",
               (stamp.isoformat(),identity,stamp.isoformat()))
    return cursor.rowcount > 0


def split_window(db, window, begin, end, checked, reason):
    middle = begin + timedelta(seconds=int((end-begin).total_seconds()//2))
    db.execute("""UPDATE windows SET end_at=?, state='pending',rows_received=NULL,
                  attempts=attempts+1, checked_at=?, outcome=?
                  WHERE begin_at=?""", (middle.isoformat(), checked, reason, window["begin_at"]))
    db.execute("INSERT OR IGNORE INTO windows(begin_at,end_at) VALUES (?,?)",
               (middle.isoformat(), end.isoformat()))


def collect(db, now):
    token = credential("sxdata-token")
    checked = now.isoformat(timespec="seconds")
    window = db.execute("""SELECT w.* FROM windows w
        LEFT JOIN window_audit audit ON audit.begin_at=w.begin_at
        WHERE (w.state='pending' OR w.state='complete' AND audit.begin_at IS NULL) AND w.retry_at<=?
        ORDER BY CASE WHEN w.state='complete' THEN 0 ELSE 1 END,w.attempts,w.begin_at LIMIT 1""",
        (checked,)).fetchone()
    if not window:
        return
    begin, end = (datetime.fromisoformat(window[key]) for key in ("begin_at", "end_at"))
    try:
        with relay_client(token, timeout=20) as client:
            rows = client.request("news", {"src": "cls", "start_date": begin.strftime("%F %T"),
                          "end_date": (end-timedelta(seconds=1)).strftime("%F %T"), "limit": 1500})
    except _Failure as error:
        with db:
            if error.code == "response_too_large" and end-begin > timedelta(minutes=2):
                split_window(db, window, begin, end, checked, error.code)
            else:
                delay = max(60, min(86400, error.retry_seconds or 60))
                db.execute("""UPDATE windows SET retry_at=?, attempts=attempts+1, outcome=?, checked_at=?
                    WHERE begin_at=?""", ((shanghai_now()+timedelta(seconds=delay)).isoformat(), error.code,
                                           checked, window["begin_at"]))
        print(json.dumps({"begin":window["begin_at"],"status":error.code}),flush=True)
        return
    if len(rows) >= 1500 and end-begin > timedelta(minutes=2):
        with db:
            split_window(db, window, begin, end, checked, "split_at_limit")
        return
    # Use the historical window end for the parser's relative age guard.
    # The capture stamp always reflects actual backfill availability.
    received = now_text()
    parsed = _news_records(rows, end, received, token)
    in_window = [row for row in parsed if begin<=datetime.fromisoformat(row["published_at"])<end]
    instances = {(content_id(datetime.fromisoformat(row["published_at"]),row["title"],row["full_text"]),
                  row["published_at"]) for row in in_window}
    with db:
        count = sum(add_article(db, row, begin, end, received) for row in in_window)
        state = "complete" if len(rows) < 1500 else "capped"
        outcome = "empty" if not rows else "partial_parse" if len(in_window)!=len(rows) else "ok"
        if len(in_window)!=len(rows):
            state = "unverified"
        if window["state"]=="complete" and len(rows)<(window["rows_received"] or 0):
            db.execute("""UPDATE windows SET retry_at=?,attempts=attempts+1,outcome=?,checked_at=?
                          WHERE begin_at=?""", ((shanghai_now()+timedelta(minutes=30)).isoformat(),
                          "recheck_less_than_original",received,window["begin_at"]))
            return
        db.execute("""UPDATE windows SET state=?,rows_received=?,outcome=?,checked_at=?,attempts=attempts+1
                      WHERE begin_at=?""", (state, len(rows), outcome, received, window["begin_at"]))
        db.execute("INSERT OR REPLACE INTO window_audit VALUES (?,?,?,?,?,?)",
                   (window["begin_at"],len(rows),len(parsed),len(in_window),len(in_window)-len(instances),received))
    print(json.dumps({"begin":window["begin_at"],"end":window["end_at"],"inserted":count,
                      "received":len(rows),"status":state}),flush=True)


def label_batch(rows, key):
    indexed = {f"N{i}": row for i, row in enumerate(rows, 1)}
    spans = {name: evidence_segments(row["body"]) for name, row in indexed.items()}
    packet = [{"id": name, "title": row["title"], "published_at": row["published_at"],
               "article_segments": [{"id": part, "text": text} for part, text in spans[name].items()]}
              for name, row in indexed.items()]

    def validate(value):
        records = value.get("articles")
        if not isinstance(records, list):
            raise ValueError("invalid_analysis")
        results, seen = {}, set()
        for item in records:
            name = item.get("id") if isinstance(item, dict) else None
            if not isinstance(name, str) or name not in indexed:
                continue
            if name in seen:
                results.pop(indexed[name]["id"], None)
                continue
            seen.add(name)
            try:
                results[indexed[name]["id"]] = validate_analysis(resolve_evidence_references(item, spans[name]),
                                                               indexed[name]["body"])
            except (ValueError, KeyError, TypeError):
                continue
        return {"results": results}

    if len(rows) == 1:
        row = rows[0]
        result = infer_article({"title": row["title"], "full_text": row["body"],
            "published_at": row["published_at"], "source": SOURCE}, {}, key)
        usage = result.pop("usage", {})
        return {"results": {row["id"]: result}, "usage": usage}
    return infer_article({"title": "多篇历史快讯独立标注", "full_text": json.dumps(packet, ensure_ascii=False),
                          "digest_articles": packet}, {}, key, prompt=BATCH_PROMPT, validator=validate)


def label(db, now, limit):
    stamp = now.isoformat(timespec="seconds")
    if metadata(db, "label_pause_until") > stamp:
        return
    last_attempt = db.execute("SELECT at,attempted,outcome FROM usage ORDER BY id DESC LIMIT 1").fetchone()
    if last_attempt and now < datetime.fromisoformat(last_attempt["at"]) + MIN_LABEL_BATCH_GAP:
        print(json.dumps({"label": "inter_batch_wait"}), flush=True)
        return
    if last_attempt and last_attempt["outcome"] == "transport_error":
        limit = min(limit, max(1, last_attempt["attempted"] // 2))
    rows = db.execute("""SELECT * FROM articles WHERE label IS NULL AND length(body)>0 AND retry_at<=?
                          AND tries<6 ORDER BY tries, published_at LIMIT ?""", (stamp, limit)).fetchall()
    if not rows:
        return
    # Isolate retries so one malformed article cannot block a batch.
    rows = rows[:1] if rows[0]["tries"] else [row for row in rows if not row["tries"]]
    retained = []
    size = 0
    for row in rows:
        if retained and size+len(row["body"]) > 33000:
            break
        retained.append(row)
        size += len(row["body"])
    rows = retained
    key = credential("minimax-key")
    try:
        response = label_batch(rows, key)
    except ModelFailure as error:
        with db:
            delay = (LABEL_TRANSPORT_RETRY_DELAY if error.code == "transport_error"
                     else timedelta(seconds=max(60, min(error.retry, 86400))))
            retry = (shanghai_now()+delay).isoformat(timespec="seconds")
            if error.code.startswith(("invalid_", "input_over_budget", "incomplete_response")):
                db.executemany("UPDATE articles SET tries=tries+1,retry_at=?,error=? WHERE id=?",
                               ((retry, error.code, row["id"]) for row in rows))
            else:
                set_meta(db, "label_pause_until", retry)
                set_meta(db, "label_error", error.code)
            db.execute("INSERT INTO usage(at,attempted,accepted,outcome) VALUES (?,?,0,?)",
                       (now_text(), len(rows), error.code))
        print(json.dumps({"label":error.code}),flush=True)
        return
    completed = now_text()
    valid = response["results"]
    usage = response.get("usage", {})
    with db:
        for row in rows:
            output = valid.get(row["id"])
            if output is not None:
                output["annotation_prompt"] = PROMPT_VERSION + ("+independent-batch-v1" if len(rows)>1 else "")
                output["input_published_at"] = row["published_at"]
                db.execute("UPDATE articles SET label=?,labelled_at=?,tries=tries+1,error=NULL WHERE id=?",
                           (json.dumps(output, ensure_ascii=False), completed, row["id"]))
            else:
                db.execute("UPDATE articles SET tries=tries+1,error='invalid_batch_item' WHERE id=?", (row["id"],))
        db.execute("""INSERT INTO usage(at,attempted,accepted,input_tokens,output_tokens,total_tokens,outcome)
                       VALUES (?,?,?,?,?,?,?)""", (completed, len(rows), len(valid), usage.get("prompt_tokens"),
                       usage.get("completion_tokens"), usage.get("total_tokens"), "ok" if len(valid)==len(rows) else "partial"))
        set_meta(db, "label_error", "")
        set_meta(db, "label_pause_until", "")
    print(json.dumps({"submitted":len(rows),"labelled":len(valid),"tokens":usage.get("total_tokens")}),flush=True)


def progress(db):
    days = {}
    for row in db.execute("""SELECT substr(w.begin_at,1,10) AS day,count(*) AS windows,
        sum(w.state='complete') AS complete_windows,sum(w.state IN ('capped','unverified')) AS incomplete_windows,
        max(w.checked_at) AS updated,sum(w.rows_received) AS returned,count(audit.begin_at) AS audited_windows,
        sum(audit.in_window_rows) AS valid_rows,sum(audit.same_time_copies) AS same_time_copies
        FROM windows w LEFT JOIN window_audit audit ON w.begin_at=audit.begin_at GROUP BY 1"""):
        days[row["day"]] = dict(row)
    result = db.execute("""SELECT count(*) AS collected,count(label) AS labelled,
                            sum(length(body)=0) AS missing_body,sum(tries>=6 AND label IS NULL) AS exhausted
                            FROM articles""").fetchone()
    for row in db.execute("SELECT day,count(*) AS collected,count(label) AS labelled FROM articles GROUP BY day"):
        days[row["day"]].update(collected=row["collected"],labelled=row["labelled"])
    for row in db.execute("""SELECT a.day,count(*) AS occurrences FROM article_occurrences o
                             JOIN articles a ON a.id=o.article_id GROUP BY a.day"""):
        days[row["day"]]["occurrences"] = row["occurrences"]
    usage = db.execute("""SELECT count(*) AS calls,sum(input_tokens) AS input_tokens,
        sum(output_tokens) AS output_tokens,sum(total_tokens) AS total_tokens FROM usage""").fetchone()
    window_count = db.execute("SELECT coalesce(sum(attempts),0) FROM windows").fetchone()[0]
    states = list(days.values())
    completed = sum(row["windows"] == row["complete_windows"] for row in states)
    audited = sum(row["windows"]==row["audited_windows"] and row["windows"]==row["complete_windows"] for row in states)
    latest = db.execute("SELECT checked_at,outcome FROM windows WHERE checked_at IS NOT NULL ORDER BY checked_at DESC LIMIT 1").fetchone()
    start_day, end_day = metadata(db,"start_day"), metadata(db,"end_day")
    verified_at = None
    target = final_directory(start_day, end_day)
    if (target / "news.jsonl.gz").is_file() and (target / "manifest.json").is_file():
        verified_at = json.loads((target / "manifest.json").read_text()).get("verified_at")
    return {"schema_version":1,"source":SOURCE,"model":DEFAULT_MODEL,"start_day":start_day,
            "end_day_exclusive":end_day,"total_days":len(states),"queried_days":completed,"audited_days":audited,
            "days_with_news":sum(row.get("collected",0)>0 for row in states),
            **{key:(value or 0) for key,value in dict(result).items()}, "usage":{k:(v or 0) for k,v in dict(usage).items()},
            "collection_attempts":window_count,"latest_collection":dict(latest) if latest else None,
            "label_error":metadata(db,"label_error"),"label_retry_at":metadata(db,"label_pause_until"),
            "export_at":metadata(db,"export_at"),"final_archived_at":verified_at,
            "generated_at":now_text(),"days":states}


def export(db):
    folder = ROOT / "export"
    folder.mkdir(exist_ok=True)
    db.execute("BEGIN")
    snapshot = progress(db)
    rows = db.execute("SELECT * FROM articles ORDER BY published_at,id")
    with NamedTemporaryFile(dir=folder,delete=False) as handle:
        temporary = Path(handle.name)
        try:
            with gzip.GzipFile(fileobj=handle,mode="wb",mtime=0) as writer:
                for row in rows:
                    info = {key:row[key] for key in ("id","published_at","received_at","title","body","body_sha256","channels","labelled_at")}
                    occurrences = [dict(item) for item in db.execute("""SELECT published_at,received_at,channels
                           FROM article_occurrences WHERE article_id=? ORDER BY published_at""",(row["id"],))]
                    info.update(source=SOURCE,backfilled=True,label=json.loads(row["label"]) if row["label"] else None,
                                source_occurrences=occurrences,
                                annotation_status="labelled" if row["label"] else (row["error"] or "pending"),
                                model=DEFAULT_MODEL if row["label"] else None,prompt_version=PROMPT_VERSION if row["label"] else None)
                    writer.write((json.dumps(info,ensure_ascii=False,separators=(",",":"))+"\n").encode())
            handle.flush()
            checksum = file_checksum(temporary)
            stamp = now_text()
            db.commit()
            temporary.replace(folder/"news.jsonl.gz")
            snapshot.pop("days")
            manifest = {**snapshot,"version":VERSION,"export_at":stamp,
                        "sha256":checksum,"format":"UTF-8 JSON Lines + gzip","timezone":"Asia/Shanghai",
                        "archive_complete":snapshot["total_days"]==snapshot["audited_days"]==180
                        and snapshot["collected"]>0 and snapshot["collected"]==snapshot["labelled"]}
            publish(folder/"manifest.json", manifest)
            publish_final(folder, manifest)
            with db:
                set_meta(db,"export_at",stamp)
        finally:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("job",choices=("collect","label","export","progress"))
    parser.add_argument("--batch-size",type=int,choices=range(1,17),default=10)
    args = parser.parse_args()
    now = shanghai_now()
    with closing(open_dataset()) as db:
        initialize(db,now.date())
        if args.job=="collect":
            collect(db,now)
        elif args.job=="label":
            with (ROOT/"label.lock").open("a") as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                label(db,shanghai_now(),args.batch_size)
        elif args.job=="export":
            export(db)
        with (ROOT/"progress.lock").open("a") as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            publish(OUTPUT,progress(db))
