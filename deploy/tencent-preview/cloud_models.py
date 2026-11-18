"""Inference-only forecast publication and observed five-minute factor checkpoints."""

from datetime import time
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from pipeline.calendar import market_session, shanghai_now
from pipeline.daily_forecast import update_daily_forecasts
from pipeline.direction_probability import update_direction_probabilities
from market_daily import merge_daily, read_daily
from relay_common import STATE
from relay_quotes import SOURCE, publish

RUNTIME = Path("/opt/ashare-radar-preview/runtime")
OUTPUT = STATE / "model-output/models.json"
STATUS = "现有预训练模型支持次日收盘点位和方向。次日开盘、盘中收盘待相应训练产物。现有方向模型的新闻因子未启用。"


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
    if not OUTPUT.exists():
        publish(OUTPUT, {"schema_version": 1, "source": "tencent_models", "status": STATUS})
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
                "status": "missing_intraday_model" if rows else "missing_exact_minute_observations"})))
            return
        if phase != "post_close" or now.time() < time(15, 30):
            return
        daily = read_daily()
        fingerprint = sha256(json.dumps([now.date().isoformat(), daily["bars"], revision], sort_keys=True).encode()).hexdigest()
        previous = json.loads(OUTPUT.read_text())
        if previous.get("input_signature") == fingerprint:
            return
        db.execute("INSERT OR REPLACE INTO factors VALUES (?,?)", ("close:"+now.date().isoformat(),
                   json.dumps({"news_revision": revision, "news": news})))
        data = merge_daily(json.loads(SOURCE.read_text()), daily)
        data = update_daily_forecasts(data, RUNTIME/"data/daily_forecast/models", now, previous)
        data = update_direction_probabilities(data, RUNTIME/"data/direction_probability/models/bundle.json", now, previous)
        payload = {"schema_version": 1, "source": "tencent_models", "status": STATUS,
                   "daily_forecast": data["daily_forecast"], "direction_probability": data["direction_probability"],
                   "input_signature": fingerprint, "generated_at": now.isoformat()}
        publish(OUTPUT, payload)
        print(json.dumps({"point_status": data["daily_forecast"]["status"],
                          "direction_status": data["direction_probability"]["status"],
                          "reference_date": data["daily_forecast"].get("reference_date"),
                          "target_date": data["daily_forecast"].get("target_date")}), flush=True)


if __name__ == "__main__":
    main()
