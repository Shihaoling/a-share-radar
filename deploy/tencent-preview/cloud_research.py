"""Post-close research using read-only model assets and confirmed SXData rows."""

from copy import deepcopy
from datetime import time, timedelta
import json
import math
import os
from pathlib import Path
import shutil

from pipeline.calendar import is_trading_day, shanghai_now
from pipeline.equity_data import collect_equity_history
from pipeline.equity_model import update_equity_predictions
from pipeline.funds import enrich_funds
from pipeline.metrics import compute_metrics
from pipeline.model import DEFAULTS, _settle
from pipeline.review import daily_review
from pipeline.stock_api import FIELDS, _Failure, _attach_members, _members
from pipeline.validation import validate_prices
from market_daily import merge_daily, read_daily
from relay_common import STATE, relay_client, relay_guard
from relay_quotes import SOURCE, publish

RUNTIME = Path("/opt/ashare-radar-preview/runtime")
PRIVATE = STATE / "research"
PUBLIC = STATE / "research-output/research.json"


def read(path, fallback=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return fallback if fallback is not None else {}


def history_directory():
    source = RUNTIME / "data/equity"
    root = PRIVATE / "equity"
    marker = root / ".seeded"
    if not marker.exists():
        root.mkdir(parents=True, exist_ok=True)
        daily = root / "daily"
        daily.mkdir(exist_ok=True)
        for path in sorted((source/"daily").glob("*.json.gz"))[-70:]:
            shutil.copyfile(path,daily/path.name)
        if (source/"forecasts").exists():
            shutil.copytree(source/"forecasts",root/"forecasts",dirs_exist_ok=True)
        marker.touch()
    return root


def refresh_members(dashboard, token, now, cache):
    cutoff = (now.date()-timedelta(days=18)).isoformat()
    order = sorted(dashboard["sectors"], key=lambda s: cache.get(s["id"], {}).get("checked_at", ""))
    for sector in order[:2]:
        code = sector["id"]
        old = cache.get(code, {})
        if sector.get("taxonomy")!="SW2021" or old.get("checked_at", "")[:10]>=cutoff:
            continue
        try:
            with relay_client(token,timeout=8) as client:
                rows = client.request("index_member_all", {"l1_code":code,"is_new":"Y"})
            quality = {}
            members = _members(rows,code,token,now.isoformat(),quality)
            cache[code] = {"checked_at":now.isoformat(),"fetched_at":now.isoformat(),
                           "records":members,"complete":len(rows)<2000 and quality["complete"]}
        except _Failure as error:
            print(json.dumps({"membership":code,"status":error.code}),flush=True)
            if error.code == "rate_limited":
                break
            cache[code] = {**old,"checked_at":now.isoformat()}
    for sector in dashboard["sectors"]:
        current = cache.get(sector["id"], {})
        if current.get("records"):
            published = {row.get("ts_code"):row for row in sector.get("constituents", [])}
            display = {row.get("ts_code"):row for row in sector.get("stocks", [])}
            _attach_members(sector,current)
            for member in sector.get("constituents", []):
                previous = published.get(member["ts_code"]) or display.get(member["ts_code"])
                if previous:
                    member.update({field:value for field,value in previous.items() if field not in member or member[field] is None})


def fetch_flows(token, now, previous, stocks):
    date = now.date().isoformat()
    if now.time()<time(19) or previous.get("trade_date")==date:
        return previous
    FIELDS["moneyflow"] = "ts_code,trade_date,net_mf_amount,buy_lg_amount,sell_lg_amount"
    try:
        with relay_client(token,timeout=20) as client:
            rows = client.request("moneyflow", {"trade_date":now.strftime("%Y%m%d")})
    except _Failure as error:
        return {**previous,"status":error.code}
    values = {}
    for row in rows:
        symbol = row["ts_code"]
        if symbol not in stocks or row["trade_date"]!=now.strftime("%Y%m%d"):
            continue
        fields = {key: value for key,value in row.items() if key.endswith("_amount")
                  and type(value) in (int,float) and math.isfinite(value)}
        if fields:
            values[symbol] = fields
    return {"status":"ready","trade_date":date,"unit":"CNY_10000","stocks":values} if values else {**previous,"status":"empty"}


def attach_quotes(dashboard, daily, now):
    bars = daily["bars"]
    current = now.date().isoformat()
    evidence = dashboard.setdefault("price_observations", [])
    for group in ("market","sectors"):
        for item in dashboard[group]:
            bar = bars.get(item["id"],{}).get(current)
            if bar:
                evidence.append({"entity_id":item["id"],"provider":"stock_api","source":"stock_api","basis":"index",
                    "field":"close","price":bar["close"],"timestamp":bar["as_of"],"market_timestamp":bar["as_of"],
                    "observed_at":bar["fetched_at"],"timestamp_quality":"daily_close","trade_date":current,
                    "adjustment":bar.get("adjustment","unadjusted"),"complete":True})


def review_models(dashboard, forecasts, now):
    entities = {item["id"]:item for group in ("market","sectors") for item in dashboard[group]}
    _settle(dashboard["ledger"],entities,now,{**DEFAULTS,"training_enabled":False})
    date = now.date().isoformat()
    review = daily_review(dashboard,now)
    point = [row for row in forecasts.get("daily_forecast",{}).get("ledger",[])
             if row.get("target_date")==date and row.get("settled_at")]
    direction = {}
    for row in sorted(forecasts.get("direction_probability",{}).get("ledger",[]),key=lambda r:r.get("issued_at","")):
        if row.get("target_date")==date and row.get("settled_at"):
            direction.setdefault(row["id"],row)
    review["model_settlements"] = {
        "closing_point": {"count":len(point),"mae_pct":sum(r["absolute_error_pct"] for r in point)/len(point) if point else None},
        "closing_direction": {"count":len(direction),
                              "accuracy":sum(r["direction_correct"] for r in direction.values())/len(direction) if direction else None},
    }
    return review


def main():
    now = shanghai_now()
    if is_trading_day(now.date()) is not True or now.time()<time(15,30):
        return
    PRIVATE.mkdir(parents=True,exist_ok=True)
    os.environ["RADAR_ALLOW_TRAINING"] = "false"
    key_path = Path(os.environ["CREDENTIALS_DIRECTORY"])/"sxdata-token"
    token = key_path.read_text().strip()
    os.environ["STOCK_API_TOKEN"] = token
    config = read(RUNTIME/"config/settings.json")
    config.update(network_enabled=True,training_enabled=False)
    config["equity"] = {**config.get("equity",{}),"history_days":65,"enable_training":False,"max_requests":2}
    config["funds"] = {**config.get("funds",{}),"max_requests":4,"min_interval_seconds":6}
    dashboard = read(RUNTIME/"web/data/dashboard.json")
    baseline = read(SOURCE)
    for group in ("market","sectors"):
        histories = {item["id"]:item for item in dashboard.get(group,[])}
        dashboard[group] = [{**histories.get(item["id"],{}),**item} for item in baseline.get(group,[])]
    for field in ("news","predictions","ledger","market_session"):
        dashboard[field] = baseline.get(field,dashboard.get(field,[]))
    dashboard["generated_at"] = now.isoformat()
    dashboard["ledger"] = read(PRIVATE/"ledger.json",dashboard["ledger"])
    cache = read(PRIVATE/"inputs.json")
    if cache.get("fund_state"):
        dashboard["fund_state"] = cache["fund_state"]
    daily = read_daily()
    merge_daily(dashboard,daily)
    attach_quotes(dashboard,daily,now)
    memberships = cache.get("memberships",{})
    refresh_members(dashboard,token,now,memberships)
    equity_root = history_directory()
    with relay_guard():
        dashboard["equity_collection"] = collect_equity_history(dashboard, config, now, output_dir=equity_root)
    with relay_guard():
        dashboard = enrich_funds(dashboard,config,now)
    stock_codes = {stock.get("ts_code") for sector in dashboard["sectors"] for stock in sector.get("stocks",[])}
    flows = fetch_flows(token,now,cache.get("flows",{}),stock_codes)
    publish(PRIVATE/"inputs.json",{"memberships":memberships,
                                  "fund_state":dashboard.get("fund_state",{}),"flows":flows})
    dashboard = compute_metrics(validate_prices(dashboard,now),{**config,"as_of":now.isoformat()})
    dashboard = update_equity_predictions(dashboard,equity_root,RUNTIME/"data/equity/models",now,config)
    forecast = read(STATE/"model-output/models.json")
    news = read(STATE/"news-output/news.json")
    if news.get("trade_date")==now.date().isoformat():
        items = {item["id"]:item for item in dashboard["news"]}
        for source in news["articles"]:
            item = deepcopy(source)
            analysis = item.get("llm_analysis") or {}
            if analysis.get("status")=="ready":
                item.update(sentiment=analysis.get("tone","unknown"),sentiment_method="minimax_full_text")
            items[item["id"]] = item
        dashboard["news"] = list(items.values())
    review = review_models(dashboard,forecast,now)
    publish(PRIVATE/"ledger.json",dashboard["ledger"])
    fields = ("id","quant_metrics","validation","data_quality","equity_prediction","funds","fund_method",
              "fund_summary","valuation_coverage","membership_count","membership_as_of","membership_complete")
    entities = {item["id"]: {key:item[key] for key in fields if key in item}
                for group in ("market","sectors") for item in dashboard[group]}
    payload = {"schema_version":1,"source":"tencent_research","generated_at":now.isoformat(),
        "entities":entities,"daily_review":review,"data_checks":dashboard["data_checks"],
        "validation_summary":dashboard["validation_summary"],"equity_forecast":dashboard.get("equity_forecast"),
        "fund_summary":dashboard.get("fund_summary"),
        "fund_state":{key:value for key,value in dashboard.get("fund_state",{}).items() if key!="cache"},
        "equity_collection":dashboard.get("equity_collection"),"moneyflow":flows,
        "ledger":dashboard["ledger"],"training_enabled":False}
    publish(PUBLIC,payload)
    print(json.dumps({"published":now.isoformat(),"review":review["settled_count"],
                      "equity":dashboard.get("equity_forecast",{}).get("status"),
                      "fund":dashboard.get("fund_state",{}).get("status"),"flow":flows.get("status")}),flush=True)


if __name__=="__main__":
    main()
