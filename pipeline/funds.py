"""Bounded ETF discovery and dated values; no holdings certification or orders.

Only an explicit domestic industry-index name can create a theme candidate.
It is not proof of SW industry exposure. No broad-index fund fills a gap.
Credentials come exclusively from STOCK_API_TOKEN and leave only for ENDPOINT.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from datetime import time as clock, timedelta
from pathlib import Path

from pipeline.calendar import is_trading_day, previous_trading_day, shanghai_now
from pipeline.stock_api import (ENDPOINT, MAX_BYTES, _Client as _BaseClient, _Failure,
    _bounded, _date, _instant, _provider_reason, _reject_constant, _retry_after)

FIELDS = {
    "etf_basic": "ts_code,csname,cname,index_code,index_name,list_date,list_status,mgr_name,etf_type",
    "fund_daily": "ts_code,trade_date,close,pct_chg,amount",
    "etf_share_size": "ts_code,trade_date,total_share,total_size,nav,close",
}
DOCS = {"etf_basic": "https://tushare.pro/document/2?doc_id=385",
        "fund_daily": "https://tushare.pro/document/2?doc_id=127",
        "etf_share_size": "https://tushare.pro/document/2?doc_id=408"}
REGISTRY_PATH = Path(__file__).resolve().parents[1] / "config/fund_managers.json"
# These are transparent name rules, not certified SW constituent mappings.
THEMES = {
    "801010.SI": r"农业|农林牧渔|养殖|畜牧|种业",
    "801030.SI": r"基础化工|化工",
    "801040.SI": r"钢铁",
    "801050.SI": r"有色金属|稀土|黄金股",
    "801080.SI": r"电子|半导体|芯片",
    "801110.SI": r"家用电器|家电",
    "801120.SI": r"食品饮料|白酒|饮料",
    "801130.SI": r"纺织服饰|纺织服装",
    "801140.SI": r"轻工制造",
    "801150.SI": r"医药|医疗|生物科技",
    "801160.SI": r"公用事业|电力(?!设备)",
    "801170.SI": r"交通运输|物流|航运",
    "801180.SI": r"房地产",
    "801200.SI": r"商贸零售|零售",
    "801210.SI": r"社会服务|旅游",
    "801710.SI": r"建筑材料|建材",
    "801720.SI": r"建筑装饰|基建",
    "801730.SI": r"电力设备|光伏|电池",
    "801740.SI": r"国防军工|军工",
    "801750.SI": r"计算机|软件",
    "801760.SI": r"传媒|游戏",
    "801770.SI": r"通信",
    "801780.SI": r"(?<!非)银行",
    "801790.SI": r"非银金融|非银行|证券公司|证券行业|保险",
    "801880.SI": r"汽车",
    "801890.SI": r"机械设备|机床|工程机械",
    "801950.SI": r"煤炭",
    "801960.SI": r"石油石化|油气",
    "801970.SI": r"环保",
    "801980.SI": r"美容护理",
}
EXCLUDE = re.compile(r"QDII|港股|沪港|深港|恒生|香港|全球|海外|纳斯达克|标普|日经|美国|日本|韩国|中概|债券|国债|货币|期货|商品|REIT|沪深300|中证500|中证1000|中证A500|中证A50|上证50|红利|联接|FOF", re.I)


def _rows(payload, api):
    if not isinstance(payload, dict) or type(payload.get("code")) is not int:
        raise _Failure("invalid_payload", 900)
    if payload["code"] != 0:
        reason = _provider_reason(payload)
        if payload["code"] in (401, 403):
            reason = "authorization_denied"
        delay = 86400 if reason in {"authorization_denied", "permission_denied", "points_insufficient", "unsupported_api"} else 3600
        raise _Failure("api_denied", delay, provider_reason=reason)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise _Failure("invalid_payload", 900)
    fields, items = data.get("fields"), data.get("items")
    expected = FIELDS[api].split(",")
    if (not isinstance(fields, list) or not all(isinstance(x, str) for x in fields)
            or len(fields) != len(expected) or set(fields) != set(expected)
            or not isinstance(items, list) or len(items) > 5000
            or not all(isinstance(row, list) and len(row) == len(fields) for row in items)):
        raise _Failure("invalid_payload", 900)
    # A full page might be truncated; do not silently label it complete.
    if len(items) == 5000:
        raise _Failure("page_limit_reached", 86400)
    return [dict(zip(fields, row)) for row in items]


class _Client(_BaseClient):
    def __init__(self, token, cfg, now):
        options = dict(cfg)
        options["max_requests"] = int(_bounded(cfg.get("max_requests"), 4, 0, 4))
        super().__init__(token, options, now)

    def request(self, api, params):
        if api not in FIELDS or self.count >= self.max_requests or self.remaining() <= 0:
            raise _Failure("budget_exhausted")
        if self.last is not None:
            delay = max(0, self.interval - (time.monotonic() - self.last))
            if delay >= self.remaining():
                raise _Failure("budget_exhausted")
            if delay:
                time.sleep(delay)
        if self.remaining() <= 0:
            raise _Failure("budget_exhausted")
        body = json.dumps({"api_name": api, "token": self._token, "params": params,
                           "fields": FIELDS[api]}, separators=(",", ":")).encode()
        request = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": "A-share-radar/fund-research"})
        self.count += 1
        self.last = time.monotonic()
        try:
            with self.opener.open(request, timeout=min(self.timeout, self.remaining())) as response:
                if response.geturl() != ENDPOINT:
                    raise _Failure("redirect_blocked", 3600)
                status = getattr(response, "status", 200)
                if status == 429:
                    raise _Failure("rate_limited", _retry_after(response.headers, self.now))
                if status in (401, 403):
                    raise _Failure("authorization_denied", 86400)
                if status != 200:
                    raise _Failure("http_error", 3600)
                length = response.headers.get("Content-Length") if response.headers else None
                if length and (not str(length).isdigit() or int(length) > MAX_BYTES):
                    raise _Failure("response_too_large", 900)
                raw = response.read(MAX_BYTES + 1)
                if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
                    raise _Failure("response_too_large", 900)
                if self.remaining() <= 0:
                    raise _Failure("budget_exhausted")
            return _rows(json.loads(raw.decode("utf-8"), parse_constant=_reject_constant), api)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise _Failure("rate_limited", _retry_after(exc.headers, self.now)) from None
            if exc.code in (401, 403):
                raise _Failure("authorization_denied", 86400) from None
            raise _Failure("redirect_blocked" if 300 <= exc.code < 400 else "http_error", 3600) from None
        except _Failure:
            raise
        except (TimeoutError, urllib.error.URLError, OSError):
            raise _Failure("transport_error", 900) from None
        except Exception:
            raise _Failure("invalid_payload", 900) from None


def _positive(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and 0 < value < 1e16 else None


def _text(value, token="", limit=180):
    if (not isinstance(value, str) or not 0 < len(value) <= limit
            or any(ord(c) < 32 for c in value) or (token and token in value)):
        return None
    return value.strip() or None


def _registry():
    data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return {alias: manager for manager in data["managers"] for alias in manager["aliases"]}


def _match(row):
    text = " ".join(str(row.get(key) or "") for key in ("cname", "index_name"))
    if row.get("etf_type") not in {"境内", "纯境内"} or EXCLUDE.search(text):
        return None
    index = row.get("index_name") or ""
    hits = [(code, re.search(pattern, index)) for code, pattern in THEMES.items()]
    hits = [(code, hit.group()) for code, hit in hits if hit]
    return hits[0] if len(hits) == 1 else None


def _clean(api, rows, now, registry, token="", target=None):
    cleaned = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = row.get("ts_code")
        if not isinstance(code, str) or not re.fullmatch(r"\d{6}\.(SH|SZ)", code):
            continue
        if api == "etf_basic":
            item = {key: _text(row.get(key), token) for key in FIELDS[api].split(",")}
            if (item["list_status"] != "L" or item["mgr_name"] not in registry
                    or not all(item[k] for k in ("cname", "index_code", "index_name", "csname"))):
                continue
            try:
                if _date(item["list_date"], compact=True) > now.date():
                    continue
            except _Failure:
                continue
            if _match(item):
                cleaned[code] = item
        else:
            try:
                day = _date(row.get("trade_date"), compact=True)
            except _Failure:
                continue
            if day > now.date() or is_trading_day(day) is not True or (target and day != target):
                continue
            item = {"ts_code": code, "trade_date": day.strftime("%Y%m%d")}
            keys = ("close", "amount") if api == "fund_daily" else ("total_share", "total_size", "nav", "close")
            item.update({key: _positive(row.get(key)) for key in keys})
            if api == "fund_daily":
                pct = row.get("pct_chg")
                item["pct_chg"] = float(pct) if type(pct) in (int, float) and math.isfinite(pct) and abs(pct) < 10000 else None
            if not any(item.get(k) is not None for k in keys):
                continue
            previous = cleaned.get(code)
            if previous is None or item["trade_date"] > previous["trade_date"]:
                cleaned[code] = item
    return list(cleaned.values())


def _targets(now):
    previous = previous_trading_day(now.date())
    daily = now.date() if is_trading_day(now.date()) is True and now.time().replace(tzinfo=None) >= clock(15, 5) else previous
    size = previous
    # The prior session is not yet released the following morning before 08:45.
    if size and (now.date() - size).days == 1 and now.time().replace(tzinfo=None) < clock(8, 45):
        size = previous_trading_day(size)
    return daily, size


def _render(result, state, registry, now):
    caches = state.get("cache", {})
    catalog = caches.get("etf_basic", {}).get("rows", [])
    daily = {x["ts_code"]: x for x in caches.get("fund_daily", {}).get("rows", [])}
    sizes = {x["ts_code"]: x for x in caches.get("etf_share_size", {}).get("rows", [])}
    groups = {}
    for row in catalog:
        match = _match(row)
        manager = registry.get(row.get("mgr_name"))
        if not match or not manager:
            continue
        code = row["ts_code"]
        quote, size = daily.get(code, {}), sizes.get(code, {})
        price, price_day = quote.get("close"), quote.get("trade_date")
        price_source = DOCS["fund_daily"]
        if size.get("close") and (not price or (size.get("trade_date") or "") > (price_day or "")):
            price, price_day = size["close"], size["trade_date"]
            price_source = DOCS["etf_share_size"]
        nav, nav_day = size.get("nav"), size.get("trade_date") if size.get("nav") else None
        aum = size.get("total_size")
        premium = (price / nav - 1) * 100 if price and nav and price_day == nav_day else None
        iso = lambda value: _date(value, compact=True).isoformat() if value else None
        fund = {
            "code": code, "name": row["csname"], "manager": manager["name"],
            "broker_relation": manager["broker_relation"],
            "broker_source_url": manager["source_url"], "broker_source_date": manager["source_date"],
            "price": price, "price_date": iso(price_day), "price_kind": "daily_close",
            "nav": nav, "nav_date": iso(nav_day), "aum_cny": aum * 10000 if aum else None,
            "aum_date": iso(size.get("trade_date")) if aum else None,
            "premium_pct": round(premium, 6) if premium is not None else None,
            "match_basis": f"Tushare基准指数名称“{row['index_name']}”含“{match[1]}”；主题候选，未核验申万持仓比例",
            "match_status": "index_name_candidate", "source_url": DOCS["etf_basic"],
            "index_code": row["index_code"], "index_name": row["index_name"],
            "provider": "stock_api", "upstream_provider": "unknown",
            "data_sources": {"price": price_source, "nav_and_aum": DOCS["etf_share_size"]},
            "stale": not price_day or _date(price_day, compact=True) != _targets(now)[0],
            "turnover_cny": quote.get("amount") * 1000 if quote.get("amount") else None,
        }
        groups.setdefault(match[0], []).append(fund)
    covered = 0
    for sector in result.get("sectors", []):
        sector["funds"] = []
        sector["funds_note"] = "暂无通过A股行业主题规则的券商关联基金；不以宽基或跨境基金补位。"
        code = sector.get("index_code") or sector.get("id")
        if sector.get("taxonomy") != "SW2021" or code not in THEMES:
            continue
        candidates = sorted(groups.get(code, []), key=lambda x: (x["aum_cny"] or 0, x["turnover_cny"] or 0, x["code"]), reverse=True)
        chosen, seen = [], set()
        for candidate in candidates:
            if candidate["manager"] not in seen:
                chosen.append(candidate)
                seen.add(candidate["manager"])
            if len(chosen) == 3:
                break
        if len(chosen) < 3:
            chosen.extend(x for x in candidates if x not in chosen)
        sector["funds"] = chosen[:3]
        if chosen:
            covered += 1
            sector["funds_note"] = "按基准指数名称关联的主题候选，非申万行业持仓认证。市场价、净值及规模各自显示日期；单位净值高低不代表估值贵贱。"
    result["fund_summary"] = {"status": state.get("status"), "covered_sectors": covered,
        "total_sectors": len(result.get("sectors", [])), "candidate_count": sum(len(x) for x in groups.values()),
        "requests_this_run": state.get("requests_this_run", 0),
        "note": "券商控股与参股分别披露；最多三只基金，不能严谨关联的行业保持空白。"}
    return result


def enrich_funds(dashboard, config=None, now=None):
    """Return a copy with dated fund candidates and resumable fund_state caches.

    config.funds: enabled, max_requests (hard <=4), min_interval_seconds (>=3),
    budget_seconds (<=50), timeout_seconds (<=15). Network and stock_api enabled
    switches also apply. No key or credential may be supplied through config.
    """
    result = copy.deepcopy(dashboard)
    config = config or {}
    cfg = config.get("funds", {})
    now = _instant(now) if now is not None else shanghai_now()
    if now is None:
        raise ValueError("invalid_now")
    registry = _registry()
    token = os.environ.get("STOCK_API_TOKEN", "").strip()
    stamp = now.isoformat(timespec="seconds")
    old = result.get("fund_state", {})
    state = {"version": 1, "last_run_at": stamp, "requests_this_run": 0, "status": "pending", "cache": {}, "endpoints": {}}
    for api in FIELDS:
        cached = old.get("cache", {}).get(api, {})
        fetched = _instant(cached.get("fetched_at"))
        if fetched and fetched <= now:
            state["cache"][api] = {"fetched_at": fetched.isoformat(timespec="seconds"),
                "target_date": cached.get("target_date"),
                "rows": _clean(api, cached.get("rows", []), now, registry, token)}
        prior = old.get("endpoints", {}).get(api, {})
        retry = _instant(prior.get("cooldown_until"))
        if retry and retry > now:
            state["endpoints"][api] = {"cooldown_until": retry.isoformat(timespec="seconds"), "status": "cooldown"}
    result["fund_state"] = state
    sources = {x["id"]: x for x in result.get("sources", []) if isinstance(x, dict) and x.get("id")}
    source = {"id": "funds", "name": "Tushare兼容ETF基金", "type": "fund", "url": ENDPOINT,
              "provider": "stock_api", "upstream_provider": "unknown", "status": "pending",
              "message": "按指数名称筛选的券商关联A股主题ETF；未核验行业持仓比例"}
    sources["funds"] = source
    result["sources"] = list(sources.values())

    def finish(status, error=None):
        state["status"] = source["status"] = status
        if error:
            source["error_code"] = error
        return _render(result, state, registry, now)

    if not config.get("network_enabled", True) or not cfg.get("enabled", True) or not config.get("stock_api", {}).get("enabled", True):
        return finish("pending", "offline")
    if not token:
        return finish("pending", "not_configured")
    # The primary pipeline uses the same credential and host; honor its global
    # rate-limit/authentication cooldown as well as this module's own cooldown.
    retries = [_instant(old.get("cooldown_until")),
               _instant(result.get("stock_api_state", {}).get("cooldown_until"))]
    global_retry = max((value for value in retries if value), default=None)
    if global_retry and global_retry > now:
        state["cooldown_until"] = source["retry_after"] = global_retry.isoformat(timespec="seconds")
        return finish("cooldown")
    daily_target, size_target = _targets(now)
    if daily_target is None or size_target is None:
        return finish("pending", "unknown_calendar")
    try:
        client_cfg = {**config.get("stock_api", {}), **cfg}
        client_cfg["max_requests"] = int(_bounded(cfg.get("max_requests"), 4, 0, 4))
        client = _Client(token, client_cfg, now)
    except _Failure as exc:
        return finish("error", exc.code)
    errors = []
    stop = False

    def fetch(api, target=None):
        nonlocal stop
        if stop:
            return False
        cached = state["cache"].get(api, {})
        fetched = _instant(cached.get("fetched_at"))
        if fetched and ((api == "etf_basic" and now - fetched < timedelta(days=7)) or
                        (api != "etf_basic" and cached.get("target_date") == target.isoformat())):
            return True
        endpoint = state["endpoints"].setdefault(api, {})
        retry = _instant(endpoint.get("cooldown_until"))
        if retry and retry > now:
            errors.append("cooldown")
            return False
        endpoint["last_attempt"] = stamp
        params = {"list_status": "L"} if api == "etf_basic" else {"trade_date": target.strftime("%Y%m%d")}
        try:
            rows = client.request(api, params)
            if not rows:
                raise _Failure("empty_response", 900)
            cleaned = _clean(api, rows, now, registry, token, target)
            if api != "etf_basic" and not cleaned:
                raise _Failure("invalid_snapshot", 900)
            if api != "etf_basic":
                eligible = {x["ts_code"] for x in state["cache"].get("etf_basic", {}).get("rows", [])}
                # Preserve older records for a fund absent from a partial daily publication.
                merged = {x["ts_code"]: x for x in cached.get("rows", []) if x["ts_code"] in eligible}
                for row in cleaned:
                    if row["ts_code"] in eligible and row["trade_date"] >= merged.get(row["ts_code"], {}).get("trade_date", ""):
                        merged[row["ts_code"]] = row
                cleaned = list(merged.values())
            state["cache"][api] = {"fetched_at": stamp, "target_date": target.isoformat() if target else None, "rows": cleaned}
            endpoint.update(status="success", last_success=stamp, cooldown_until=None)
            source["last_success"] = stamp
            return True
        except _Failure as exc:
            errors.append(exc.code)
            if exc.code == "budget_exhausted":
                stop = True
                endpoint.update(status="pending", error_code=exc.code)
                return False
            until = (now + timedelta(seconds=max(900, exc.retry_seconds))).isoformat(timespec="seconds")
            endpoint.update(status="cooldown", error_code=exc.code, cooldown_until=until)
            if exc.code in {"rate_limited", "authorization_denied", "redirect_blocked"} or exc.provider_reason in {"rate_limited", "authorization_denied"}:
                state["cooldown_until"] = source["retry_after"] = until
                stop = True
            return False
        finally:
            state["requests_this_run"] = client.count

    fetch("etf_basic")
    if state["cache"].get("etf_basic", {}).get("rows"):
        fetch("fund_daily", daily_target)
        fetch("etf_share_size", size_target)
    return finish("partial" if errors and state["cache"] else "error" if errors else "success", errors[0] if errors else None)
