"""Refresh vendor-dated constituent weights for exchange industry indices."""

import argparse
from datetime import timedelta
import json
import os
from pathlib import Path
import re

from pipeline.calendar import shanghai_now
from pipeline.stock_api import FIELDS, _Failure
from relay_common import STATE, relay_client
from relay_quotes import INDUSTRIES, publish


def update(limit):
    now = shanghai_now()
    output = STATE / "relay/industry-weights.json"
    existing = json.loads(output.read_text()) if output.exists() else {}
    token = (Path(os.environ["CREDENTIALS_DIRECTORY"]) / "sxdata-token").read_text().strip()
    FIELDS["index_weight"] = "index_code,con_code,trade_date,weight"
    due = sorted(INDUSTRIES, key=lambda code: existing.get(code, {}).get("checked_at", ""))
    for code in due[:limit]:
        old = existing.get(code, {})
        if old.get("checked_at", "")[:10] >= (now.date()-timedelta(days=3)).isoformat():
            break
        try:
            with relay_client(token) as client:
                rows = client.request("index_weight", {
                    "index_code": code, "start_date": (now.date()-timedelta(days=65)).strftime("%Y%m%d"),
                    "end_date": now.strftime("%Y%m%d")})
        except _Failure as error:
            print(json.dumps({"api": "index_weight", "index": code, "status": error.code}), flush=True)
            if error.code == "rate_limited":
                break
            existing[code] = {**old, "checked_at": now.isoformat()}
            continue
        dates = sorted({r["trade_date"] for r in rows if r["index_code"] == code
                        and isinstance(r["trade_date"], str) and r["trade_date"] <= now.strftime("%Y%m%d")})
        date = dates[-1] if dates else None
        weights = {r["con_code"]: r["weight"] for r in rows if r["index_code"] == code and r["trade_date"] == date
                   and re.fullmatch(r"\d{6}\.(?:SH|SZ)", str(r["con_code"]))
                   and type(r["weight"]) in (float, int) and 0 < r["weight"] <= 100}
        if weights:
            date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
            top = sorted(weights, key=weights.get, reverse=True)[:8]
            old = {"date": date, "stocks": [{"ts_code": symbol, "weight_pct": weights[symbol]} for symbol in top]}
        existing[code] = {**old, "checked_at": now.isoformat()}
        print(json.dumps({"api": "index_weight", "index": code, "date": date, "valid_rows": len(weights)}), flush=True)
    publish(output, existing)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, choices=range(1, 18), default=2)
    update(parser.parse_args().limit)
