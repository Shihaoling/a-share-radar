"""Small, timestamp-aware quote batches; no daily-price or valuation substitution.

The Tencent/Sina URLs below are public web quote endpoints, not contracted APIs.
Field layouts are validated against the original open-source implementations:
https://github.com/shidenggui/easyquotation/blob/master/easyquotation/sina.py
https://github.com/ZhuLinsen/daily_stock_analysis/blob/main/data_provider/akshare_fetcher.py
https://github.com/waditu/tushare/blob/master/tushare/stock/trading.py
No sufficiently authoritative provider specification confirms TTM/dynamic PE for
these payloads. PE is deliberately null; ETF/index zero fields are never reused.
"""
from __future__ import annotations

import copy
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import datetime, timedelta
from pathlib import Path

from pipeline.calendar import market_session
from pipeline.sources import _Gate, _dt, _merge, _number, _text, RateLimited, SourceUnavailable

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = {
    "tencent": {"url": "https://qt.gtimg.cn/q=", "name": "腾讯批量快照", "referer": "https://finance.qq.com/"},
    "sina": {"url": "https://hq.sinajs.cn/list=", "name": "新浪批量快照", "referer": "https://finance.sina.com.cn/"},
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _BatchTransport(_Gate):
    """Persist robots decisions and count every HTTP operation, with no redirects."""
    def __init__(self, config, persistent_robots, now):
        super().__init__(config)
        self.persistent_robots = persistent_robots
        self.now = now
        self.opener = urllib.request.build_opener(_NoRedirect())

    def raw(self, url, headers=None):
        with self.send_lock:
            domain = self._before(url)
            request = urllib.request.Request(url, headers={**(headers or {}), "User-Agent": self.cfg["user_agent"]})
            try:
                with self.opener.open(request, timeout=self.timeout()) as response:
                    self._status(response.status, response.headers, domain)
                    body = response.read(500001)
                    if len(body) > 500000:
                        raise SourceUnavailable("快照响应超过500KB预算")
                    return body, response.status
            except urllib.error.HTTPError as exc:
                if exc.code in (301, 302, 303, 307, 308):
                    raise SourceUnavailable("来源要求重定向；本轮不自动跟随，以保持请求与域名预算") from exc
                self._status(exc.code, exc.headers, domain)
                raise

    def permission(self, url):
        parsed = urllib.parse.urlparse(url)
        origin = parsed.scheme + "://" + parsed.netloc
        entry = self.persistent_robots.get(origin)
        fetched = _dt(entry.get("fetched_at")) if entry and entry.get("fetched_at") else None
        fresh = fetched and 0 <= (self.now - fetched).total_seconds() < 86400
        if not fresh:
            try:
                body, _ = self.raw(origin + "/robots.txt")
                rules = body.decode("utf-8", errors="replace").splitlines()
                entry = {"fetched_at": self.now.isoformat(timespec="seconds"), "rules": rules, "status": "available"}
            except RateLimited:
                raise
            except Exception as exc:
                if "HTTP 404" in str(exc) or "HTTP 410" in str(exc):
                    entry = {"fetched_at": self.now.isoformat(timespec="seconds"), "rules": [], "status": "not_published"}
                else:
                    raise SourceUnavailable("robots.txt无法核验，未继续请求报价：" + _text(exc, 140)) from exc
            self.persistent_robots[origin] = entry
        parser = urllib.robotparser.RobotFileParser(origin + "/robots.txt")
        parser.parse(entry.get("rules", []))
        if not parser.can_fetch(self.cfg["user_agent"], url):
            raise SourceUnavailable("robots.txt不允许此快照路径；未发起行情请求")
        delay = parser.crawl_delay(self.cfg["user_agent"]) or parser.crawl_delay("*")
        if delay and delay > 10:
            raise SourceUnavailable("来源crawl-delay超过本轮允许等待时长，暂缓采集")
        if delay:
            self.cfg["min_request_interval_seconds"] = max(float(self.cfg.get("min_request_interval_seconds", 2)), float(delay))


def _source_time(value, now):
    try:
        raw = str(value or "").strip()
        if re.fullmatch(r"\d{14}", raw):
            parsed = datetime.strptime(raw, "%Y%m%d%H%M%S").replace(tzinfo=now.tzinfo)
        else:
            parsed = datetime.fromisoformat(raw).replace(tzinfo=now.tzinfo) if re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", raw) else None
        if parsed is None or parsed.year < 2000 or parsed > now + timedelta(seconds=5):
            return None
        return parsed.isoformat(timespec="seconds")
    except (ValueError, TypeError):
        return None


def parse_tencent(payload, now):
    """Parse without eval. Preserve source volume units instead of guessing lots."""
    rows = {}
    for symbol, body in re.findall(r'v_((?:sh|sz|bj)\d{6})\s*=\s*"([^"]*)"\s*;', payload):
        fields = body.split("~")
        if len(fields) < 31 or fields[2] != symbol[2:]:
            continue
        price = _number(fields[3])
        if price is None or price <= 0:
            continue
        previous = _number(fields[4])
        stamp = _source_time(fields[30], now)
        rows[symbol] = {"name": fields[1], "price": price, "previous_close": previous,
                        "open": _number(fields[5]), "volume": _number(fields[6]),
                        "volume_unit": "provider_units_unverified", "volume_note": "来源原始累计成交量，未核实所有指数/ETF量纲，不跨源直接相加",
                        "change_pct": (price / previous - 1) * 100 if previous and previous > 0 else None,
                        "quote_timestamp": stamp, "timestamp_quality": "source_reported" if stamp else "retrieved_only"}
    return rows


def parse_sina(payload, now):
    rows = {}
    for symbol, body in re.findall(r'(?:var\s+)?hq_str_((?:sh|sz|bj)\d{6})\s*=\s*"([^"]*)"\s*;', payload):
        fields = body.split(",")
        if len(fields) < 10:
            continue
        price = _number(fields[3])
        if price is None or price <= 0:
            continue
        previous = _number(fields[2])
        stamp = _source_time(fields[30] + "T" + fields[31], now) if len(fields) > 31 else None
        rows[symbol] = {"name": fields[0], "price": price, "previous_close": previous,
                        "open": _number(fields[1]), "volume": _number(fields[8]), "amount": _number(fields[9]),
                        "volume_unit": "provider_units_unverified", "volume_note": "原始累计成交量；个股/ETF通常为股/份，指数汇总口径未统一",
                        "amount_unit": "CNY", "change_pct": (price / previous - 1) * 100 if previous and previous > 0 else None,
                        "quote_timestamp": stamp, "timestamp_quality": "source_reported" if stamp else "retrieved_only"}
    return rows


def _stock_symbol(code):
    if re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", str(code or "").upper()):
        raw = str(code).lower()
        return raw[-2:] + raw[:6]
    code = str(code or "").lower().replace(".", "")
    if re.fullmatch(r"(?:sh|sz|bj)\d{6}", code):
        return code
    if not re.fullmatch(r"\d{6}", code):
        return None
    return ("sh" if code.startswith(("5", "6", "9")) else "bj" if code.startswith(("4", "8")) else "sz") + code


def _targets(data, max_stocks):
    mapping = {}

    def add(symbol, entity_id, obj, basis):
        if symbol and re.fullmatch(r"(?:sh|sz|bj)\d{6}", symbol):
            mapping.setdefault(symbol, []).append({"entity_id": entity_id, "object": obj, "basis": basis})
    for item in data.get("market", []):
        add(item.get("id"), item.get("id"), item, "index")
    stocks = {}
    for sector in data.get("sectors", []):
        proxy = sector.get("proxy")
        if isinstance(proxy, dict) and proxy.get("code"):
            symbol = proxy.get("symbol") or _stock_symbol(proxy["code"])
            add(symbol, f"{sector['id']}:proxy:{proxy['code']}", proxy, "etf")
        for stock in sector.get("stocks", []):
            symbol = _stock_symbol(stock.get("code"))
            if symbol:
                stocks.setdefault(symbol, []).append(stock)
    for symbol, objects in list(stocks.items())[:max_stocks]:
        for item in objects:
            add(symbol, "stock:" + symbol, item, "stock")
    return mapping


def _trim_points(points, maximum):
    days = sorted({p["quote_timestamp"][:10] for p in points if p.get("quote_timestamp")})[-5:]
    keep_days = set(days)
    kept = [p for p in points if p.get("quote_timestamp") and p["quote_timestamp"][:10] in keep_days]
    unique = {(p["provider"], p["quote_timestamp"]): p for p in kept}
    return sorted(unique.values(), key=lambda p: (p["quote_timestamp"], p["provider"]))[-maximum:]


def _refresh_freshness(data, now):
    """Freshness is a current view, never a permanently cached quote property."""
    session = market_session(now)
    continuous = session["phase"] in {"morning", "afternoon", "opening_auction", "auction_complete", "closing_auction"}
    quotes = []
    # Full data refreshes can rebuild entity dictionaries while retaining the
    # durable intraday series. Restore the cached view before a cache/offline exit.
    stock_count = sum(len(s.get("stocks", [])) for s in data.get("sectors", []))
    for targets in _targets(data, stock_count).values():
        for target in targets:
            latest = data.get("intraday", {}).get(target["entity_id"], {}).get("latest")
            current = target["object"].get("live_quote") or {}
            if isinstance(latest, dict) and str(latest.get("quote_timestamp") or "") > str(current.get("quote_timestamp") or ""):
                target["object"]["live_quote"] = latest
    for entry in data.get("intraday", {}).values():
        quotes.extend(entry.get("providers", {}).values())
        if isinstance(entry.get("latest"), dict):
            quotes.append(entry["latest"])
    for entity in data.get("market", []) + data.get("sectors", []):
        objects = [entity] + entity.get("stocks", [])
        if isinstance(entity.get("proxy"), dict):
            objects.append(entity["proxy"])
        quotes.extend(obj["live_quote"] for obj in objects if isinstance(obj.get("live_quote"), dict))
    for quote in quotes:
        timestamp = _dt(quote.get("quote_timestamp")) if quote.get("quote_timestamp") else None
        age = (now - timestamp).total_seconds() if timestamp else None
        quote["age_seconds"] = age
        quote["is_live"] = bool(timestamp and quote.get("timestamp_quality") in {"source_reported", "exchange_timestamp"}
                                and continuous and timestamp.date() == now.date() and 0 <= age <= 300)


def collect_intraday(data, config=None, now=None):
    """Collect at most two HTTP requests, including robots; preserve daily fields.

    data.intraday_state and data.intraday must be carried across scheduler runs.
    Raw points are collected snapshots, not a complete reconstructed minute tape.
    """
    started = time.monotonic()
    result = copy.deepcopy(data)
    now = _dt(now)
    if now is None:
        raise ValueError("无效now")

    def response_clock():
        # Advance the caller's clock only by measured elapsed time. Never replace
        # a supplied historical/test clock with the executor's wall-clock date.
        return now + timedelta(seconds=max(0, time.monotonic() - started))

    _refresh_freshness(result, now)
    if not (config or {}).get("network_enabled", True):
        return result
    opts = {"enabled": True, "cache_seconds": 60, "max_points": 2000, "max_constituent_stocks": 20,
            "providers": ["tencent", "sina"], "request_budget_seconds": 35}
    opts.update((config or {}).get("intraday", {}))
    if not opts["enabled"]:
        return result
    state = result.setdefault("intraday_state", {})
    last = _dt(state.get("last_attempt_at")) if state.get("last_attempt_at") else None
    if last and 0 <= (now - last).total_seconds() < max(60, float(opts["cache_seconds"])):
        state["cache_hit"] = True
        return result
    state.update(last_attempt_at=now.isoformat(timespec="seconds"), cache_hit=False)
    attempts = state.setdefault("provider_attempts", {})
    robots = state.setdefault("robots", {})
    series = result.setdefault("intraday", {})
    targets = _targets(result, max(0, min(30, int(opts["max_constituent_stocks"]))))
    if (config or {}).get("primary_provider") == "stock_api":
        def needs_backup(target):
            q = target["object"].get("live_quote") or {}
            stamp = _dt(q.get("quote_timestamp")) if q.get("quote_timestamp") else None
            if q.get("provider") != "stock_api" or stamp is None or stamp.date() != now.date() or stamp > now:
                return True
            return not ((now - stamp).total_seconds() <= 300 or (now.hour >= 15 and stamp.hour >= 15))
        targets = {symbol: [t for t in rows if needs_backup(t)] for symbol, rows in targets.items()}
        targets = {symbol: rows for symbol, rows in targets.items() if rows}
    if not targets:
        state["message"] = "尚无已确认的指数/ETF/成分股代码"
        return result
    cfg = json.loads((ROOT / "config/sources.json").read_text())
    cfg.update(max_http_requests=2, max_retries=0, request_budget_seconds=opts["request_budget_seconds"],
               respect_robots=True, min_request_interval_seconds=max(2, float(cfg.get("min_request_interval_seconds", 2))))
    transport = _BatchTransport(cfg, robots, now)
    sources = {s["id"]: s for s in result.setdefault("sources", [])}
    observations = list(result.get("price_observations", []))
    providers = sorted([p for p in opts["providers"] if p in PROVIDERS], key=lambda p: attempts.get(p, ""))
    for provider in providers:
        sid = provider + "_intraday"
        source = sources.setdefault(sid, {"id": sid, "name": PROVIDERS[provider]["name"], "type": "intraday", "url": PROVIDERS[provider]["url"], "status": "pending", "last_success": None})
        retry = _dt(source.get("retry_after")) if source.get("retry_after") else None
        if retry and retry > now:
            continue
        if transport.count >= 2:
            source["message"] = "本轮两次HTTP预算已用完；下一轮继续，含robots冷启动探测"
            continue
        attempts[provider] = now.isoformat(timespec="seconds")
        url = PROVIDERS[provider]["url"] + ",".join(targets)
        try:
            transport.permission(url)
            body, _ = transport.raw(url, {"Referer": PROVIDERS[provider]["referer"]})
            response_now = response_clock()
            payload = body.decode("gb18030", errors="replace")
            quotes = (parse_tencent if provider == "tencent" else parse_sina)(payload, response_now)
            if not quotes:
                raise SourceUnavailable("快照为空或字段结构不符，保留原始缓存")
            session = market_session(response_now)
            continuous = session["phase"] in {"morning", "afternoon", "opening_auction", "auction_complete", "closing_auction"}
            accepted = 0
            for symbol, quote in quotes.items():
                for target in targets.get(symbol, []):
                    identity = target["entity_id"]
                    q = copy.deepcopy(quote)
                    timestamp = q["quote_timestamp"]
                    market_time = _dt(timestamp) if timestamp else None
                    age = (response_now - market_time).total_seconds() if market_time else None
                    q.update(entity_id=identity, symbol=symbol, basis=target["basis"], provider=provider,
                             upstream_provider=provider, fetched_at=response_now.isoformat(timespec="seconds"),
                             timestamp=timestamp, trade_date=timestamp[:10] if timestamp else None,
                             is_live=bool(market_time and continuous and market_time.date() == response_now.date() and 0 <= age <= 300),
                             age_seconds=age, pe_ttm=None, pe_dynamic=None, pe_static=None,
                             pe_note="该公开快照的PE口径未获提供方确认，保留为空；ETF/指数的零值不构成板块估值")
                    entry = series.setdefault(identity, {"entity_id": identity, "name": target["object"].get("name", quote["name"]), "symbol": symbol, "basis": target["basis"], "points": []})
                    entry.setdefault("providers", {})[provider] = q
                    latest = entry.get("latest")
                    if latest is None or (timestamp or "") >= (latest.get("quote_timestamp") or ""):
                        entry["latest"] = q
                        target["object"]["live_quote"] = q
                    if timestamp:
                        entry["points"] = _trim_points(entry.get("points", []) + [q], min(2000, max(1, int(opts["max_points"]))))
                        observations.append({"entity_id": identity, "provider": provider, "upstream_provider": provider,
                                             "field": "last", "trade_date": timestamp[:10], "price": q["price"], "adjustment": "none",
                                             "observed_at": q["fetched_at"], "market_timestamp": timestamp, "timestamp_quality": "source_reported"})
                    entry["note"] = "自开始采集以来的实际快照；不补造缺失时点，不代表完整分钟行情。"
                    accepted += 1
            if not accepted:
                raise SourceUnavailable("来源未返回任何请求标的的快照，保留缓存")
            source.update(status="success", last_success=response_now.isoformat(timespec="seconds"), retry_after=None,
                          message=f"取得{accepted}个实体快照；是否实时按来源时间与当前交易时段判断")
        except Exception as exc:
            response_now = response_clock()
            source.update(status="stale" if source.get("last_success") else "error", last_attempt=response_now.isoformat(timespec="seconds"), message=_text(exc, 240))
            # Network/permission failure is cooled down; quota exhaustion is only deferred.
            if "预算" not in str(exc):
                seconds = getattr(exc, "retry_seconds", 900)
                source["retry_after"] = (response_now + timedelta(seconds=seconds)).isoformat(timespec="seconds")
    # One latest live observation per entity/upstream/date; daily evidence remains separate.
    deduped = {}
    for observation in observations:
        key = tuple(observation.get(k) for k in ("entity_id", "provider", "field", "trade_date", "adjustment"))
        old = deduped.get(key)
        if old is None or str(observation.get("market_timestamp") or observation.get("observed_at") or "") >= str(old.get("market_timestamp") or old.get("observed_at") or ""):
            deduped[key] = observation
    result["price_observations"] = sorted(deduped.values(), key=lambda o: (o.get("trade_date") or "", o.get("observed_at") or ""))[-1000:]
    result["sources"] = list(sources.values())
    state.update(http_requests=transport.count, request_limit=2, requested_symbols=len(targets),
                 message="最多2次HTTP/轮（含robots）；固定UA、60秒缓存；来源禁令与429不绕过")
    completed_at = response_clock()
    state["completed_at"] = completed_at.isoformat(timespec="seconds")
    _refresh_freshness(result, completed_at)
    return result
