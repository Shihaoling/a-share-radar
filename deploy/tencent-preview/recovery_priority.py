"""Rank outstanding collection gaps by data importance and time lost.

Market quotes always outrank news, announcements and research reports. Within a
kind, a gap that has been missing longer is more urgent. Collectors register the
gaps they still see, resolve the ones they repaired, and take the highest scoring
work in the next scheduled window. Lower-priority backfill windows step aside
while a recently observed quote gap remains open.
"""
from contextlib import closing
from datetime import date, datetime, time as day_time
from pathlib import Path
import time as clock

from pipeline.calendar import SHANGHAI
from relay_common import STATE
from collector_db import connect as collector_connect, retry_locked

# The smallest possible quote score must stay above the largest possible score of
# any other kind, so a stale article can never displace a fresh quote gap.
WEIGHTS = {
    "quote": 100.0,
    "announcement": 24.0,
    "research": 22.0,
    "news": 20.0,
    "other": 8.0,
}
AGE_BOOST = 3.0
AGE_CAP_SECONDS = 7 * 24 * 3600
QUOTE_FLOOR = WEIGHTS["quote"]
OTHER_CEILING = max(value for kind, value in WEIGHTS.items() if kind != "quote") * (1 + AGE_BOOST)
assert QUOTE_FLOOR > OTHER_CEILING, "quote priority must dominate every other kind"

# A quote gap only presses lower-priority backfill after it survived one full
# recovery window. Very old, permanently unrecoverable history is excluded so it
# cannot starve news, announcement and report work indefinitely.
DEFER_AFTER_SECONDS = 15 * 60
DEFER_UNTIL_SECONDS = 24 * 3600
RECENT_OBSERVATION_SECONDS = 10 * 60
MAX_CONSECUTIVE_DEFERRALS = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS recovery_gaps (
    kind TEXT NOT NULL, gap_key TEXT NOT NULL,
    missing_since REAL NOT NULL, last_seen REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, resolved_at REAL,
    PRIMARY KEY(kind,gap_key)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS recovery_gaps_open
    ON recovery_gaps(resolved_at,kind,missing_since);
CREATE TABLE IF NOT EXISTS recovery_meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
) WITHOUT ROWID;
"""


def epoch(value=None):
    """Accept epoch seconds, dates, datetimes or ISO stamps; return epoch seconds."""
    if value is None:
        return clock.time()
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=SHANGHAI)).timestamp()
    text = str(value)
    if len(text) >= 16 and text[10] in "T ":
        try:
            return datetime.fromisoformat(text[:16]).replace(tzinfo=SHANGHAI).timestamp()
        except ValueError:
            pass
    try:
        day = date.fromisoformat(text[:10])
    except ValueError:
        return clock.time()
    return datetime.combine(day, day_time(15, 0)).replace(tzinfo=SHANGHAI).timestamp()


def due_stamp(value, *, hour=15, minute=0):
    """When a dated observation should have been available (Shanghai time)."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=SHANGHAI)).timestamp()
    text = str(value)
    if len(text) >= 16 and text[10] in "T ":
        try:
            return datetime.fromisoformat(text[:16]).replace(tzinfo=SHANGHAI).timestamp()
        except ValueError:
            pass
    try:
        day = date.fromisoformat(text[:10])
    except ValueError:
        return clock.time()
    return datetime.combine(day, day_time(hour, minute)).replace(tzinfo=SHANGHAI).timestamp()


def score(kind, missing_since, now=None):
    """Importance weight scaled by how long the data has been missing."""
    now = epoch(now)
    age = max(0.0, now - epoch(missing_since))
    freshness = 1.0 + AGE_BOOST * min(age, AGE_CAP_SECONDS) / AGE_CAP_SECONDS
    return WEIGHTS.get(kind, WEIGHTS["other"]) * freshness


def chunks(items, size=400):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _write(path, task):
    with closing(collector_connect(path)) as db:
        def run():
            with db:
                return task(db)
        return retry_locked(run, attempts=4, base=0.4)


def _read(path, task):
    with closing(collector_connect(path)) as db:
        return task(db)


class GapLedger:
    """Small, separate SQLite ledger so recovery scheduling never contends with
    the quote and research databases."""

    def __init__(self, path=None, *, state=STATE):
        self.path = Path(path) if path is not None else Path(state) / "recovery" / "gaps.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _write(self.path, lambda db: db.executescript(SCHEMA))

    def observe(self, kind, gaps, now=None):
        """Register gaps still missing; the first sighting fixes their age.

        ``gaps`` accepts plain keys (first seen now) or ``(key, missing_since)``
        pairs. A previously resolved gap that reappears starts a new lifetime.
        """
        seen = epoch(now)
        rows = []
        for item in gaps:
            key, since = item if isinstance(item, (tuple, list)) and len(item) == 2 else (item, seen)
            rows.append((kind, str(key), epoch(since), seen))
        if not rows:
            return 0

        def task(db):
            db.executemany("""UPDATE recovery_gaps SET missing_since=?,last_seen=?,resolved_at=NULL
                              WHERE kind=? AND gap_key=? AND resolved_at IS NOT NULL""",
                           ((since, seen, kind, key) for kind, key, since, seen in rows))
            db.executemany("""INSERT OR IGNORE INTO recovery_gaps
                              (kind,gap_key,missing_since,last_seen) VALUES(?,?,?,?)""", rows)
            db.executemany("""UPDATE recovery_gaps SET last_seen=?
                              WHERE kind=? AND gap_key=? AND resolved_at IS NULL""",
                           ((seen, kind, key) for kind, key, _since, seen in rows))
            return len(rows)

        return _write(self.path, task)

    def resolve(self, kind, keys, now=None):
        seen = epoch(now)
        rows = [(seen, kind, str(key)) for key in keys]
        if not rows:
            return 0

        def task(db):
            return db.executemany("""UPDATE recovery_gaps SET resolved_at=?,last_seen=?
                                    WHERE kind=? AND gap_key=? AND resolved_at IS NULL""",
                                  ((seen, seen, kind, key) for seen, kind, key in rows)).rowcount

        return _write(self.path, task)

    def rank(self, kind, keys, now=None):
        """Return keys ordered by priority; unregistered keys count as brand new."""
        seen = epoch(now)
        keys = [str(key) for key in keys]
        if not keys:
            return []
        first = {}
        for group in chunks(keys):
            sql = ("SELECT gap_key,missing_since FROM recovery_gaps WHERE kind=? "
                   "AND resolved_at IS NULL AND gap_key IN (%s)" % ",".join("?" * len(group)))
            rows = _read(self.path, lambda db, group=group, sql=sql:
                         db.execute(sql, [kind, *group]).fetchall())
            first.update(dict(rows))
        return sorted(keys, key=lambda key: (-score(kind, first.get(key, seen), seen), key))

    def pending(self, kind=None, now=None):
        seen = epoch(now)
        sql = ("SELECT kind,gap_key,missing_since,last_seen,attempts FROM recovery_gaps "
               "WHERE resolved_at IS NULL" + (" AND kind=?" if kind else ""))
        args = (kind,) if kind else ()
        rows = _read(self.path, lambda db: db.execute(sql, args).fetchall())
        items = []
        for kind_name, key, since, last_seen, attempts in rows:
            age = max(0.0, seen - since)
            items.append({"kind": kind_name, "gap_key": key, "score": round(score(kind_name, since, seen), 2),
                          "age_seconds": round(age), "missing_since": since, "last_seen": last_seen,
                          "attempts": attempts})
        items.sort(key=lambda row: (-row["score"], row["gap_key"]))
        return items

    def _meta(self, key, default=""):
        row = _read(self.path, lambda db:
                    db.execute("SELECT value FROM recovery_meta WHERE key=?", (key,)).fetchone())
        return row[0] if row else default

    def _set_meta(self, key, value):
        _write(self.path, lambda db:
               db.execute("INSERT OR REPLACE INTO recovery_meta VALUES(?,?)", (key, str(value))))

    def reset_deferral(self, kind):
        self._set_meta("deferrals:" + kind, 0)

    def should_defer(self, kind, now=None):
        """True when a low-priority backfill window should yield its turn.

        Only recently observed, still-unrepaired quote gaps that survived at
        least one recovery window create pressure. After two consecutive yields
        the window runs anyway, so news/announcement/report work cannot starve.
        """
        if kind == "quote":
            return False
        seen = epoch(now)
        urgent = [row for row in self.pending("quote", seen)
                  if DEFER_AFTER_SECONDS <= row["age_seconds"] <= DEFER_UNTIL_SECONDS
                  and seen - row["last_seen"] <= RECENT_OBSERVATION_SECONDS]
        if not urgent:
            self.reset_deferral(kind)
            return False
        key = "deferrals:" + kind
        try:
            count = int(self._meta(key, "0")) + 1
        except ValueError:
            count = 1
        if count > MAX_CONSECUTIVE_DEFERRALS:
            self.reset_deferral(kind)
            return False
        self._set_meta(key, count)
        return True

    def summary(self, now=None, limit=12):
        seen = epoch(now)
        items = self.pending(now=seen)
        by_kind = {}
        for row in items:
            by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + 1
        return {
            "schema_version": 1,
            "generated_at": datetime.fromtimestamp(seen, SHANGHAI).isoformat(timespec="seconds"),
            "policy": {
                "quote_always_first": True,
                "weights": WEIGHTS,
                "age_boost": AGE_BOOST,
                "age_cap_days": AGE_CAP_SECONDS / 86400,
                "defer_after_minutes": DEFER_AFTER_SECONDS / 60,
                "max_consecutive_deferrals": MAX_CONSECUTIVE_DEFERRALS,
            },
            "open_gaps": len(items),
            "by_kind": by_kind,
            "queue": [{key: row[key] for key in ("kind", "gap_key", "score", "age_seconds")}
                      for row in items[:limit]],
        }


def ledger(state=STATE):
    """Shared accessor used by collectors and the health publisher."""
    return GapLedger(state=state)
