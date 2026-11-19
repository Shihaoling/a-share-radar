"""Intraday equal-weight moves from source-timed, recently observed member stock quotes."""
from datetime import datetime, time, timedelta
import math
import re

from pipeline.calendar import SHANGHAI, is_trading_day

MAX_AGE = timedelta(seconds=180)
A_SHARE = re.compile(r'[0-9]{6}\.(?:SH|SZ|BJ)')
SCHEMA = '''CREATE TABLE IF NOT EXISTS constituent_minutes(
 code TEXT, day TEXT, minute TEXT, stamp TEXT, oldest TEXT, change REAL, quoted INTEGER, total INTEGER,
 PRIMARY KEY(code,day,minute)) WITHOUT ROWID'''


def observed_members(roster, stocks, now):
    codes = {r['code'] for r in roster.get('members', []) if isinstance(r, dict)
             and isinstance(r.get('code'), str) and r['code']}
    fresh = {}
    day = now.date().isoformat()
    if is_trading_day(now.date()):
        for row in stocks:
            code = row.get('id')
            if not isinstance(code, str) or code not in codes or not A_SHARE.fullmatch(code):
                continue
            if row.get('provider') != 'stock_api' or row.get('quote_date') != day:
                continue
            if row.get('timestamp_quality') not in ('source_reported', 'exchange_timestamp'):
                continue
            try:
                stamp = datetime.fromisoformat(row['quote_timestamp'])
                if stamp.tzinfo is None:
                    continue
                stamp = stamp.astimezone(SHANGHAI)
                clock = stamp.time()
                if not (time(9, 30) <= clock <= time(11, 30) or time(13) <= clock <= time(15)):
                    continue
                if stamp.date() != now.date() or not timedelta(0) <= now - stamp <= MAX_AGE:
                    continue
                price, change = row['price'], row['change_pct']
                if any(type(x) not in (float, int) or not math.isfinite(x) for x in (price, change)):
                    continue
                if price <= 0 or change <= -100:
                    continue
            except (TypeError, ValueError, KeyError, OverflowError):
                continue
            at = stamp.isoformat(timespec='seconds')
            if code not in fresh or at > fresh[code]['quote_at']:
                fresh[code] = dict(code=code, price=price, change_pct=change, quote_at=at)
    return codes, list(fresh.values())


def summarize(db, code, roster, stocks, now, *, include_points=True):
    day = now.date().isoformat()
    codes, members = observed_members(roster, stocks, now)
    total, count = len(codes), len(members)
    required = math.ceil(total * .8)
    complete = roster.get('status') == 'ready'
    qualifies = complete and count > 0 and count >= required
    latest = max((r['quote_at'] for r in members), default=None)
    oldest = min((r['quote_at'] for r in members), default=None)
    if qualifies:
        value = math.fsum(r['change_pct'] for r in members) / count
        db.execute('''INSERT INTO constituent_minutes VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(code,day,minute) DO UPDATE SET stamp=excluded.stamp,oldest=excluded.oldest,
                    change=excluded.change,quoted=excluded.quoted,total=excluded.total
                    WHERE excluded.stamp>constituent_minutes.stamp OR (excluded.stamp=constituent_minutes.stamp
                    AND excluded.quoted>=constituent_minutes.quoted)''',
                   (code,day,latest[:16],latest,oldest,value,count,total))
    if not include_points:
        return
    last = db.execute('''SELECT day,stamp,oldest,change,quoted,total FROM constituent_minutes
                       WHERE code=? AND day<=? ORDER BY day DESC,minute DESC LIMIT 1''', (code,day)).fetchone()
    result = dict(status='partial' if count else 'waiting', trade_date=day, quote_at=latest, oldest_at=oldest,
                  change_pct=None, total_count=total, quoted_count=count, current_quoted_count=count,
                  required_count=required, roster_complete=complete, members=members, points=[])
    if last:
        current = qualifies and last[1] == latest and last[4] == count and last[5] == total
        result.update(status='ready' if current else 'stale', trade_date=last[0],
                      quote_at=last[1], oldest_at=last[2], change_pct=last[3], quoted_count=last[4],
                      total_count=last[5], required_count=math.ceil(last[5]*.8))
        result['points'] = [list(row) for row in db.execute('''SELECT stamp,change FROM constituent_minutes
                            WHERE code=? AND day=? ORDER BY minute''', (code,last[0]))]
    return result
