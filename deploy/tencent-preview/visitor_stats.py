"""Aggregate Nginx visitor access logs into a private SQLite store and an admin-only snapshot.

Nginx writes one JSON line per public HTTPS request (see log_format visitor in nginx.conf).
This worker incrementally parses new lines, stores them in a dedicated database, and
publishes an atomic aggregate for the administration console. The console never opens
the database directly.
"""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

ACCESS_LOG = Path("/var/lib/ashare-radar-preview-nginx/visitor-access.log")
STATE = Path("/var/lib/ashare-radar-preview/visitors")
DB = STATE / "visits.sqlite3"
OFFSET = STATE / "offset.json"
SNAPSHOT = STATE / "admin-visitors.json"
SHANGHAI = timezone(timedelta(hours=8))
RETENTION_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS visits (
    id INTEGER PRIMARY KEY,
    epoch INTEGER NOT NULL,
    at TEXT NOT NULL,
    ip TEXT NOT NULL,
    host TEXT NOT NULL,
    path TEXT NOT NULL,
    status INTEGER NOT NULL,
    ua TEXT NOT NULL,
    dedup TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS visits_epoch ON visits(epoch);
CREATE INDEX IF NOT EXISTS visits_ip ON visits(ip);
"""
STATIC = re.compile(r"\.(?:js|css|json|png|jpe?g|gif|svg|ico|woff2?|map|txt)$", re.I)
NOISE = ("/data/", "/api/", "/internal/")


def connect(path):
    db = sqlite3.connect(path, timeout=15)
    db.execute("PRAGMA journal_mode=WAL")
    return db


def is_page(path):
    return not path.startswith(NOISE) and not STATIC.search(path) and "/data/" not in path


def parse_line(raw):
    try:
        row = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(row, dict):
        return None
    ip, path, at = row.get("ip"), row.get("path"), row.get("at")
    if not isinstance(ip, str) or not ip or not isinstance(path, str) or not isinstance(at, str):
        return None
    try:
        moment = datetime.fromisoformat(at)
    except ValueError:
        return None
    status = row.get("status")
    status = status if isinstance(status, int) and 0 <= status < 600 else 0
    ua = row.get("ua") if isinstance(row.get("ua"), str) else ""
    host = row.get("host") if isinstance(row.get("host"), str) else ""
    key = hashlib.sha256(f"{int(moment.timestamp())}|{ip}|{path}|{status}|{ua}".encode()).hexdigest()
    return {"epoch": int(moment.timestamp()), "at": at, "ip": ip[:64], "host": host[:200],
            "path": path[:400], "status": status, "ua": ua[:300], "dedup": key}


def _read_state(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def fh_read_all(path):
    with path.open("rb") as handle:
        return handle.read()


def collect(log=ACCESS_LOG, db_path=DB, state_path=OFFSET, now=None):
    """Ingest lines appended since the last run. Returns the number of new rows."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    stat = log.stat() if log.exists() else None
    size = stat.st_size if stat else 0
    saved = _read_state(state_path)
    offset = saved.get("size", 0)
    rotated = stat is not None and saved.get("ino") not in (None, stat.st_ino)
    if not isinstance(offset, int) or offset < 0 or offset > size or rotated:
        offset = 0  # log rotated (new inode) or truncated
    added = 0
    consumed = 0
    if size > offset:
        with log.open("rb") as fh:
            fh.seek(offset)
            data = fh.read()
        # If the saved offset lands mid-line (e.g. the file was replaced in place
        # with the same inode), fall back to reading from the beginning.
        if data and b"\n" in data and not data.startswith(b"{"):
            first_newline = data.index(b"\n") + 1
            if not data[:first_newline].lstrip().startswith(b"{"):
                offset, data = 0, fh_read_all(log)
        with closing(connect(db_path)) as db, db:
            db.executescript(SCHEMA)
            for raw in data.splitlines(keepends=True):
                if not raw.endswith(b"\n"):
                    break  # incomplete tail, retry on the next run
                consumed += len(raw)
                row = parse_line(raw)
                if not row:
                    continue
                cursor = db.execute("""INSERT OR IGNORE INTO visits
                    (epoch,at,ip,host,path,status,ua,dedup) VALUES(?,?,?,?,?,?,?,?)""",
                    (row["epoch"], row["at"], row["ip"], row["host"], row["path"],
                     row["status"], row["ua"], row["dedup"]))
                added += cursor.rowcount
    payload = {"size": offset + consumed, "ino": stat.st_ino if stat else None}
    fd, tmp = tempfile.mkstemp(prefix="visitor-offset-", dir=str(state_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, state_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    purge(db_path, now=now)
    return added


def purge(db_path, days=RETENTION_DAYS, now=None):
    cutoff = int((now or datetime.now(timezone.utc)).timestamp()) - days * 86400
    with closing(connect(db_path)) as db, db:
        db.executescript(SCHEMA)
        db.execute("DELETE FROM visits WHERE epoch < ?", (cutoff,))


def build_snapshot(db_path=DB, now=None, top=20, days=RETENTION_DAYS, recent=50):
    now = now or datetime.now(timezone.utc)
    current = int(now.timestamp())
    with closing(connect(db_path)) as db:
        db.executescript(SCHEMA)
        def pair(since):
            return db.execute("SELECT count(*), count(DISTINCT ip) FROM visits WHERE epoch>=?",
                              (since,)).fetchone()
        all_req, all_ip = db.execute("SELECT count(*), count(DISTINCT ip) FROM visits").fetchone()
        day_req, day_ip = pair(current - 86400)
        week_req, week_ip = pair(current - 7 * 86400)
        hour_start = int(now.replace(minute=0, second=0, microsecond=0).timestamp())
        hourly = []
        for back in range(23, -1, -1):
            start = hour_start - back * 3600
            requests, unique = db.execute(
                "SELECT count(*), count(DISTINCT ip) FROM visits WHERE epoch>=? AND epoch<?",
                (start, start + 3600)).fetchone()
            hourly.append({"hour": datetime.fromtimestamp(start, SHANGHAI).strftime("%m-%d %H:00"),
                           "requests": requests, "unique_ips": unique})
        since = current - 7 * 86400
        top_ips = []
        for ip, requests, pages, first, last in db.execute("""SELECT ip, count(*),
                sum(CASE WHEN path NOT LIKE '/data/%' AND path NOT LIKE '/api/%'
                    AND path NOT LIKE '/internal/%' AND instr(path,'.')=0 THEN 1 ELSE 0 END),
                min(epoch), max(epoch)
                FROM visits WHERE epoch>=? GROUP BY ip ORDER BY count(*) DESC, ip LIMIT ?""",
                (since, top)):
            sample = db.execute("SELECT ua FROM visits WHERE ip=? ORDER BY epoch DESC LIMIT 1",
                                (ip,)).fetchone()
            top_ips.append({"ip": ip, "requests": requests, "page_views": pages or 0,
                            "first_seen": datetime.fromtimestamp(first, SHANGHAI).isoformat(),
                            "last_seen": datetime.fromtimestamp(last, SHANGHAI).isoformat(),
                            "ua_sample": (sample[0] if sample else "")[:140]})
        top_paths = [{"path": path, "requests": requests, "unique_ips": unique}
                     for path, requests, unique in db.execute("""SELECT path, count(*), count(DISTINCT ip)
                        FROM visits WHERE epoch>=? AND path NOT LIKE '/data/%' AND path NOT LIKE '/api/%'
                        AND path NOT LIKE '/internal/%' AND instr(path,'.')=0
                        GROUP BY path ORDER BY count(*) DESC LIMIT ?""", (since, top))]
        latest = []
        for at, ip, path, status in db.execute(
                "SELECT at, ip, path, status FROM visits ORDER BY epoch DESC, id DESC LIMIT ?",
                (recent,)):
            try:
                shown = datetime.fromisoformat(at).astimezone(SHANGHAI).strftime("%m-%d %H:%M:%S")
            except ValueError:
                shown = at[:19]
            latest.append({"at": shown, "ip": ip, "path": path, "status": status})
    return {
        "schema_version": 1,
        "generated_at": now.astimezone(SHANGHAI).isoformat(),
        "retention_days": days,
        "totals": {
            "requests_24h": day_req, "unique_ips_24h": day_ip,
            "requests_7d": week_req, "unique_ips_7d": week_ip,
            "requests_all": all_req, "unique_ips_all": all_ip,
        },
        "hourly_24h": hourly,
        "top_ips": top_ips,
        "top_paths": top_paths,
        "recent": latest,
    }


def publish(db_path=DB, target=SNAPSHOT, now=None):
    data = build_snapshot(db_path, now=now)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="visitor-snapshot-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("collect", "publish", "run"))
    parser.add_argument("--log", type=Path, default=ACCESS_LOG)
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--offset", type=Path, default=OFFSET)
    parser.add_argument("--snapshot", type=Path, default=SNAPSHOT)
    args = parser.parse_args()
    if args.action in ("collect", "run"):
        added = collect(args.log, args.db, args.offset)
        print(f"visitor rows added: {added}", flush=True)
    if args.action in ("publish", "run"):
        data = publish(args.db, args.snapshot)
        print(f"visitor snapshot: {data['totals']['requests_all']} requests, "
              f"{data['totals']['unique_ips_all']} IPs", flush=True)


if __name__ == "__main__":
    main()
