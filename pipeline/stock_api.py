"""Bounded, credential-safe daily supplements from a Tushare-compatible service.

The compatible endpoint's upstream is unknown. Its calendar is an observation,
not authority to change the application's published exchange-calendar rules.
Vendor valuation ratios never enter the parent-attributable financial aggregates.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time as clock, timedelta
from email.utils import parsedate_to_datetime

from pipeline.calendar import SHANGHAI, is_trading_day, previous_trading_day, shanghai_now

ENDPOINT = "https://t.xiaodefa.top/"
MAX_BYTES = 4 * 1024 * 1024
FIELDS = {
    "trade_cal": "exchange,cal_date,is_open,pretrade_date",
    "index_daily": "ts_code,trade_date,close,open,high,low,pre_close,change,pct_chg,vol,amount",
    "daily_basic": "ts_code,trade_date,close,total_mv,pe_ttm,pb",
}
PE_DEFINITION = "供应商PE TTM：总市值/净利润TTM；公开定义未明确归母口径，亏损时为空；不用于归母聚合"
PB_DEFINITION = "供应商PB：总市值/（净资产减其他权益工具）；不等同已核实归母权益，不用于归母聚合"


class _Failure(Exception):
    """Only fixed local codes may cross the transport boundary."""

    def __init__(self, code, retry_seconds=0):
        super().__init__(code)
        self.code = code
        self.retry_seconds = retry_seconds


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _Failure("redirect_blocked", 3600)


def _instant(value):
    try:
        if isinstance(value, datetime):
            return shanghai_now(value)
        if not isinstance(value, str) or not value:
            return None
        return shanghai_now(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (TypeError, ValueError, OverflowError):
        return None


def _date(value, compact=False):
    if not isinstance(value, str) or not re.fullmatch(r"\d{8}" if compact else r"\d{4}-\d{2}-\d{2}", value):
        raise _Failure("invalid_date")
    try:
        return datetime.strptime(value, "%Y%m%d").date() if compact else date.fromisoformat(value)
    except ValueError:
        raise _Failure("invalid_date") from None


def _number(value, nullable=False, minimum=None):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _Failure("invalid_number")
    if not math.isfinite(value) or (minimum is not None and value < minimum):
        raise _Failure("invalid_number")
    return float(value)


def _bounded(value, default, lower, upper):
    try:
        number = float(value)
        return min(upper, max(lower, number)) if math.isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _endpoint(cfg):
    value = cfg.get("endpoint", cfg.get("base_url", cfg.get("url", ENDPOINT)))
    if not isinstance(value, str) or value != ENDPOINT:
        raise _Failure("invalid_endpoint")
    parsed = urllib.parse.urlsplit(value)
    if (value != ENDPOINT or parsed.scheme != "https" or parsed.netloc != "t.xiaodefa.top"
            or parsed.path != "/" or parsed.query or parsed.fragment):
        raise _Failure("invalid_endpoint")
    return ENDPOINT


def _retry_after(headers, now):
    raw = headers.get("Retry-After", "") if headers else ""
    try:
        seconds = float(raw)
        if math.isfinite(seconds) and seconds >= 0:
            # A deliberately conservative horizon handles unrepresentable values.
            return max(60, min(seconds, 315360000))
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        until = parsedate_to_datetime(raw)
        until = until.replace(tzinfo=SHANGHAI) if until.tzinfo is None else until
        return max(60, min((until - now).total_seconds(), 315360000))
    except (TypeError, ValueError, OverflowError):
        return 3600


def _reject_constant(_value):
    raise _Failure("invalid_number")


def _rows(payload, api):
    if not isinstance(payload, dict) or type(payload.get("code")) is not int:
        raise _Failure("invalid_payload")
    code = payload["code"]
    if code != 0:
        raise _Failure("api_denied", 86400 if code in (-2001, -2002, 2002, 2003, 401, 403) else 3600)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise _Failure("invalid_payload")
    fields, items = data.get("fields"), data.get("items")
    expected = FIELDS[api].split(",")
    if (not isinstance(fields, list) or not all(isinstance(f, str) for f in fields)
            or len(fields) != len(expected) or set(fields) != set(expected)
            or not isinstance(items, list) or len(items) > 15000):
        raise _Failure("invalid_payload")
    if not all(isinstance(row, list) and len(row) == len(fields) for row in items):
        raise _Failure("invalid_payload")
    return [dict(zip(fields, row)) for row in items]


class _Client:
    def __init__(self, token, cfg, now):
        self._token = token
        self.url = _endpoint(cfg)
        self.now = now
        self.max_requests = int(_bounded(cfg.get("max_requests"), 4, 0, 4))
        self.interval = _bounded(cfg.get("min_interval_seconds"), 3, 3, 50)
        self.budget = _bounded(cfg.get("budget_seconds", cfg.get("request_budget_seconds")), 50, 0, 50)
        self.timeout = _bounded(cfg.get("timeout_seconds"), 10, .1, 15)
        self.started = time.monotonic()
        self.last = None
        self.count = 0
        self.opener = urllib.request.build_opener(_NoRedirect())

    def remaining(self):
        return self.budget - (time.monotonic() - self.started)

    def request(self, api, params):
        if self.count >= self.max_requests or self.remaining() <= 0:
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
                           "fields": FIELDS[api]}, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": "A-share-radar/daily-supplement"})
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
                    raise _Failure("response_too_large")
                raw = response.read(MAX_BYTES + 1)
                if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
                    raise _Failure("response_too_large")
                if self.remaining() <= 0:
                    raise _Failure("budget_exhausted")
            return _rows(json.loads(raw.decode("utf-8"), parse_constant=_reject_constant), api)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise _Failure("rate_limited", _retry_after(exc.headers, self.now)) from None
            if exc.code in (401, 403):
                raise _Failure("authorization_denied", 86400) from None
            if 300 <= exc.code < 400:
                raise _Failure("redirect_blocked", 3600) from None
            raise _Failure("http_error", 3600) from None
        except _Failure:
            raise
        except (TimeoutError, urllib.error.URLError, OSError):
            raise _Failure("transport_error", 900) from None
        except Exception:
            # Never stringify an exception, request, URL, response body or message.
            raise _Failure("invalid_payload", 900) from None


def _closed_day(now):
    if is_trading_day(now.date()) is True and now.time().replace(tzinfo=None) >= clock(18, 30):
        return now.date()
    return previous_trading_day(now.date())


def _index_code(item):
    raw = item.get("id")
    if item.get("basis") == "etf" or item.get("kind") == "etf":
        return None
    if not isinstance(raw, str) or not re.fullmatch(r"(?:sh000\d{3}|sz399\d{3})", raw):
        return None
    return raw[2:] + (".SH" if raw.startswith("sh") else ".SZ")


def _stock_code(raw):
    if not isinstance(raw, str):
        return None
    if re.fullmatch(r"(?:600|601|603|605|688|689)\d{3}(?:\.SH)?", raw):
        return raw[:6] + ".SH"
    if re.fullmatch(r"(?:000|001|002|003|300|301)\d{3}(?:\.SZ)?", raw):
        return raw[:6] + ".SZ"
    if re.fullmatch(r"(?:[48]\d{5}|920\d{3})(?:\.BJ)?", raw):
        return raw[:6] + ".BJ"
    return None


def _calendar(rows, start, end):
    records, seen = [], set()
    for row in rows:
        day = _date(row["cal_date"], compact=True)
        if row["exchange"] != "SSE" or day < start or day > end or day in seen:
            raise _Failure("invalid_calendar")
        opened = row["is_open"]
        if type(opened) not in (str, int) or opened not in (0, 1, "0", "1"):
            raise _Failure("invalid_calendar")
        previous = row["pretrade_date"]
        previous = _date(previous, compact=True) if previous not in (None, "") else None
        if previous and previous >= day:
            raise _Failure("invalid_calendar")
        seen.add(day)
        records.append({"exchange": "SSE", "cal_date": day.isoformat(), "is_open": int(opened),
                        "pretrade_date": previous.isoformat() if previous else None,
                        "matches_official_calendar": is_trading_day(day) == bool(int(opened)) if is_trading_day(day) is not None else None})
    if not records:
        raise _Failure("empty_response")
    return sorted(records, key=lambda row: row["cal_date"])


def _index_bars(rows, code, start, cutoff, stamp):
    bars, seen = [], set()
    for row in rows:
        day = _date(row["trade_date"], compact=True)
        if row["ts_code"] != code or day in seen:
            raise _Failure("invalid_code" if row["ts_code"] != code else "duplicate_date")
        seen.add(day)
        values = {key: _number(row[key], minimum=0 if key in ("close", "open", "high", "low", "pre_close", "vol", "amount") else None)
                  for key in ("close", "open", "high", "low", "pre_close", "change", "pct_chg", "vol", "amount")}
        if (min(values[k] for k in ("close", "open", "high", "low")) <= 0
                or values["low"] > min(values["open"], values["close"])
                or values["high"] < max(values["open"], values["close"])):
            raise _Failure("invalid_bar")
        # Future/unfinished and non-trading-day rows can never update a daily quote.
        if day < start or day > cutoff or is_trading_day(day) is not True:
            continue
        bars.append({"date": day.isoformat(), "open": values["open"], "high": values["high"],
                     "low": values["low"], "close": values["close"], "change_pct": values["pct_chg"],
                     "volume": values["vol"], "amount": values["amount"], "volume_unit": "lot",
                     "volume_unit_label": "手", "amount_unit": "CNY_thousand", "amount_unit_label": "千元",
                     "source": "stock_api", "provider": "stock_api", "upstream_provider": "unknown",
                     "adjustment": "unadjusted", "complete": True, "fetched_at": stamp,
                     "as_of": day.isoformat() + "T15:00:00+08:00", "timestamp_quality": "daily_close"})
    if not bars:
        raise _Failure("empty_completed_bars")
    return sorted(bars, key=lambda bar: bar["date"])


def _basic_records(rows, cutoff, stamp):
    records, seen = [], set()
    for row in rows:
        code = _stock_code(row["ts_code"])
        day = _date(row["trade_date"], compact=True)
        if code is None or code != row["ts_code"] or code in seen:
            raise _Failure("invalid_code")
        if day != cutoff:
            raise _Failure("date_mismatch")
        seen.add(code)
        close = _number(row["close"], minimum=0)
        total_mv = _number(row["total_mv"], nullable=True, minimum=0)
        pe = _number(row["pe_ttm"], nullable=True)
        pe = pe if pe is not None and pe > 0 else None
        pb = _number(row["pb"], nullable=True)
        cap = total_mv * 10000 if total_mv is not None else None
        if close <= 0 or (cap is not None and not math.isfinite(cap)):
            raise _Failure("invalid_number")
        records.append({"ts_code": code, "trade_date": day.isoformat(), "close": close,
                        "total_mv": total_mv, "total_mv_unit": "CNY_10000", "market_cap_cny": cap,
                        "vendor_pe_ttm": pe, "vendor_pb": pb, "vendor_pe_ttm_definition": PE_DEFINITION,
                        "vendor_pb_definition": PB_DEFINITION, "provider": "stock_api", "upstream_provider": "unknown",
                        "fetched_at": stamp, "as_of": day.isoformat() + "T15:00:00+08:00",
                        "timestamp_quality": "daily_close"})
    if not records:
        raise _Failure("empty_response")
    return records


def _apply_index(result, item, bars, stamp):
    sources = item.setdefault("source_history", {})
    if not isinstance(sources, dict):
        sources = item["source_history"] = {}
    cache = {b["date"]: b for b in sources.get("stock_api", []) if isinstance(b, dict) and "date" in b}
    cache.update({b["date"]: b for b in bars})
    sources["stock_api"] = sorted(cache.values(), key=lambda row: row["date"])[-900:]
    history = {b["date"]: b for b in item.get("history", []) if isinstance(b, dict) and "date" in b}
    for bar in bars:
        old = history.get(bar["date"])
        if old is None or old.get("close") is None or old.get("source") == "stock_api":
            history[bar["date"]] = bar
    item["history"] = sorted(history.values(), key=lambda row: row["date"])[-900:]
    result.setdefault("history", {})[item["id"]] = item["history"]
    latest = bars[-1]
    old_day = item.get("quote_date") or str(item.get("as_of") or "")[:10]
    try:
        old_day = _date(old_day).isoformat()
    except _Failure:
        old_day = None
    if ((old_day and latest["date"] > old_day)
            or (item.get("price") is None and (not old_day or latest["date"] == old_day))):
        item.update(price=latest["close"], open=latest["open"], change_pct=latest["change_pct"],
                    quote_date=latest["date"], as_of=latest["as_of"], fetched_at=stamp,
                    provider="stock_api", source="stock_api", upstream_provider="unknown", source_url=ENDPOINT,
                    timestamp_quality="daily_close", price_label="最近日K收盘点位", adjustment="unadjusted",
                    stale=latest["date"] != stamp[:10], history_updated_at=stamp)
    observations = result.setdefault("price_observations", [])
    keyed = {(o.get("entity_id"), o.get("provider"), o.get("field"), o.get("trade_date"), o.get("adjustment")): o
             for o in observations if isinstance(o, dict)}
    for bar in bars[-2:]:
        observation = {"entity_id": item["id"], "provider": "stock_api", "upstream_provider": "unknown",
                       "field": "close", "trade_date": bar["date"], "price": bar["close"], "adjustment": "none",
                       "observed_at": stamp, "market_timestamp": bar["as_of"], "timestamp_quality": "daily_close"}
        keyed[(item["id"], "stock_api", "close", bar["date"], "none")] = observation
    result["price_observations"] = sorted(keyed.values(), key=lambda row: (row.get("trade_date", ""), row.get("observed_at", "")))[-600:]


def _apply_basic(stocks, cache, cutoff):
    if cache.get("trade_date") != cutoff.isoformat():
        return 0
    lookup = {row["ts_code"]: row for row in cache.get("records", [])}
    count = 0
    for stock in stocks:
        row = lookup.get(_stock_code(stock.get("code") or stock.get("ts_code")))
        if row is None:
            continue
        stock["stock_api_daily_basic"] = copy.deepcopy(row)
        # A daily close is intentionally separate from the original live price/time.
        stock.update(vendor_pe_ttm=row["vendor_pe_ttm"], vendor_pb=row["vendor_pb"],
                     vendor_valuation_date=row["trade_date"], vendor_pe_ttm_definition=PE_DEFINITION,
                     vendor_pb_definition=PB_DEFINITION)
        existing_day = stock.get("market_cap_date") or stock.get("quote_date") or str(stock.get("as_of") or "")[:10]
        if (stock.get("market_cap") is None and row["market_cap_cny"] is not None
                and (not existing_day or existing_day == row["trade_date"])):
            stock.update(market_cap=row["market_cap_cny"], market_cap_date=row["trade_date"],
                         market_cap_unit="CNY", market_cap_currency="CNY", market_cap_basis="total",
                         market_cap_provider="stock_api", market_cap_available_at=row["fetched_at"])
        count += 1
    return count


def enrich_with_stock_api(data, config=None, now=None):
    """Return a deep copy; at most four POSTs, no retries and no public credentials.

    Only the STOCK_API_TOKEN environment variable provides credentials. Endpoint,
    request and runtime overrides are constrained to the built-in safety ceilings.
    Run this adapter only in the full collection path, not during quote polling.
    """
    result = copy.deepcopy(data)
    config = config or {}
    cfg = config.get("stock_api", {})
    cfg = cfg if isinstance(cfg, dict) else {}
    now = _instant(now) if now is not None else shanghai_now()
    if now is None:
        raise ValueError("invalid_now")
    stamp = now.isoformat(timespec="seconds")
    old_state = result.get("stock_api_state", {})
    old_state = old_state if isinstance(old_state, dict) else {}
    state = {key: copy.deepcopy(old_state[key]) for key in
             ("calendar", "daily_basic", "index_cursor", "index_requests", "cooldown_until") if key in old_state}
    state.update(version=1, last_run_at=stamp, requests_this_run=0)
    result["stock_api_state"] = state
    states = {row["id"]: row for row in result.setdefault("sources", []) if isinstance(row, dict) and "id" in row}
    last_success = states.get("stock_api", {}).get("last_success")
    source = {"id": "stock_api", "name": "股票日线与估值补充", "type": "daily_supplement", "url": ENDPOINT,
              "provider": "stock_api", "upstream_provider": "unknown", "status": "pending", "last_success": last_success,
              "message": "等待日线数据补充；兼容服务的上游来源未知"}
    states["stock_api"] = source
    result["sources"] = list(states.values())
    stocks = [stock for sector in result.get("sectors", []) for stock in sector.get("stocks", [])
              if isinstance(stock, dict) and _stock_code(stock.get("code") or stock.get("ts_code"))]
    state["financial_status"] = "pending_constituents" if not stocks else "vendor_ratios_only"
    state["financial_message"] = "待成分：未发现可用股票代码，跳过全市场估值请求" if not stocks else "供应商估值仅作日线补充，归母财务待补充"
    token = os.environ.get("STOCK_API_TOKEN", "")
    if not token or not token.strip():
        source.update(status="pending", error_code="not_configured", message="尚未配置股票数据凭据，保留现有数据")
        return result
    if not config.get("network_enabled", True) or not cfg.get("enabled", True):
        source.update(status="pending", error_code="offline", message="离线或未启用股票数据补充，保留现有数据")
        return result
    until = _instant(state.get("cooldown_until"))
    if until and until > now:
        source.update(status="cooldown", retry_after=until.isoformat(timespec="seconds"), message="股票数据服务冷却中，保留现有数据")
        return result
    cutoff = _closed_day(now)
    if cutoff is None:
        source.update(error_code="calendar_unverified", message="缺少已核实交易日历，暂停股票日线请求")
        return result
    state["target_trade_date"] = cutoff.isoformat()
    try:
        client = _Client(token, cfg, now)
    except _Failure as exc:
        source.update(status="error", error_code=exc.code, message="股票数据服务地址无效，未发起请求")
        return result
    succeeded, stopped = [], False

    def attempt(api, params, consume):
        nonlocal stopped
        if stopped:
            return False
        try:
            rows = client.request(api, params)
            consume(rows)
            succeeded.append(api)
            return True
        except _Failure as exc:
            state["last_error_code"] = exc.code
            source.update(error_code=exc.code, last_attempt=stamp)
            delay = exc.retry_seconds or (900 if exc.code != "budget_exhausted" else 0)
            if delay:
                retry = (now + timedelta(seconds=delay)).isoformat(timespec="seconds")
                state["cooldown_until"] = retry
                source["retry_after"] = retry
            source.update(status="cooldown" if delay else "pending", message="股票日线补充暂不可用，保留现有数据")
            stopped = True
            return False
        except Exception:
            # Also contain unexpected parsing errors without exposing response text.
            source.update(status="error", error_code="invalid_payload", message="股票数据格式校验未通过，保留现有数据")
            state["last_error_code"] = "invalid_payload"
            stopped = True
            return False
        finally:
            state["requests_this_run"] = client.count

    cal = state.get("calendar") or {}
    checked = _instant(cal.get("last_attempt") or cal.get("checked_at"))
    if checked is None or (now - checked).total_seconds() >= 86400:
        start, end = now.date() - timedelta(days=7), now.date() + timedelta(days=7)
        state["calendar"] = {**cal, "last_attempt": stamp}

        def save_calendar(rows):
            records = _calendar(rows, start, end)
            state["calendar"] = {"checked_at": stamp, "last_attempt": stamp, "records": records, "authority": "observation_only",
                                 "official_rules_unchanged": True, "start_date": start.isoformat(), "end_date": end.isoformat()}

        attempt("trade_cal", {"exchange": "SSE", "start_date": start.strftime("%Y%m%d"), "end_date": end.strftime("%Y%m%d")}, save_calendar)
    candidates = [item for item in result.get("market", []) if isinstance(item, dict) and _index_code(item)]
    attempts = state.setdefault("index_requests", {})
    cursor = int(_bounded(state.get("index_cursor"), 0, 0, 1000000)) % max(1, len(candidates))
    scheduled = 0
    for offset in range(len(candidates)):
        if stopped or scheduled >= 2:
            break
        position = (cursor + offset) % len(candidates)
        item = candidates[position]
        cached = attempts.get(item["id"], {})
        last_attempt = _instant(cached.get("last_attempt"))
        if cached.get("target_date") == cutoff.isoformat() and (cached.get("last_success") or (last_attempt and (now - last_attempt).total_seconds() < 21600)):
            continue
        if client.count >= client.max_requests:
            break
        previous_days = []
        for bar in item.get("source_history", {}).get("stock_api", []) + item.get("history", []):
            try:
                day = _date(bar.get("date"))
                if day <= cutoff:
                    previous_days.append(day)
            except _Failure:
                pass
        start = max(previous_days) - timedelta(days=7) if previous_days else cutoff - timedelta(days=365)
        attempts[item["id"]] = {"target_date": cutoff.isoformat(), "last_attempt": stamp}
        scheduled += 1
        state["index_cursor"] = (position + 1) % len(candidates)

        def save_index(rows):
            bars = _index_bars(rows, _index_code(item), start, cutoff, stamp)
            _apply_index(result, item, bars, stamp)
            if bars[-1]["date"] == cutoff.isoformat():
                attempts[item["id"]]["last_success"] = stamp
            attempts[item["id"]]["latest_trade_date"] = bars[-1]["date"]

        attempt("index_daily", {"ts_code": _index_code(item), "start_date": start.strftime("%Y%m%d"), "end_date": cutoff.strftime("%Y%m%d")}, save_index)
    basic = state.get("daily_basic") or {}
    if stocks and not stopped and basic.get("attempted_on") != cutoff.isoformat() and client.count < client.max_requests:
        basic["attempted_on"] = cutoff.isoformat()
        basic["last_attempt"] = stamp
        state["daily_basic"] = basic

        def save_basic(rows):
            records = _basic_records(rows, cutoff, stamp)
            basic.update(trade_date=cutoff.isoformat(), fetched_at=stamp, records=records, row_count=len(rows),
                         possibly_truncated=len(rows) >= 6000, full_market_request=True,
                         completeness="possibly_truncated" if len(rows) >= 6000 else "not_verified")

        attempt("daily_basic", {"trade_date": cutoff.strftime("%Y%m%d")}, save_basic)
    if stocks:
        state["matched_constituents"] = _apply_basic(stocks, basic, cutoff)
    if succeeded:
        source.update(status="stale" if stopped else "success", last_success=stamp,
                      message="已补充股票日线数据；日期来自已完成交易日，兼容服务上游未知" if not stopped else "部分日线补充成功，其他数据保留原缓存")
        if not stopped:
            state["cooldown_until"] = None
            source["retry_after"] = None
    elif not stopped:
        source.update(status="success" if last_success else "pending", message="使用已缓存股票日线；本轮无到期请求")
    return result
