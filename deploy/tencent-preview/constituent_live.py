"""Intraday equal-weight moves from source-timed, recently observed member stock quotes."""
from datetime import datetime, time, timedelta
import math
import re

from pipeline.calendar import SHANGHAI, is_trading_day

MAX_AGE = timedelta(seconds=180)
A_SHARE = re.compile(r'[0-9]{6}\.(?:SH|SZ|BJ)')
SCHEMA = '''CREATE TABLE IF NOT EXISTS constituent_minutes(
 code TEXT, day TEXT, minute TEXT, stamp TEXT, oldest TEXT, change REAL, quoted INTEGER, total INTEGER, rebuilt INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(code,day,minute)) WITHOUT ROWID'''


def roster_codes(roster):
    return {r['code'] for r in roster.get('members', []) if isinstance(r, dict)
            and isinstance(r.get('code'), str) and r['code']}


def stock_observations(stocks, now, *, recent=True, per_minute=False):
    observed = {}
    day = now.date().isoformat()
    if not is_trading_day(now.date()):
        return observed
    for row in stocks:
        if not isinstance(row, dict):
            continue
        code = row.get('id')
        if not isinstance(code, str) or not A_SHARE.fullmatch(code):
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
            if stamp.date() != now.date() or stamp > now or recent and now - stamp > MAX_AGE:
                continue
            price, change = row['price'], row['change_pct']
            if any(type(x) not in (float, int) or not math.isfinite(x) for x in (price, change)):
                continue
            if price <= 0 or change <= -100:
                continue
        except (TypeError, ValueError, KeyError, OverflowError):
            continue
        at = stamp.isoformat(timespec='seconds')
        key = (code,at[:16]) if per_minute else code
        if key not in observed or at > observed[key]['quote_at']:
            observed[key] = dict(code=code, price=price, change_pct=change, quote_at=at)
            prior = row.get('previous_close')
            if (type(prior) in (float, int) and math.isfinite(prior) and prior > 0 and
                    math.isclose((price/prior-1)*100, change, abs_tol=0.000001)):
                observed[key]['previous_close'] = prior
    return observed


def observed_members(roster, stocks, now):
    codes = roster_codes(roster)
    return codes, [r for c,r in stock_observations(stocks, now).items() if c in codes]


WRITE_MINUTE = """INSERT INTO constituent_minutes VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(code,day,minute) DO UPDATE SET stamp=excluded.stamp,oldest=excluded.oldest,
        change=excluded.change,quoted=excluded.quoted,total=excluded.total,rebuilt=excluded.rebuilt
        WHERE excluded.rebuilt<=constituent_minutes.rebuilt AND (excluded.stamp>constituent_minutes.stamp OR
          (excluded.stamp=constituent_minutes.stamp AND excluded.quoted>=constituent_minutes.quoted))"""


def minute_sample(code, day, total, members, *, rebuilt=False):
    count = len(members)
    if not count or count*5 < total*4:
        return None
    times = [r['quote_at'] for r in members]
    latest = max(times)
    return (code, day, latest[:16], latest, min(times), math.fsum(r['change_pct'] for r in members)/count,
            count, total, int(rebuilt))


def summarize(db, code, roster, stocks, now, *, include_points=True, normalized=False):
    day = now.date().isoformat()
    codes = roster_codes(roster)
    if normalized:
        cutoff = (now-MAX_AGE).isoformat(timespec='seconds')
        members = [stocks[c] for c in codes if c in stocks and stocks[c]['quote_at'] >= cutoff]
    else:
        members = [r for c,r in stock_observations(stocks, now).items() if c in codes]
    total, count = len(codes), len(members)
    complete = roster.get('status') == 'ready'
    if not complete and type(roster.get('expected_count')) is int:
        total = max(total,roster['expected_count'])
    required = math.ceil(total * .8)
    sample = minute_sample(code, day, total, members) if complete else None
    qualifies = sample is not None
    latest = max((r['quote_at'] for r in members), default=None)
    oldest = min((r['quote_at'] for r in members), default=None)
    if qualifies:
        db.execute(WRITE_MINUTE, sample)
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
