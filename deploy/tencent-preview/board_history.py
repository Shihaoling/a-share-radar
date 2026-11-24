"""Archive real stock observations and project complete sector rosters without page traffic."""
from contextlib import closing
from datetime import datetime, time, timedelta
import gzip
import json
import math
import re
import sys
import threading

from pipeline.calendar import SHANGHAI, is_trading_day, previous_trading_day
from constituent_live import MAX_AGE, WRITE_MINUTE, minute_sample, roster_codes, stock_observations
from intraday_chart import WRITE_CHART_POINT

BOARD = re.compile(r"[0-9]{6}R?\.(?:TI|SI)")
BATCH = re.compile(r"[0-9]{12}\.json\.gz")
SCHEMA = """
CREATE TABLE IF NOT EXISTS board_stock_minutes(day TEXT,minute TEXT,code TEXT,stamp TEXT,change REAL,direct INTEGER,
 PRIMARY KEY(day,minute,code)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS board_partial_minutes(day TEXT, code TEXT, minute TEXT, stamp TEXT, change REAL,
 quoted INTEGER, total INTEGER, PRIMARY KEY(day,code,minute)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS board_source_files(day TEXT,name TEXT,prices_saved INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(day,name)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS board_stock_bases(day TEXT,code TEXT,previous_close REAL,PRIMARY KEY(day,code)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS board_history_recovered(day TEXT,code TEXT,minute TEXT,
 PRIMARY KEY(day,code,minute)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS constituent_by_minute ON constituent_minutes(day,minute,code);
"""
WRITE_STOCK = """INSERT INTO board_stock_minutes VALUES (?,?,?,?,?,?) ON CONFLICT(day,minute,code)
 DO UPDATE SET stamp=excluded.stamp,change=excluded.change,direct=excluded.direct
 WHERE excluded.stamp>board_stock_minutes.stamp OR (excluded.stamp=board_stock_minutes.stamp AND
 excluded.direct>board_stock_minutes.direct)"""
WRITE_PARTIAL = """INSERT INTO board_partial_minutes VALUES(?,?,?,?,?,?,?)
 ON CONFLICT(day,code,minute) DO UPDATE SET stamp=excluded.stamp,change=excluded.change,
 quoted=excluded.quoted,total=excluded.total
 WHERE excluded.stamp>board_partial_minutes.stamp OR (excluded.stamp=board_partial_minutes.stamp AND
 excluded.quoted>=board_partial_minutes.quoted)"""



class BoardHistory:
    def __init__(self, store, publish):
        self.store, self.publish = store, publish
        self.day = None
        self.rosters, self.files, self.expected, self.complete = {}, {}, {}, set()
        self.scan_at = 0
        self.batch_scan_at = 0
        self.pending = set()
        self.archive_files = []
        self.bad_files = {}
        self.imported_files = set()
        self.bases = set()
        self.summary = {}
        self.summary_dirty = True
        self.history_version = None
        self.seen_quotes = {}
        self.archived_version = None
        self.snapshot = (None,{})
        self.read_lock = threading.Lock()
        with closing(store.db()) as db, db:
            if 'rebuilt' not in {r[1] for r in db.execute('PRAGMA table_info(constituent_minutes)')}:
                db.execute('ALTER TABLE constituent_minutes ADD COLUMN rebuilt INTEGER NOT NULL DEFAULT 0')
            db.executescript(SCHEMA)
            if 'prices_saved' not in {r[1] for r in db.execute('PRAGMA table_info(board_source_files)')}:
                db.execute('ALTER TABLE board_source_files ADD COLUMN prices_saved INTEGER NOT NULL DEFAULT 0')

    def quotes(self, now):
        names = ('board-stock-quotes','stock-quotes')
        versions = []
        for name in names:
            try:
                st = (self.store.out/(name+'.json')).stat()
                versions.append((st.st_ino,st.st_mtime_ns,st.st_size))
            except OSError:
                versions.append(None)
        signature = (now.date(),*versions)
        with self.read_lock:
            if self.snapshot[0] != signature:
                packets = [self.store.feed(name) for name in names]
                stocks = (r for p in packets if p.get('source','sxdata_relay') == 'sxdata_relay'
                          for r in p.get('stocks', []))
                self.snapshot = (signature,stock_observations(stocks,now,recent=False))
            return self.snapshot

    def roll_day(self, db, day):
        if self.day == day:
            return
        self.summary = {}
        for row in db.execute('''SELECT code,stamp,change,quoted,total,max(minute) FROM constituent_minutes
                                  WHERE day=? GROUP BY code''', (day,)):
            self.update_summary(row[0],row[1],row[2],row[3],row[4])
        self.summary_dirty = True
        self.bases = {r[0] for r in db.execute('SELECT code FROM board_stock_bases WHERE day=?',(day,))}
        self.imported_files = {r[0] for r in db.execute(
            'SELECT name FROM board_source_files WHERE day=? AND prices_saved=1', (day,))}
        self.pending = {r[0] for r in db.execute('SELECT DISTINCT minute FROM board_stock_minutes WHERE day=?',(day,))}
        self.archive_files, self.seen_quotes, self.bad_files = [], {}, {}
        self.archived_version = None
        self.day, self.scan_at, self.batch_scan_at = day, 0, 0
        # Keep full-market raw minutes for this trading date; retain compact sector curves for ten days.
        for table in ('board_stock_minutes','board_stock_bases','board_source_files','board_partial_minutes'):
            db.execute('DELETE FROM '+table+' WHERE day<?',(day,))
        cutoff = (datetime.fromisoformat(day).date()-timedelta(days=10)).isoformat()
        db.execute('DELETE FROM constituent_minutes WHERE day<?',(cutoff,))

    def refresh_rosters(self, now, db):
        if now.timestamp() < self.scan_at:
            return False
        self.scan_at = now.timestamp()+60
        directory = self.store.out/'sector-members'
        changed = False
        for path in directory.glob('*.json'):
            code = path.stem
            if not BOARD.fullmatch(code):
                continue
            try:
                st = path.stat()
                version = (st.st_ino,st.st_mtime_ns,st.st_size)
                if self.files.get(code) == version:
                    continue
                roster = self.store.roster(code)
                if not roster:  # A damaged projection can recover on the next scan.
                    continue
                codes = frozenset(sys.intern(c) for c in roster_codes(roster))
                ready = roster.get('status') == 'ready'
                declared = roster.get('expected_count')
                total = max(len(codes),declared) if not ready and type(declared) is int else len(codes)
                self.files[code] = version
                if (self.rosters.get(code) != codes or (code in self.complete) != ready
                        or total != self.expected.get(code)):
                    changed = True
                    if codes:
                        self.rosters[code] = codes
                        self.expected[code] = total
                    else:
                        self.rosters.pop(code,None)
                        self.expected.pop(code,None)
                    if ready:
                        self.complete.add(code)
                    else:
                        self.complete.discard(code)
            except OSError:
                continue
        if changed:
            self.pending.update(r[0] for r in db.execute('SELECT DISTINCT minute FROM board_stock_minutes WHERE day=?',(self.day,)))
        return changed

    def archive(self, db, quotes, now, *, latest=False):
        day = now.date().isoformat()
        raw = []
        basis = []
        added_bases = set()
        legacy = []
        for row in quotes.values():
            code = row['code']
            stamp = row['quote_at']
            seen = self.seen_quotes.get(code,'')
            if stamp > seen or not latest and stamp != seen:
                raw.append((day,stamp[:16],code,stamp,row['change_pct'],1))
            prior = row.get('previous_close')
            if prior and code not in self.bases and code not in added_bases:
                added_bases.add(code)
                basis.append((day,code,prior))
                # Existing intraday charts stored real stock prices but not the daily percentage.
                for old,price in db.execute("""SELECT stamp,price FROM chart_points WHERE kind='stock'
                                      AND code=? AND day=? ORDER BY minute""",(code,day)):
                    try:
                        at = datetime.fromisoformat(old)
                        if at.tzinfo is None:
                            continue
                        at = at.astimezone(SHANGHAI)
                        if (at.date() != now.date() or at > now or not math.isfinite(price) or price <= 0
                                or not ('09:30:00' <= at.strftime('%H:%M:%S') <= '11:30:00'
                                        or '13:00:00' <= at.strftime('%H:%M:%S') <= '15:00:00')):
                            continue
                    except (TypeError,ValueError,OverflowError):
                        continue
                    at = at.isoformat(timespec='seconds')
                    legacy.append((day,at[:16],code,at,(price/prior-1)*100,0))
        db.executemany(WRITE_STOCK,legacy)
        db.executemany(WRITE_STOCK,raw)
        db.executemany('INSERT OR IGNORE INTO board_stock_bases VALUES (?,?,?)',basis)
        return raw, legacy, basis

    def remember(self, data, *, latest=False):
        raw, legacy, basis = data
        self.bases.update(r[1] for r in basis)
        minutes = {r[1] for r in raw+legacy}
        # A late observation can also change a following observed minute's three-minute window.
        for minute in minutes:
            start = datetime.fromisoformat(minute)
            self.pending.update((start+timedelta(minutes=i)).isoformat(timespec='minutes') for i in range(4))
        if latest:
            self.seen_quotes.update((r[2],r[3]) for r in raw)

    def update_summary(self, code, stamp, change, count, total):
        previous = self.summary.get(code)
        row = dict(quote_at=stamp,change_pct=change,quoted_count=count,total_count=total)
        if previous != row and (not previous or stamp>previous['quote_at'] or
                                stamp==previous['quote_at'] and count>=previous['quoted_count']):
            self.summary[code] = row
            self.summary_dirty = True

    def sync_recovery(self, db, day):
        try:
            version = (day,(self.store.private/"stock-minute-recovery-report.json").stat().st_mtime_ns)
        except FileNotFoundError:
            return
        if version == self.history_version:
            return
        rows = db.execute("""SELECT c.code,c.stamp,c.change,c.quoted,c.total,max(c.minute)
            FROM board_history_recovered h JOIN constituent_minutes c
            ON c.code=h.code AND c.day=h.day AND c.minute=h.minute
            WHERE h.day=? GROUP BY c.code""",(day,))
        for code,stamp,change,count,total,_ in rows:
            self.update_summary(code,stamp,change,count,total)
        self.history_version = version

    def live_samples(self, db, quotes, now):
        cutoff = (now-MAX_AGE).isoformat(timespec='seconds')
        fresh = {c:r for c,r in quotes.items() if r['quote_at'] >= cutoff}
        samples, partial = [], []
        for code,codes in self.rosters.items():
            members = [fresh[c] for c in codes if c in fresh]
            if not members:
                continue
            total = self.expected[code]
            sample = minute_sample(code,self.day,total,members) if code in self.complete else None
            if sample:
                samples.append(sample)
            elif code not in self.summary:
                stamp = max(r['quote_at'] for r in members)
                value = math.fsum(r['change_pct'] for r in members)/len(members)
                partial.append((self.day,code,stamp[:16],stamp,value,len(members),total))
        self.save_samples(db,samples,partial)
        return samples

    def save_samples(self, db, samples, partial):
        db.executemany(WRITE_MINUTE,samples)
        db.executemany(WRITE_PARTIAL,partial)
        # Once an adequate observation exists, the full curve takes precedence for the day.
        db.executemany('DELETE FROM board_partial_minutes WHERE day=? AND code=?',
                       ((self.day,r[0]) for r in samples if r[0] not in self.summary))

    def attach_partial(self, db, code, result, now):
        day = now.date().isoformat()
        if result['points'] and result['trade_date']==day:
            return
        result['sample_points'] = [list(r) for r in db.execute("""SELECT stamp,change,quoted,total FROM
                                board_partial_minutes WHERE day=? AND code=? ORDER BY minute""",(day,code))]
        result['sample_trade_date'] = day

    def import_batches(self, now, limit=2):
        if now.timestamp() >= self.batch_scan_at:
            self.batch_scan_at = now.timestamp()+12
            directory = self.store.private/'stock-batches'/self.day
            self.archive_files = sorted(p for p in directory.glob('*.json.gz')
                                        if BATCH.fullmatch(p.name) and p.name not in self.imported_files
                                        and (p not in self.bad_files or self.bad_files[p] != p.stat().st_mtime_ns))
        for _ in range(min(limit,len(self.archive_files))):
            path = self.archive_files.pop(0)
            try:
                if self.bad_files.get(path) == path.stat().st_mtime_ns:
                    continue
                with gzip.open(path,'rb') as f:
                    packet = json.load(f)
                received = datetime.fromisoformat(packet['received_at'])
                if (packet.get('source') != 'sxdata_relay' or packet.get('trade_date') != self.day
                        or received.tzinfo is None or received > now
                        or received.astimezone(SHANGHAI).date().isoformat() != self.day
                        or not isinstance(packet.get('stocks'),list)):
                    if received.tzinfo is None or received <= now:
                        self.bad_files[path] = path.stat().st_mtime_ns
                    continue
                received = received.astimezone(SHANGHAI)
                quotes = stock_observations(packet['stocks'],received,recent=False,per_minute=True)
                with closing(self.store.db()) as db, db:
                    receipt = db.execute('SELECT prices_saved FROM board_source_files WHERE day=? AND name=?',
                                         (self.day,path.name)).fetchone()
                    changes = self.archive(db,quotes,received) if receipt is None else None
                    # Keep the original provider price, not a price inferred from the change.
                    db.executemany(WRITE_CHART_POINT, (
                        ('stock', row['code'], self.day, row['quote_at'][:16], row['quote_at'], row['price'])
                        for row in quotes.values()
                    ))
                    db.execute('INSERT OR REPLACE INTO board_source_files VALUES(?,?,1)',(self.day,path.name))
                if changes is not None:
                    self.remember(changes)
                self.imported_files.add(path.name)
            except (EOFError,ValueError,KeyError,TypeError,AttributeError,gzip.BadGzipFile):
                try:
                    self.bad_files[path] = path.stat().st_mtime_ns
                except OSError:
                    pass
            except OSError:
                continue

    def replay(self, db, minute):
        # Select the latest real observation per stock up to the end of this source minute.
        # Every emitted timestamp belongs to a stock that actually traded in this minute.
        if not db.execute('SELECT 1 FROM board_stock_minutes WHERE day=? AND minute=? LIMIT 1', (self.day,minute)).fetchone():
            return []
        start = (datetime.fromisoformat(minute)-MAX_AGE).isoformat(timespec='minutes')
        lookup = {}
        changed = set()
        for code,stamp,pct,stored_minute in db.execute('''SELECT code,stamp,change,minute FROM board_stock_minutes
                WHERE day=? AND minute BETWEEN ? AND ? ORDER BY minute,code''',(self.day,start,minute)):
            lookup[code] = (stamp,pct)
            if stored_minute == minute:
                changed.add(code)
        if not changed:
            return []
        samples, partial = [], []
        cutoffs = {}
        for code,codes in self.rosters.items():
            if codes.isdisjoint(changed):
                continue
            relevant = [lookup[c] for c in codes if c in lookup]
            total = self.expected[code]
            if code in self.summary and len(relevant)*5 < total*4:
                continue
            newest = max(x[0] for x in relevant)
            if newest not in cutoffs:
                cutoffs[newest] = (datetime.fromisoformat(newest)-MAX_AGE).isoformat(timespec='seconds')
            values = [q for q in relevant if q[0]>=cutoffs[newest]]
            qualified = code in self.complete and len(values)*5 >= total*4
            if qualified or code not in self.summary:
                change = math.fsum(q[1] for q in values)/len(values)
                if qualified:
                    samples.append((code,self.day,minute,newest,min(q[0] for q in values),
                                    change,len(values),total,1))
                else:
                    partial.append((self.day,code,minute,newest,change,len(values),total))
        self.save_samples(db,samples,partial)
        return samples

    def capture(self, now, replay_limit=None):
        today = now.date()
        clock = now.time()
        if is_trading_day(today) is not True or clock < time(9, 30):
            day = previous_trading_day(today)
            if day is None:
                return
            day = day.isoformat()
        else:
            day = today.isoformat()
        replay_only = day != today.isoformat() or clock > time(15, 5)
        version, quotes = (None, {}) if replay_only else self.quotes(now)
        if replay_limit is None:
            replay_limit = 18 if replay_only else 6
        with closing(self.store.db()) as db, db:
            self.roll_day(db,day)
            self.sync_recovery(db,day)
            rosters_changed = self.refresh_rosters(now,db)
            if not replay_only and (rosters_changed or version != self.archived_version):
                changes = self.archive(db,quotes,now,latest=True)
                samples = self.live_samples(db,quotes,now)
                db.commit()
                self.remember(changes,latest=True)
                self.archived_version = version
                for r in samples:
                    self.update_summary(r[0],r[3],r[5],r[6],r[7])
        self.import_batches(now, limit=12 if replay_only else 2)
        # Read and write one historical minute per transaction. Web readers continue on WAL snapshots.
        due = sorted(m for m in self.pending if m <= now.isoformat(timespec='minutes')[:16])
        for minute in due[:replay_limit]:
            with closing(self.store.db()) as db, db:
                self.replay(db,minute)
                # A replay never overwrites a directly calculated observation.
                for row in db.execute('SELECT code,stamp,change,quoted,total FROM constituent_minutes WHERE day=? AND minute=?',
                                      (day,minute)):
                    self.update_summary(*row)
            self.pending.discard(minute)
        if self.summary_dirty:
            self.publish(self.store.out/'board-live-summary.json',dict(schema_version=1,source='sxdata_board_summary',
                                                                      trade_date=day,items=self.summary))
            self.summary_dirty = False
