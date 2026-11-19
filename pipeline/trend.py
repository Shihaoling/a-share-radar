"""Five descriptive trend states derived from completed trading sessions."""

import math
from datetime import time
from statistics import median

from pipeline.calendar import previous_trading_day, shanghai_now

WINDOWS = (5, 10, 20)
DEFAULT_WINDOW = 10
BASELINE = 40
MIN_BASELINE = 35
TREND_SCORE = 0.75
STRONG_SCORE = 1.75
TREND_EFFICIENCY = 0.20
STRONG_EFFICIENCY = 0.40
STRONG_DIRECTION_SHARE = 0.60
LEADER_COUNT = 3
MIN_CAP_COVERAGE = 0.90


def positive(value):
    return type(value) in (int, float) and value > 0 and math.isfinite(value)


def daily_source(bar):
    return bar.get("complete") is not False and (bar.get("provider") or bar.get("source")) in {
        "stock_api", "index_daily", "sw_daily", "ths_daily", "sxdata_daily", "daily", "equity_cache",
    }


def completed_session(now):
    now = shanghai_now(now)
    return previous_trading_day(now.date(), include_today=now.time() >= time(16))


def session_dates(through, count=BASELINE + max(WINDOWS) + 6):
    days = []
    while through is not None and len(days) < count:
        days.append(through.isoformat())
        through = previous_trading_day(through)
    return days[::-1]


def daily_returns(bars, sessions, *, stock=False):
    """Return aligned log moves. A missing or suspended session stays missing."""
    closes = {bar["date"]: bar for bar in bars if positive(bar.get("close"))}
    returns = [None]
    for previous, day in zip(sessions, sessions[1:]):
        bar = closes.get(day, {})
        prior = bar.get("pre_close") if stock else closes.get(previous, {}).get("close")
        if previous in closes and positive(prior) and positive(bar.get("close")):
            returns.append(math.log(bar["close"]) - math.log(prior))
        else:
            returns.append(None)
    return returns


def classify_window(returns, sessions):
    """Use a typical volatility scale from sessions preceding this window."""
    size = len(sessions) - 1
    moves = returns[-size:]
    if len(moves) != size or any(move is None for move in moves):
        return {"reason": "missing_window"}
    total = math.fsum(moves)
    travel = math.fsum(map(abs, moves))
    result = dict(start_date=sessions[0], return_pct=round(math.expm1(total) * 100, 4),
                  efficiency=round(abs(total) / travel, 4) if travel else 0.0)
    prior = [abs(move) for move in returns[-size-BASELINE:-size] if move is not None]
    if len(prior) < MIN_BASELINE:
        return {**result, "reason": "missing_baseline"}
    scale = median(prior) / 0.6744897501960817
    if scale == 0 and travel:
        return {**result, "reason": "zero_baseline"}
    score = total / (scale * math.sqrt(size)) if scale else 0.0
    direction = (total > 0) - (total < 0)
    share = sum(move * direction > 0 for move in moves) / size
    efficiency = abs(total) / travel if travel else 0.0
    if abs(score) >= STRONG_SCORE and efficiency >= STRONG_EFFICIENCY and share >= STRONG_DIRECTION_SHARE:
        level = 2 * direction
    elif abs(score) >= TREND_SCORE and efficiency >= TREND_EFFICIENCY:
        level = direction
    else:
        level = 0
    return {**result, "state": level, "score": round(score, 4), "direction_share": round(share, 4)}


def observe(bars, sessions, *, stock=False):
    if not sessions:
        return {"as_of": None, "windows": {str(n): {"reason": "calendar"} for n in WINDOWS}}
    days = set(sessions)
    eligible = [bar for bar in bars if bar.get("date") in days and daily_source(bar) and positive(bar.get("close"))]
    latest = max((bar["date"] for bar in eligible), default=None)
    usable = sessions[:sessions.index(latest) + 1] if latest else []
    returns = daily_returns(eligible, usable, stock=stock)
    windows = {str(n): classify_window(returns, usable[-n-1:]) if len(usable) > n else
               {"reason": "missing_window"} for n in WINDOWS}
    return {"as_of": latest, "windows": windows}


def rank_leaders(members, caps, *, weights=False, complete):
    """Rank a single complete membership list on one official reference date."""
    if not complete or not members:
        return [], "membership_pending", 0
    codes = {row["code"]: row for row in members if row.get("code")}
    if not codes:
        return [], "membership_pending", 0
    ranked = [(code, codes[code].get("weight_pct") if weights else caps.get(code)) for code in codes]
    ranked = [(code, value) for code, value in ranked if positive(value)]
    coverage = len(ranked) / len(codes)
    if coverage < (1 if weights else MIN_CAP_COVERAGE):
        return [], "ranking_pending", round(coverage, 4)
    ranked.sort(key=lambda pair: (-pair[1], pair[0]))
    return [{"id": code, "name": codes[code].get("name") or code, "rank_value": value}
            for code, value in ranked[:LEADER_COUNT]], "index_weight" if weights else "total_market_cap", round(coverage, 4)


def trend_snapshot(market, boards, sessions, stock_bars, now):
    """Serialize shared results. The caller supplies official daily inputs and leader ranks."""
    stocks = {}
    for board in boards:
        for member in board.get("leaders", []):
            code = member["id"]
            if code not in stocks:
                stocks[code] = observe(stock_bars.get(code, []), sessions, stock=True)

    def rows(entities):
        return [{**{key: value for key, value in item.items() if key != "bars"},
                 **observe(item.get("bars", []), sessions)} for item in entities]

    return dict(schema_version=1, source="trend_observations", method="closing_trend_v1",
                generated_at=now.isoformat(timespec="seconds"), expected_date=sessions[-1] if sessions else None,
                market=rows(market), boards=rows(boards), stocks=stocks)
