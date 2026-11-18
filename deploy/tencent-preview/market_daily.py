"""Persist official completed daily inputs and as-of valuation anchors."""

from datetime import datetime, time, timedelta
import json
import math
import os
from pathlib import Path

from pipeline.calendar import is_trading_day, previous_trading_day, shanghai_now
from pipeline.stock_api import FIELDS, _Failure, _basic_records, _index_bars, _index_code, _sw_bars
from relay_common import STATE, relay_client
from relay_quotes import SOURCE, publish

OUTPUT = STATE / "quote-output/daily-market.json"


def read_daily():
    daily = json.loads(OUTPUT.read_text()) if OUTPUT.exists() else {"bars": {}, "stocks": {}}
    daily.setdefault("market_caps", {})
    return daily


def merge_daily(data, daily, through=None):
    for entity in [*data.get("market", []), *data.get("sectors", [])]:
        additions = {day: bar for day, bar in daily["bars"].get(entity["id"], {}).items()
                     if not through or day <= through}
        all_bars = {bar["date"]: bar for bar in entity.get("history", [])}
        all_bars.update(additions)
        entity["history"] = [all_bars[key] for key in sorted(all_bars)]
        if additions:
            latest = additions[max(additions)]
            date = latest["date"]
            if date >= (entity.get("daily_quote") or {}).get("quote_date", ""):
                entity["daily_quote"] = {"price": latest["close"], "change_pct": latest["change_pct"],
                    "quote_date": date, "quote_timestamp": latest["as_of"], "timestamp_quality": "daily_close",
                    "provider": "stock_api"}
            if latest.get("market_cap_cny") is not None:
                caps = {bar["date"]: bar for bar in entity.get("market_cap_history", [])}
                caps[date] = {"date": date, "value": latest["market_cap_cny"]}
                entity["market_cap_history"] = [caps[key] for key in sorted(caps)]
                if date >= (entity.get("market_cap_date") or ""):
                    entity.update(market_cap=latest["market_cap_cny"], market_cap_date=date)
        cap_history = daily["market_caps"].get(entity["id"], {})
        caps = {day: value for day, value in cap_history.items() if not through or day <= through}
        if caps:
            history = {bar["date"]: bar for bar in entity.get("market_cap_history", [])}
            history.update({day: {"date": day, "value": value} for day, value in caps.items()})
            entity["market_cap_history"] = [history[day] for day in sorted(history)]
            latest = max(caps)
            if latest >= (entity.get("market_cap_date") or ""):
                entity.update(market_cap=caps[latest], market_cap_date=latest)
        for stock in entity.get("stocks", []):
            ref = daily["stocks"].get(stock.get("ts_code"))
            if ref and (not through or ref["quote_date"] <= through) and ref["quote_date"] >= (stock.get("quote_date") or ""):
                stock.update(ref)
    return data


def refresh():
    now = shanghai_now()
    day = now.date()
    if is_trading_day(day) is not True:
        return
    pre_open_recovery = time(7) <= now.time() < time(9, 15)
    if pre_open_recovery:
        day = previous_trading_day(day)
        if day is None:
            return
    elif now.time() < time(15, 30):
        return
    date = day.isoformat()
    api_date = day.strftime("%Y%m%d")
    stamp = now.isoformat()
    token = (Path(os.environ["CREDENTIALS_DIRECTORY"]) / "sxdata-token").read_text().strip()
    source = json.loads(SOURCE.read_text())
    daily = read_daily()
    daily.update(schema_version=1, source="sxdata_daily", trade_date=date)
    index_map = {_index_code(item): item["id"] for item in source["market"] if _index_code(item)}
    industry_ids = {i["id"] for i in source["sectors"] if i.get("taxonomy") == "SW2021"}
    stock_codes = {s["ts_code"] for i in source["sectors"] for s in i.get("stocks", []) if s.get("ts_code")}
    FIELDS["index_dailybasic"] = "ts_code,trade_date,total_mv"

    def request(api, query):
        try:
            with relay_client(token, timeout=18) as client:
                return client.request(api, query)
        except _Failure as error:
            print(json.dumps({"api": api, "status": error.code}), flush=True)
            return []

    def save(entity_id, bar):
        by_day = daily["bars"].setdefault(entity_id, {})
        by_day[bar["date"]] = bar
        for key in sorted(by_day)[:-250]:
            del by_day[key]

    try:
        for code, entity_id in index_map.items():
            if date in daily["bars"].get(entity_id, {}):
                continue
            rows = request("index_daily", {"ts_code": code, "start_date": api_date, "end_date": api_date})
            try:
                for bar in _index_bars(rows, code, day, day, stamp):
                    save(entity_id, bar)
            except _Failure as error:
                print(json.dumps({"api": "index_daily", "symbol": code, "status": error.code}), flush=True)
        if (pre_open_recovery or now.time() >= time(18, 30)) and any(date not in daily["bars"].get(code, {}) for code in industry_ids):
            rows = request("sw_daily", {"trade_date": api_date})
            if rows:
                grouped = {}
                for row in rows:
                    if isinstance(row["ts_code"], str) and row["ts_code"] in industry_ids:
                        grouped.setdefault(row["ts_code"], []).append(row)
                for code, group in grouped.items():
                    try:
                        for bar in _sw_bars(group, {code}, day, day, stamp, token).get(code, []):
                            save(code, bar)
                    except _Failure as error:
                        print(json.dumps({"api": "sw_daily", "symbol": code, "status": error.code}), flush=True)
        missing_stocks = any(daily["stocks"].get(code, {}).get("quote_date") != date for code in stock_codes)
        attempts = daily.get("stock_batch_attempts", {})
        attempts_today = attempts.get("count", 0) if attempts.get("date") == date else 0
        if missing_stocks and attempts_today < 2:
            rows = request("daily_basic", {"trade_date": api_date})
            matching = [r for r in rows if isinstance(r["ts_code"], str) and r["ts_code"] in stock_codes]
            if matching:
                try:
                    values = _basic_records(matching, day, stamp)
                except _Failure as error:
                    values = []
                    print(json.dumps({"api": "daily_basic", "status": error.code}), flush=True)
                for value in values:
                    if value.get("market_cap_cny") is not None:
                        daily["stocks"][value["ts_code"]] = {"quote_date": date, "price": value["close"], "market_cap_date": date,
                            "market_cap": value["market_cap_cny"], "timestamp_quality": "daily_close",
                            "as_of": date+"T15:00:00+08:00", "provider": "stock_api"}
                if values:
                    daily["stock_batch_attempts"] = {"date": date, "count": attempts_today + 1}
        coverage = {"000001.SH", "399001.SZ", "399006.SZ", "000688.SH", "000905.SH"}
        for code, entity_id in index_map.items():
            if code not in coverage:
                continue
            caps = daily["market_caps"].setdefault(entity_id, {})
            if date in caps:
                continue
            start = day-timedelta(days=365) if not caps else day-timedelta(days=7)
            for row in request("index_dailybasic", {"ts_code": code, "start_date": start.strftime("%Y%m%d"), "end_date": api_date}):
                try:
                    row_day = datetime.strptime(row["trade_date"], "%Y%m%d").date()
                except (TypeError, ValueError):
                    continue
                cap = row["total_mv"]
                if row["ts_code"] == code and is_trading_day(row_day) is True and row_day <= day and type(cap) in (float, int) and cap > 0 and math.isfinite(cap):
                    caps[row_day.isoformat()] = cap
            for key in sorted(caps)[:-250]:
                del caps[key]
    except _Failure as error:
        print(json.dumps({"daily": error.code}), flush=True)
    finally:
        publish(OUTPUT, daily)
        print(json.dumps({"day": date, "daily_bars": sum(date in bars for bars in daily["bars"].values()),
                          "valued_stocks": sum(value["quote_date"] == date for value in daily["stocks"].values())}), flush=True)


if __name__ == "__main__":
    refresh()
