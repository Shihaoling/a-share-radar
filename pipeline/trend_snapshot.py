"""Publish the static website's trend observations from the existing daily cache."""

from collections import defaultdict
from pathlib import Path

from pipeline.calendar import shanghai_now
from pipeline.equity_data import _read_batch
from pipeline.run import atomic_json, load
from pipeline.trend import completed_session, rank_leaders, session_dates, trend_snapshot

ROOT = Path(__file__).resolve().parents[1]


def dashboard_entities(dashboard, expected_date):
    market = [{"id": item["id"], "name": item["name"], "bars": item.get("history", [])}
              for item in dashboard.get("market", [])]
    boards = []
    for item in dashboard.get("sectors", []):
        if item.get("taxonomy") != "SW2021":
            continue
        members = [dict(row, code=row["ts_code"]) for row in item.get("constituents", []) if row.get("ts_code")]
        cap_day = item.get("rank_date")
        caps = {row["code"]: row.get("market_cap") for row in members
                if cap_day and cap_day <= (expected_date or "") and row.get("market_cap_date") == cap_day}
        leaders, basis, coverage = rank_leaders(members, caps, complete=item.get("membership_complete") is True)
        boards.append(dict(id=item["id"], name=item["name"], group="sw", bars=item.get("history", []),
                           leaders=leaders, rank_basis=basis, rank_date=cap_day if leaders else None,
                           rank_coverage=coverage))
    return market, boards


def cached_stocks(history, codes, sessions):
    stocks = defaultdict(list)
    if not codes:
        return stocks
    for day in sessions:
        path = history / f"{day.replace('-', '')}.json.gz"
        batch = _read_batch(path)
        if not batch or batch.get("trade_date") != day:
            continue
        for row in batch["rows"]:
            if row.get("ts_code") in codes and row.get("date") == day:
                stocks[row["ts_code"]].append({**row, "source": "equity_cache"})
    return stocks


def main():
    now = shanghai_now()
    sessions = session_dates(completed_session(now))
    output = ROOT / "web/data/trends.json"
    dashboard = load(output.parent / "dashboard.json", {})
    market, boards = dashboard_entities(dashboard, sessions[-1] if sessions else None)
    codes = {row["id"] for board in boards for row in board["leaders"]}
    stocks = cached_stocks(ROOT / "data/equity/daily", codes, sessions)
    packet = trend_snapshot(market, boards, sessions, stocks, now)
    old = load(output, {})
    if any(old.get(key) != value for key, value in packet.items() if key != "generated_at"):
        atomic_json(output, packet)


if __name__ == "__main__":
    main()
