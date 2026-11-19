"""Single-asset price and risk curves on the same completed sessions as the research cards."""
from pipeline.calendar import is_trading_day
from research_factors import _bars, _daily, _day, _number, _returns


def _display_levels(days, own, returns, stock):
    # Work from the last observed raw close of each independent segment.
    # A missing date or reference starts a new segment without bridging a suspension or split.
    levels = [None] * len(days)
    for i in reversed(range(len(days))):
        obs = own.get(days[i])
        if not obs:
            continue
        linked = stock and i+1 < len(days) and levels[i+1] is not None and returns[i] is not None
        level = levels[i+1]/(1+returns[i]) if linked else obs[0]
        if _number(level, True):
            levels[i] = level
    return levels


def _drawdown(rows):
    if len(rows) != 60 or any(r["close"] is None for r in rows) or any(r["gap"] for r in rows[1:]):
        return None
    peak, best, pair = rows[0], 0.0, None
    for row in rows[1:]:
        if row["close"] > peak["close"]:
            peak = row
        decline = (1-row["close"]/peak["close"])*100
        if decline > best:
            best = decline
            pair = {"peak_day":peak["day"], "peak_price":peak["close"],
                    "trough_day":row["day"], "trough_price":row["close"], "pct":decline}
    return pair


def _window_bars(parsed):
    return {day:{"close":bar[0], "pre_close":bar[1], "adjustment":bar[2]}
            for day,bar in parsed.items() if bar}


def build_daily_chart(kind, bars, benchmark_bars, sessions):
    """Return at most 60 session slots, with a 60-session warm-up for paired returns."""
    if kind not in {"stock","index","sector"}:
        raise ValueError("kind must be stock, index or sector")
    days = sorted({day for value in sessions if (day:=_day(value)) and is_trading_day(day) is True})[-120:]
    stock = kind == "stock"
    own, benchmark = _bars(bars,days), _bars(benchmark_bars,days)
    returns = _returns(days,own,stock)
    levels = _display_levels(days,own,returns,stock)
    prepared, base = _window_bars(own), _window_bars(benchmark)
    anchors = {}
    rows = []
    for i in range(max(0,len(days)-60),len(days)):
        day = days[i]
        close = levels[i]
        gap = i == 0 or close is None or returns[i-1] is None
        row = {"day": day, "close":close, "raw_close":own[day][0] if own.get(day) else None,
               "gap":gap, "ma20":None, "momentum20":None, "volatility20":None, "beta60":None, "drawdown60":None}
        if close is not None:
            window = days[max(0,i-60):i+1]
            factors = {r["key"]:r["value"] for r in _daily(kind,prepared,base,window)}
            bias = factors["ma20_bias_pct"]
            if bias is not None and bias > -100:
                average = close/(1+bias/100)
                row["ma20"] = average if _number(average, True) else None
            row.update(momentum20=factors["momentum20_pct"], volatility20=factors["volatility20_pct"],
                       beta60=factors["beta60"], drawdown60=factors["drawdown60_pct"])
            if i == len(days)-1 or returns[i] is None:
                anchors[day] = close
        rows.append(row)
    as_of = next((r["day"] for r in reversed(rows) if r["close"] is not None),None)
    return {"rows":rows, "as_of":as_of, "through":days[-1] if days else None, "drawdown":_drawdown(rows),
            "basis":"reference_linked" if stock else "index_close",
            "basis_label":"按真实参考昨收逐日链接，各连续段锚定于末次收盘价" if stock else "指数实际收盘点位",
            "anchors":anchors if stock else {}}
