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
                if source_day != day or not isinstance(r.get("id"), str):
                    continue
                series = [(kind, r.get("price"), False)]
                if kind == "sector" and r.get("basis") == "constituent_estimate":
                    series.append(("sector_change", r.get("change_pct"), True))
                for key, value, percent in series:
                    point = observation(r.get("quote_timestamp"), value, source_day, percentage=percent)
                    if point and point[0] <= now.isoformat(timespec='seconds'):
                        rows.append((key,r["id"],day,point[0][:16],*point))
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
        today = now.date().isoformat()
        quote_day = quote.get("quote_date")
        days = [today]
        if isinstance(quote_day, str) and quote_day < today:
            days.append(quote_day)
        estimate = kind == "sector" and code.endswith(".SI")
        heat = kind == "sector" and code.endswith(".TI")
        prices = {day: [] for day in days}
        changes = {day: [] for day in days}
        if kind == "stock" or estimate:
            for stored_kind, target in [(kind,prices)] + ([("sector_change",changes)] if estimate else []):
                for day in days:
                    target[day].extend(db.execute("""SELECT stamp,price FROM chart_points
                                   WHERE kind=? AND code=? AND day=? ORDER BY minute""",(stored_kind,code,day)))
        elif heat:
            packet = self.store.feed("sector-heat-samples")
            if packet.get("day") in days:
                changes[packet["day"]].extend(packet.get("series",{}).get(code,{}).items())
        else:
            ident = code[-2:].lower()+code[:6] if kind=="index" else code
            for feed in ("today-intraday","minute-recovery"):
                packet = self.store.feed(feed)
                if packet.get("trade_date") in days:
                    prices[packet["trade_date"]].extend(packet.get("series",{}).get(ident, []))
            if kind == "sector":
                path = self.store.out / "sector-intraday" / (code+".json")
                try:
                    packet=json.loads(path.read_bytes())
                    if packet.get("id")==code and packet.get("trade_date") in days:
                        prices[packet["trade_date"]].extend(packet.get("points",[]))
                except (OSError,ValueError,AttributeError):
                    pass
        if quote_day in days:
            if not heat:
                prices[quote_day].append((quote.get("quote_timestamp"),quote.get("price")))
            if heat or estimate:
                changes[quote_day].append((quote.get("quote_timestamp"),quote.get("change_pct")))

        # Prefer any verified current-session series over the prior day's last displayed quote.
        # Percent-only estimates and genuine point levels keep separate units throughout.
        latest = {}
        cutoff = now.astimezone(SHANGHAI).isoformat(timespec="seconds")
        for day in days:
            for percentage, points in ((False,prices[day]),(True,changes[day])):
                for row in points:
                    if not hasattr(row,"__len__") or len(row)<2:
                        continue
                    point = observation(row[0],row[1],day,percentage=percentage)
                    if point and point[0]<=cutoff and (point[0][:16] not in latest or point[0] >= latest[point[0][:16]][0]):
                        latest[point[0][:16]] = point
                if latest:
                    break
            if latest:
                break
        if not latest:
            percentage = heat
        weighted_change = estimate and percentage
        out=dict(day=day, points=[list(latest[k]) for k in sorted(latest)],
                 measure="change_pct" if heat else "weighted_change_pct" if weighted_change else "price",
                 basis="概念热榜涨跌幅快照" if heat else "权重股估算涨跌幅" if weighted_change else
                       "权重股估算点位" if estimate else "中转站实际行情", max_gap_minutes=35 if heat else 3)
        for field in ("previous_close","open","high","low"):
            out[field] = quote[field] if not percentage and quote_day == day and finite(quote.get(field)) and quote[field]>0 else None
        benchmark = self.store.live("index","000300.SH")
        bench_points = [(benchmark.get("quote_timestamp"),benchmark.get("price"))]
        for feed in ("today-intraday","minute-recovery"):
            packet = self.store.feed(feed)
            if packet.get("trade_date")==day:
                bench_points.extend(packet.get("series",{}).get("sh000300",[]))
        out["relative"] = relative_changes(out["points"],out["previous_close"],percentage,benchmark,bench_points,day)
        return out
