"""Collect searchable exchange sectors in existing index slots and publish their real observations."""
from contextlib import closing
import fcntl
import json
import re
import sqlite3


def catalog_codes(path):
    try:
        doc = json.loads(path.read_bytes())
        if doc["schema_version"] != 1 or not 1 <= len(doc["items"]) <= 750:
            raise ValueError("invalid sector catalog size or version")
        codes = [row["code"] for row in doc["items"]]
        if (not all(isinstance(code, str) and re.fullmatch(r"[0-9]{6}\.(?:SH|SZ)", code) for code in codes)
                or len(set(codes)) != len(codes)):
            raise ValueError("invalid or duplicate sector code")
        return {code: code for code in codes}
    except (OSError, KeyError, TypeError, ValueError):
        return {}


class IndexBatches:
    """Use the combined universe when supported and protect core quotes on fallback."""
    def __init__(self, core, codes, *, prefer_combined=False):
        self.core = core
        self.all = {**codes, **core}  # Keep existing market entity IDs on shared codes.
        self.prefer_combined = prefer_combined
        self.directory_turn = False
        self.exchange = "SH"
        self.split_until = 0

    def choose(self, stamp):
        if not self.directory_turn:
            return self.core, None
        if stamp >= self.split_until:
            return self.all, None
        suffix = "." + self.exchange
        return {code: ident for code, ident in self.all.items() if code.endswith(suffix)}, "*" + suffix

    def finish(self, stamp, fetched_count, expected):
        # A provider may truncate a long code list. Use the two verified venue
        # wildcards for the next ten minutes, then try the combined list again.
        if self.directory_turn:
            if stamp < self.split_until:
                self.exchange = "SZ" if self.exchange == "SH" else "SH"
            elif fetched_count < expected * .9:
                self.split_until = stamp + 600
        self.directory_turn = (self.prefer_combined and stamp >= self.split_until) or not self.directory_turn


SCHEMA = """
CREATE TABLE IF NOT EXISTS latest (
    id TEXT PRIMARY KEY, source_stamp TEXT NOT NULL, body TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS samples (
    id TEXT, minute TEXT, source_stamp TEXT NOT NULL, price REAL NOT NULL,
    PRIMARY KEY(id,minute)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS dirty_charts (id TEXT PRIMARY KEY) WITHOUT ROWID;
CREATE TRIGGER IF NOT EXISTS mark_chart AFTER INSERT ON samples
BEGIN INSERT OR IGNORE INTO dirty_charts VALUES (new.id); END;
CREATE TABLE IF NOT EXISTS publication (day TEXT PRIMARY KEY) WITHOUT ROWID;
"""


class SectorFeed:
    def __init__(self, private, directory, codes, publish):
        self.database = private / "sector-directory.sqlite3"
        self.lock = private / "sector-directory.lock"
        self.directory = directory
        self.codes = codes
        self.publish = publish
        self.day = None

    def update(self, history, core_ids, fetched, now):
        # Only directory publishers use this lock. It also serializes a manual
        # lunch/close bootstrap with startup and never spans an upstream call.
        day = now.date().isoformat()
        with self.lock.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with closing(sqlite3.connect(self.database, timeout=2)) as db:
                if self.day != day:
                    db.executescript(SCHEMA)
                recorded = db.execute("SELECT day FROM publication").fetchone()
                if recorded and recorded[0] > day:
                    return
                initialize = self.day != day
                first_day = not recorded or recorded[0] != day
                with db:
                    if initialize and history is not None:
                        # Preserve this morning's actual core minutes when the
                        # directory is first enabled in the middle of the day.
                        for code in self.codes.keys() & core_ids:
                            rows = history.execute("""SELECT id,minute,source_stamp,price FROM samples
                                WHERE id=? AND minute BETWEEN ? AND ? ORDER BY minute""",
                                (code, day + "T09:30", day + "T15:00"))
                            db.executemany("INSERT OR IGNORE INTO samples VALUES (?,?,?,?)", rows)
                    changed = self._store(db, fetched, day)
                    if first_day:
                        db.execute("DELETE FROM publication")
                        db.execute("INSERT INTO publication VALUES (?)", (day,))
                    if initialize:
                        # Also repairs a chart lost after a service interruption.
                        db.executemany("INSERT OR IGNORE INTO dirty_charts VALUES (?)", ((c,) for c in self.codes))
                self._project(db, now, day, changed or initialize or first_day)
                self.day = day

    def _store(self, db, fetched, day):
        changed = False
        for quote in fetched:
            code = quote["id"]
            if code not in self.codes or quote["quote_date"] != day:
                continue
            stamp = quote["quote_timestamp"]
            body = json.dumps(quote, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            count = db.execute("""INSERT INTO latest VALUES (?,?,?) ON CONFLICT(id) DO UPDATE
                SET source_stamp=excluded.source_stamp,body=excluded.body
                WHERE excluded.source_stamp>latest.source_stamp OR
                    (excluded.source_stamp=latest.source_stamp AND latest.body!=excluded.body)""",
                (code, stamp, body)).rowcount
            changed |= bool(count)
            hhmm = stamp[11:16]
            if "09:30" <= hhmm <= "11:30" or "13:00" <= hhmm <= "15:00":
                db.execute("INSERT OR IGNORE INTO samples VALUES (?,?,?,?)",
                           (code, stamp[:16], stamp, quote["price"]))
        return changed

    def _project(self, db, now, day, write_quotes):
        path = self.directory / "sector-quotes.json"
        # Write the latest first so a slower chart/disk cannot hide new prices.
        if write_quotes or not path.is_file():
            quotes = []
            for code, body in db.execute("SELECT id,body FROM latest ORDER BY id"):
                if code in self.codes:
                    quote = json.loads(body)
                    if quote["quote_date"] < day:
                        quote["stale"] = True
                    quotes.append(quote)
            self.publish(path, {"schema_version": 1, "source": "sxdata_relay", "trade_date": day,
                                "generated_at": now.isoformat(), "sectors": quotes})
        dirty = [row[0] for row in db.execute("SELECT id FROM dirty_charts")]
        for code in dirty:
            if code not in self.codes:
                continue
            points = db.execute("""SELECT source_stamp,price FROM samples
                WHERE id=? AND minute BETWEEN ? AND ? ORDER BY minute""",
                (code, day + "T09:30", day + "T15:00")).fetchall()
            self.publish(self.directory / "sector-intraday" / (code + ".json"),
                         {"schema_version": 1, "source": "sxdata_relay", "trade_date": day,
                          "id": code, "points": points})
        if dirty:
            with db:
                db.execute("DELETE FROM dirty_charts")
