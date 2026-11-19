"""Chart only observed prices and source-reported heat-list changes, with their original timestamps."""
from bisect import bisect_right
from contextlib import closing
from datetime import datetime, timedelta
import math
import json

from pipeline.calendar import SHANGHAI


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def observation(stamp, value, day, *, percentage=False):
    if not finite(value) or (value < -100 if percentage else value <= 0):
        return None
    try:
        t = datetime.fromisoformat(stamp)
        if t.tzinfo is None:
            return None
        t = t.astimezone(SHANGHAI)
    except (ValueError, TypeError, OverflowError):
        return None
    return (t.isoformat(timespec="seconds"), value) if t.date().isoformat() == day else None


def archive_heat(path, rows, known, now, publish):
    series = {}
    day = now.date().isoformat()
    if path.exists():
        try:
            previous = json.loads(path.read_bytes())
        except (OSError, ValueError):
            previous = {}
        if not isinstance(previous, dict) or not isinstance(previous.get("series", {}), dict):
            previous = {}
        if previous.get("day") == day:
            series = previous.get("series", {})
        elif str(previous.get("day", "")) > day:
            return
    for r in rows:
        code = r.get("ts_code")
        if code not in known or r.get("data_type") != "概念板块" or str(r.get("trade_date")).replace("-","") != day.replace("-",""):
            continue
        try:
            t = datetime.fromisoformat(r["rank_time"])
        except (ValueError, TypeError, KeyError):
            continue
        t = t.astimezone(SHANGHAI) if t.tzinfo else t.replace(tzinfo=SHANGHAI)
        point = observation(t.isoformat(), r.get("pct_change"), day, percentage=True)
        if point and t <= now:
            series.setdefault(code, {})[point[0]] = point[1]
    if series:
        publish(path, {"day":day, "series":series})


def relative_changes(points, prior, percentage, benchmark, bench_points, day):
    """Pair with the last observed benchmark point at or before the asset timestamp."""
    bench_prior = benchmark.get("previous_close")
    if (benchmark.get("quote_date") != day or not finite(bench_prior) or bench_prior <= 0
            or not percentage and (not finite(prior) or prior <= 0)):
        return []
    sample = {}
    for row in bench_points:
        if not isinstance(row, (list,tuple)) or len(row)<2:
            continue
        point = observation(row[0],row[1],day)
        if point:
            sample[datetime.fromisoformat(point[0]).timestamp()] = point[1]
    stamps = sorted(sample)
    result = []
    for stamp, price in points:
        at = datetime.fromisoformat(stamp).timestamp()
        idx = bisect_right(stamps,at)-1
        if idx>=0 and at-stamps[idx]<=90:
            change = price if percentage else (price/prior-1)*100
            result.append([stamp,change-(sample[stamps[idx]]/bench_prior-1)*100])
    return result


class IntradayCharts:
    def __init__(self, store):
        self.store = store
        self.seen = {}
        self.last_day = None
        with closing(store.db()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS chart_points(kind TEXT,code TEXT,day TEXT,minute TEXT,
                    stamp TEXT,price REAL,PRIMARY KEY(kind,code,day,minute)) WITHOUT ROWID""")

    def capture(self, now):
        day = now.date().isoformat()
        path = self.store.out / "stock-quotes.json"
        try:
            version = path.stat().st_mtime_ns
        except OSError:
            return
        if self.seen.get(path) == version and day == self.last_day:
            return
        data = self.store.feed("stock-quotes")
        rows = []
        for kind, group in (("stock","stocks"),("sector","board_estimates")):
            for r in data.get(group, []):
                source_day = r.get("quote_date")
                price = observation(r.get("quote_timestamp"), r.get("price"), source_day)
                if not price or source_day != day or not isinstance(r.get("id"), str):
                    continue
                stamp, value = price
                rows.append((kind,r["id"],day,stamp[:16],stamp,value))
        with closing(self.store.db()) as db, db:
            db.executemany("""INSERT INTO chart_points VALUES(?,?,?,?,?,?)
                ON CONFLICT(kind,code,day,minute) DO UPDATE SET stamp=excluded.stamp,price=excluded.price
                WHERE excluded.stamp>=chart_points.stamp""",rows)
            if self.last_day != day:
                cutoff = (now.date()-timedelta(days=10)).isoformat()
                db.execute("DELETE FROM chart_points WHERE day<?",(cutoff,))
        self.last_day = day
        self.seen[path] = version

    def render(self, db, kind, code, quote, now):
        day = quote.get("quote_date") or now.date().isoformat()
        estimate = kind == "sector" and code.endswith(".SI")
        heat = kind == "sector" and code.endswith(".TI")
        points = []
        if kind == "stock" or estimate:
            points.extend(db.execute("""SELECT stamp,price FROM chart_points WHERE kind=? AND code=? AND day=?
                            ORDER BY minute""",(kind,code,day)))
        elif heat:
            packet = self.store.feed("sector-heat-samples")
            if packet.get("day") == day:
                points.extend(packet.get("series",{}).get(code,{}).items())
        else:
            ident = code[-2:].lower()+code[:6] if kind=="index" else code
            for feed in ("today-intraday","minute-recovery"):
                packet = self.store.feed(feed)
                if packet.get("trade_date")==day:
                    points.extend(packet.get("series",{}).get(ident, []))
            if kind == "sector":
                path = self.store.out / "sector-intraday" / (code+".json")
                try:
                    packet=json.loads(path.read_bytes())
                    if packet.get("id")==code and packet.get("trade_date")==day:
                        points.extend(packet.get("points",[]))
                except (OSError,ValueError,AttributeError):
                    pass
        if quote:
            points.append((quote.get("quote_timestamp"),quote.get("change_pct" if heat else "price")))
        latest = {}
        for row in points:
            if not hasattr(row,"__len__") or len(row)<2:
                continue
            point = observation(row[0],row[1],day,percentage=heat)
            if point and (point[0][:16] not in latest or point[0] >= latest[point[0][:16]][0]):
                latest[point[0][:16]]=point
        out=dict(day=day, points=[list(latest[k]) for k in sorted(latest)], measure="change_pct" if heat else "price",
                 basis="概念热榜涨跌幅快照" if heat else "权重股估算点位" if estimate else "中转站实际行情",
                 max_gap_minutes=35 if heat else 3)
        for field in ("previous_close","open","high","low"):
            out[field] = quote[field] if not heat and finite(quote.get(field)) and quote[field]>0 else None
        benchmark = self.store.live("index","000300.SH")
        bench_points = [(benchmark.get("quote_timestamp"),benchmark.get("price"))]
        for feed in ("today-intraday","minute-recovery"):
            packet = self.store.feed(feed)
            if packet.get("trade_date")==day:
                bench_points.extend(packet.get("series",{}).get("sh000300",[]))
        out["relative"] = relative_changes(out["points"],out["previous_close"],heat,benchmark,bench_points,day)
        return out
