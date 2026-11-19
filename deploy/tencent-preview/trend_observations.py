"""Project the trend room from the existing primary daily research cache."""

from collections import defaultdict
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from factor_history_jobs import EQUITY
from pipeline.calendar import shanghai_now
from pipeline.trend import completed_session, rank_leaders, session_dates, trend_snapshot
from relay_common import STATE
from relay_quotes import publish

PUBLIC = Path("/srv/ashare-radar-preview/public/data")


def read(path):
    try:
        value = json.loads(path.read_bytes())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def leader_inputs(roster, cap_history, sessions, now):
    members = [row for row in roster.get("members", []) if EQUITY.fullmatch(str(row.get("code", "")))]
    as_of = str(roster.get("as_of") or "")[:10]
    checked = str(roster.get("checked_at") or "")[:10]
    membership_date = as_of or checked
    if (not sessions or not membership_date or membership_date > now.date().isoformat()
            or roster.get("status") != "ready"):
        return dict(leaders=[], rank_basis="membership_pending", rank_date=None, rank_coverage=0)
    if roster.get("method") == "index_weight":
        leaders, basis, coverage = rank_leaders(members, {}, weights=True, complete=bool(as_of and as_of <= sessions[-1]))
        return dict(leaders=leaders, rank_basis=basis, rank_date=as_of if leaders else None,
                    rank_coverage=coverage, membership_date=membership_date)
    coverage = 0
    for day in sorted(set(cap_history).intersection(sessions[-5:]), reverse=True):
        leaders, basis, ratio = rank_leaders(members, cap_history[day], complete=True)
        if leaders:
            return dict(leaders=leaders, rank_basis=basis, rank_date=day, rank_coverage=ratio,
                        membership_date=membership_date)
        coverage = max(coverage, ratio)
    return dict(leaders=[], rank_basis="ranking_pending", rank_date=None, rank_coverage=coverage)


def snapshot(state, public, now):
    out = state / "quote-output"
    exchange = read(public / "sector-catalog.json").get("items", [])
    ths = read(public / "ths-sector-catalog.json")
    groups = [("exchange", exchange), ("sw", ths.get("sw_items", [])), ("ths", ths.get("items", []))]
    sessions = session_dates(completed_session(now))
    cap_history = read(out / "trend-stock-caps.json")
    boards = []
    for group, entities in groups:
        for item in entities:
            code = item["code"]
            roster = read(out / "sector-members" / (code + ".json"))
            boards.append({"id": code, "name": item["name"], "group": group, "kind": item.get("kind"),
                           **leader_inputs(roster, cap_history, sessions, now)})

    codes = sorted({member["id"] for board in boards for member in board["leaders"]})
    database = state / "asset-research/research.sqlite3"
    histories = defaultdict(list)
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=8)) as db:
        market = [{"id": code[-2:].lower() + code[:6], "name": name, "bars": histories["index", code]}
                  for code, name in db.execute("SELECT code,name FROM assets WHERE kind='index' ORDER BY code")]
        if sessions:
            earliest, latest = sessions[0], sessions[-1]
            for kind, code, day, close, pre_close, source in db.execute("""SELECT b.kind,b.code,b.day,b.close,b.pre_close,b.source
                    FROM bars b WHERE b.kind IN ('index','sector') AND b.day BETWEEN ? AND ?
                    AND (b.source <> 'sxdata_rt_idx_k.pre_close' OR EXISTS (
                        SELECT 1 FROM index_close_evidence e WHERE e.code=b.code AND e.day=b.day
                        AND e.close=b.close AND e.source=b.source AND e.available_at<=?))""",
                    (earliest, latest, now.isoformat())):
                # A verified next-session official pre_close is the preceding close.
                source = "sxdata_daily" if source == "sxdata_rt_idx_k.pre_close" else source
                histories[kind, code].append(dict(date=day, close=close, pre_close=pre_close, source=source))
            for code, day, close, pre_close, source in db.execute("""SELECT code,day,close,pre_close,source FROM bars
                    WHERE kind='stock' AND code IN (SELECT value FROM json_each(?)) AND day BETWEEN ? AND ?""",
                    (json.dumps(codes), earliest, latest)):
                histories["stock", code].append(dict(date=day, close=close, pre_close=pre_close, source=source))

    for board in boards:
        board["bars"] = histories["sector", board["id"]]
    stocks = {code: histories["stock", code] for code in codes}
    return trend_snapshot(market, boards, sessions, stocks, now)


def main():
    path = STATE / "quote-output/trends.json"
    packet = snapshot(STATE, PUBLIC, shanghai_now())
    old = read(path)
    if any(old.get(key) != value for key, value in packet.items() if key != "generated_at"):
        publish(path, packet)


if __name__ == "__main__":
    main()
