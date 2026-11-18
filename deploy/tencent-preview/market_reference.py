"""Refresh vendor-dated constituent weights for exchange industry indices."""

import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re

from pipeline.calendar import shanghai_now
from pipeline.stock_api import FIELDS, _Failure
from relay_common import STATE, relay_client
from relay_quotes import INDUSTRIES, publish


def due_references(codes, cache, now, days, payload_key, success_key):
    cutoff = (now - timedelta(days=days)).isoformat()
    eligible = []
    for code in codes:
        entry = cache.get(code, {})
        if (entry.get("retry_at") or "") > now.isoformat():
            continue
        success = entry.get(success_key) or ""
        if entry.get(payload_key) and entry.get("complete", True) and success > cutoff:
            continue
        eligible.append((entry.get("last_attempt_at") or entry.get("checked_at") or "", code))
    return [code for _, code in sorted(eligible)]


def failed_reference(old, now, error):
    return {**old, "last_attempt_at": now.isoformat(), "error_code": error.code,
            "retry_at": (now + timedelta(seconds=max(60, error.retry_seconds))).isoformat()}


def update(limit):
    now = shanghai_now()
    output = STATE / "relay/industry-weights.json"
    existing = json.loads(output.read_text()) if output.exists() else {}
    token = (Path(os.environ["CREDENTIALS_DIRECTORY"]) / "sxdata-token").read_text().strip()
    FIELDS["index_weight"] = "index_code,con_code,trade_date,weight"
    due = due_references(INDUSTRIES, existing, now, 3, "stocks", "checked_at")
    for code in due[:limit]:
        old = existing.get(code, {})
        try:
            with relay_client(token) as client:
                rows = client.request("index_weight", {
                    "index_code": code, "start_date": (now.date()-timedelta(days=65)).strftime("%Y%m%d"),
                    "end_date": now.strftime("%Y%m%d")})
        except _Failure as error:
            print(json.dumps({"api": "index_weight", "index": code, "status": error.code}), flush=True)
            if error.deferred:
                break
            existing[code] = failed_reference(old, now, error)
            if error.host or error.code == "rate_limited":
                break
            continue
        dates = sorted({r["trade_date"] for r in rows if r["index_code"] == code
                        and isinstance(r["trade_date"], str) and re.fullmatch(r"\d{8}", r["trade_date"])
                        and r["trade_date"] <= now.strftime("%Y%m%d")})
        date = dates[-1] if dates else None
        weights = {r["con_code"]: r["weight"] for r in rows if date and r["index_code"] == code and r["trade_date"] == date
                   and re.fullmatch(r"\d{6}\.(?:SH|SZ)", str(r["con_code"]))
                   and type(r["weight"]) in (float, int) and 0 < r["weight"] <= 100}
        try:
            vendor_date = datetime.strptime(date, "%Y%m%d").date().isoformat() if date else None
        except ValueError:
            vendor_date = None
        if not weights or not vendor_date or vendor_date < (old.get("date") or ""):
            existing[code] = failed_reference(old, now, _Failure("no_current_weights"))
        else:
            top = sorted(weights, key=weights.get, reverse=True)[:8]
            existing[code] = {"date": vendor_date, "checked_at": now.isoformat(), "last_attempt_at": now.isoformat(),
                              "stocks": [{"ts_code": symbol, "weight_pct": weights[symbol]} for symbol in top]}
        print(json.dumps({"api": "index_weight", "index": code, "date": date, "valid_rows": len(weights)}), flush=True)
    publish(output, existing)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, choices=range(1, 18), default=2)
    update(parser.parse_args().limit)
