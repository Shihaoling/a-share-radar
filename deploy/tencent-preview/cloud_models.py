"""Archive observed five-minute and post-close factor checkpoints."""

from datetime import time
import json
import sqlite3

from pipeline.calendar import market_session, shanghai_now
from relay_common import STATE


def latest_news_factors(now):
    path = STATE / "news-output/news.json"
    if not path.exists():
        return "", {}
    feed = json.loads(path.read_text())
    if feed["trade_date"] != now.date().isoformat():
        return "", {}
    groups = {}
    for article in feed["articles"]:
        analysis = article.get("llm_analysis") or {}
        if (analysis.get("status") != "ready" or analysis.get("fact_status") == "rumor"
                or article.get("credibility") == "rumor"):
            continue
        for row in analysis.get("sector_impacts", []):
            direction = row.get("direction")
            if direction in ("positive", "neutral", "negative"):
                groups.setdefault(row["sector_code"], []).append({"positive": 1, "neutral": 0, "negative": -1}[direction])
    return feed["model_revision"], {code: {"mean": sum(values)/len(values), "count": len(values)}
                                    for code, values in groups.items()}


def main():
    now = shanghai_now()
    phase = market_session(now)["phase"]
    folder = STATE / "model"
    folder.mkdir(parents=True, exist_ok=True)
    revision, news = latest_news_factors(now)
    with sqlite3.connect(folder / "inputs.sqlite3", timeout=15) as db:
        db.execute("CREATE TABLE IF NOT EXISTS factors (slot TEXT PRIMARY KEY,payload TEXT NOT NULL)")
        if phase in {"morning", "afternoon", "closing_auction"}:
            if now.minute % 5:
                return
            observations = STATE / "relay/minute-observations.sqlite3"
            if not observations.exists():
                return
            slot = now.isoformat()[:16]
            with sqlite3.connect(f"file:{observations}?mode=ro", uri=True, timeout=10) as quotes:
                rows = quotes.execute("SELECT id,source_stamp,price,change_pct FROM samples WHERE minute=?", (slot,)).fetchall()
            db.execute("INSERT OR IGNORE INTO factors VALUES (?,?)", (slot, json.dumps({
                "observations": rows, "news_revision": revision, "news": news,
                "status": "observed" if rows else "missing_exact_minute_observations"})))
            return
        if phase == "post_close" and now.time() >= time(15, 30):
            db.execute("INSERT OR REPLACE INTO factors VALUES (?,?)", ("close:"+now.date().isoformat(),
                       json.dumps({"news_revision": revision, "news": news})))


if __name__ == "__main__":
    main()
