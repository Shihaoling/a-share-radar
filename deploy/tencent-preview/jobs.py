"""Run the Tencent Cloud preview jobs against separate state directories."""

import argparse
import json
from pathlib import Path
import shutil

from pipeline.calendar import shanghai_now
from pipeline.run import atomic_json, load


def quotes(source, state, inspect_closed):
    from pipeline.page_quotes import Collector, collection_open

    live_path = state / "quotes" / "page-quotes.json"
    path = state / "diagnostics" / "page-quotes.json" if inspect_closed else live_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        shutil.copyfile(source / "web/data/page-quotes.json", path)

    market_closed = not inspect_closed and not collection_open(shanghai_now())
    result = load(path, {}) if market_closed else Collector(path).run_once(inspect_closed=inspect_closed)
    if not inspect_closed:
        # Existing pages consume the full fallback snapshot. The separate file is
        # a bounded payload for the upcoming per-quote refresh path.
        public = state / "quote-output"
        if not market_closed or not (public / "live-quotes.json").exists():
            atomic_json(public / "page-quotes.json", result)
            fields = ("schema_version", "generated_at", "trade_date", "collector", "market", "sectors")
            atomic_json(public / "live-quotes.json", {key: result[key] for key in fields if key in result})
    if market_closed:
        return {"mode": "scheduled", "status": "market_closed"}
    return {"mode": "diagnostic" if inspect_closed else "scheduled",
            "collector": result.get("collector", {}), "trade_date": result.get("trade_date")}


def news_replay(source, state):
    from pipeline.news_archive import archive_news
    from pipeline.news_digest import digest_inputs, validate_digest
    from pipeline.publish import publish_view

    data = load(source / "web/data/dashboard.json", {})
    digest = data.get("news_digest", {})
    day = digest.get("date") or data["generated_at"][:10]
    inputs = digest_inputs(data["news"], day)
    allowed_ids = {row["id"] for row in inputs}
    if digest.get("summary"):
        validate_digest(digest, allowed_ids)
    replay = state / "news-replay"
    archive_news(data, root=replay / "archive")
    publish_view(data, replay / "view.json")
    return {"mode": "offline_replay", "articles": len(data["news"]),
            "digest_inputs": len(inputs), "cached_digest_verified": bool(digest.get("summary")),
            "source_generated_at": data["generated_at"]}


def research_replay(source, state):
    from pipeline.publish import health_summary
    from pipeline.run import run

    replay = state / "research-replay"
    replay.mkdir(parents=True, exist_ok=True)
    dashboard = replay / "dashboard.json"
    for name in ("dashboard.json", "reviews.json"):
        if not (replay / name).exists():
            shutil.copyfile(source / "web/data" / name, replay / name)
    for name in ("equity", "news_archive"):
        if (source / "data" / name).is_dir() and not (replay / name).exists():
            shutil.copytree(source / "data" / name, replay / name)
    result = run(offline=True, output=dashboard)
    atomic_json(replay / "health.json", health_summary(result))
    return {"mode": "offline_replay", "status": result.get("status"),
            "indices": len(result.get("market", [])), "sectors": len(result.get("sectors", [])),
            "ledger_rows": len(result.get("ledger", []))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", choices=("quotes", "quote-diagnostic", "news-replay", "research-replay"))
    parser.add_argument("--source", type=Path, default=Path("/opt/ashare-radar-preview/runtime"))
    parser.add_argument("--state", type=Path, default=Path("/var/lib/ashare-radar-preview"))
    args = parser.parse_args()
    if args.job == "news-replay":
        result = news_replay(args.source, args.state)
    elif args.job == "research-replay":
        result = research_replay(args.source, args.state)
    else:
        result = quotes(args.source, args.state, args.job == "quote-diagnostic")
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
