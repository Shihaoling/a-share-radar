"""Bounded, credential-safe primary data from a Tushare-compatible service.

The compatible endpoint's upstream is unknown. Its calendar is an observation,
not authority to change the application's published exchange-calendar rules.
SW2021 identifiers define the industry universe; incompatible legacy sectors are
archived. Vendor ratios never enter the parent-attributable financial aggregates.
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
from pathlib import Path

from pipeline.calendar import SHANGHAI, is_trading_day, market_session, previous_trading_day, shanghai_now

ENDPOINT = "https://t.xiaodefa.top/"
MAX_BYTES = 4 * 1024 * 1024
MIN_HISTORY_BARS = 181
ROW_PARSER_VERSION = 2
FIELDS = {
    "trade_cal": "exchange,cal_date,is_open,pretrade_date",
    "index_daily": "ts_code,trade_date,close,open,high,low,pre_close,change,pct_chg,vol,amount",
    "daily_basic": "ts_code,trade_date,close,total_mv,pe_ttm,pb,turnover_rate,volume_ratio,circ_mv",
    "index_classify": "index_code,industry_name,parent_code,level,industry_code,is_pub,src",
    "sw_daily": "ts_code,trade_date,name,open,low,high,close,pct_change,vol,amount,pe,pb,float_mv,total_mv",
    "index_member_all": "l1_code,l1_name,l2_code,l2_name,l3_code,l3_name,ts_code,name,in_date,out_date,is_new",
    "rt_idx_k": "ts_code,name,trade_time,close,pre_close,high,open,low,vol,amount",
    "rt_sw_k": "ts_code,name,trade_time,close,pre_close,high,open,low,vol,amount",
    "news": "datetime,content,title,channels",
}
PE_DEFINITION = "供应商PE TTM：总市值/净利润TTM；公开定义未明确归母口径，亏损时为空；不用于归母聚合"
PB_DEFINITION = "供应商PB：总市值/（净资产减其他权益工具）；不等同已核实归母权益，不用于归母聚合"


class _Failure(Exception):
    """Only fixed local codes may cross the transport boundary."""

    def __init__(self, code, retry_seconds=0, *, provider_code=None, provider_reason=None):
        super().__init__(code)
        self.code = code
        self.retry_seconds = retry_seconds
        # Optional probe diagnostics never retain the untrusted provider message.
        self.provider_code = provider_code if type(provider_code) is int and abs(provider_code) <= 1000000 else None
        reasons = {'rate_limited', 'authorization_denied', 'points_insufficient',
                   'permission_denied', 'invalid_parameters', 'unsupported_api', 'provider_error'}
        self.provider_reason = provider_reason if provider_reason in reasons else None


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


def _provider_reason(payload):
    """Translate business errors to a fixed vocabulary, never echo provider text."""
    text = payload.get('msg')
    text = text[:2048].lower() if isinstance(text, str) else ''
    if payload.get('code') == 429 or any(word in text for word in
            ('频率', '频次', '访问次数', '每分钟最多', '每小时最多', '每天最多', 'rate limit', 'too many requests')):
        return 'rate_limited'
    if any(word in text for word in ('token无效', 'token 无效', 'token不正确', 'token不对',
                                    'invalid token', 'invalid api key', '认证失败')):
        return 'authorization_denied'
    if any(word in text for word in ('积分不足', '积分不够', '所需积分', '至少需要积分', 'insufficient points')):
        return 'points_insufficient'
    if any(word in text for word in ('没有访问该接口的权限', '没有权限', '无权限', '权限不足',
                                    '单独开通', 'permission denied', 'not authorized')):
        return 'permission_denied'
    if any(word in text for word in ('参数', 'invalid parameter', 'invalid argument')):
        return 'invalid_parameters'
    if any(word in text for word in ('接口不存在', '未知接口', '不支持该接口', 'api not found', 'unknown api')):
        return 'unsupported_api'
    return 'provider_error'


def _rows(payload, api):
    if not isinstance(payload, dict) or type(payload.get("code")) is not int:
        raise _Failure("invalid_payload")
    code = payload["code"]
    if code != 0:
        raise _Failure("api_denied", 86400 if code in (-2001, -2002, 2002, 2003, 401, 403) else 3600,
                       provider_code=code, provider_reason=_provider_reason(payload))
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
        self.max_requests = int(_bounded(cfg.get("max_requests"), 12, 0, 12))
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
    # SZSE: AVIC Chengfei moved from 300114 to 302132 on 2025-02-17.
    # This verified exception does not open an unverified 302xxx namespace.
    if raw in ("302132", "302132.SZ"):
        return "302132.SZ"
    if re.fullmatch(r"(?:600|601|603|605|688|689)\d{3}(?:\.SH)?", raw):
        return raw[:6] + ".SH"
    if re.fullmatch(r"(?:000|001|002|003|300|301)\d{3}(?:\.SZ)?", raw):
        return raw[:6] + ".SZ"
    if re.fullmatch(r"(?:[48]\d{5}|920\d{3})(?:\.BJ)?", raw):
        return raw[:6] + ".BJ"
    return None


def _quality(rows, quality=None):
    quality = quality if quality is not None else {}
    quality.update(received_count=len(rows), accepted_count=0, rejected_count=0, filtered_count=0,
                   duplicate_count=0, reason_counts={}, field_invalid_counts={}, rejection_sample=[])
    return quality


def _quality_note(quality, reason, code=None, field=None, kind="rejected"):
    if kind in ("rejected", "filtered", "duplicate"):
        quality[kind + "_count"] += 1
    quality["reason_counts"][reason] = quality["reason_counts"].get(reason, 0) + 1
    if kind == "field":
        quality["field_invalid_counts"][field] = quality["field_invalid_counts"].get(field, 0) + 1
    if len(quality["rejection_sample"]) < 10:
        sample = {"reason": reason}
        # Only a public identifier-shaped value is eligible for diagnostics.
        # Never retain an arbitrary response string, field value or message.
        if isinstance(code, str) and re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", code):
            sample["ts_code"] = code
        if field:
            sample["field"] = field
        quality["rejection_sample"].append(sample)


def _optional_number(value, field, quality, code, minimum=None):
    try:
        return _number(value, nullable=True, minimum=minimum)
    except (OverflowError, _Failure):
        _quality_note(quality, "invalid_optional_number", code, field, "field")
        return None


def _is_non_a_stock(code):
    return isinstance(code, str) and re.fullmatch(r"(?:900\d{3}\.SH|200\d{3}\.SZ)", code) is not None


def _dedupe_record(records, blocked, record, quality):
    code = record["ts_code"]
    if code in blocked:
        _quality_note(quality, "duplicate_conflict", code, "ts_code")
    elif code not in records:
        records[code] = record
    elif records[code] == record:
        _quality_note(quality, "identical_duplicate", code, "ts_code", "duplicate")
    else:
        del records[code]
        blocked.add(code)
        _quality_note(quality, "duplicate_conflict", code, "ts_code")
        _quality_note(quality, "duplicate_conflict", code, "ts_code")


def _finish_quality(quality, records):
    quality["accepted_count"] = len(records)
    quality["complete"] = bool(records) and not (quality["rejected_count"] or quality["field_invalid_counts"]
                                                   or quality["reason_counts"].get("out_of_scope_security"))


def _completed_dates(bars, cutoff):
    """Count distinct usable source bars, never trust a legacy completion flag."""
    days = set()
    for bar in bars:
        if not isinstance(bar, dict) or bar.get("complete") is False or bar.get("is_complete") is False:
            continue
        try:
            day = _date(bar.get("date"))
            close = _number(bar.get("close"), minimum=0)
        except _Failure:
            continue
        if close > 0 and day <= cutoff and is_trading_day(day) is True:
            days.add(day)
    return days


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


def _basic_records(rows, cutoff, stamp, quality=None):
    quality = _quality(rows, quality)
    records, blocked = {}, set()
    for row in rows:
        raw_code = row.get("ts_code")
        if _is_non_a_stock(raw_code):
            _quality_note(quality, "out_of_scope_security", raw_code, "ts_code", "filtered")
            continue
        try:
            code = _stock_code(raw_code)
            if code is None or code != raw_code:
                raise _Failure("invalid_code")
            day = _date(row["trade_date"], compact=True)
            if day != cutoff:
                raise _Failure("date_mismatch")
            close = _number(row["close"], minimum=0)
            if close <= 0:
                raise _Failure("invalid_close")
        except (OverflowError, _Failure) as exc:
            reason = exc.code if isinstance(exc, _Failure) else "invalid_number"
            _quality_note(quality, reason, raw_code, "ts_code" if reason == "invalid_code" else "trade_date" if reason in ("invalid_date", "date_mismatch") else "close")
            continue
        total_mv = _optional_number(row["total_mv"], "total_mv", quality, code, minimum=0)
        pe = _optional_number(row["pe_ttm"], "pe_ttm", quality, code)
        pe = pe if pe is not None and pe > 0 else None
        pb = _optional_number(row["pb"], "pb", quality, code)
        cap = total_mv * 10000 if total_mv is not None else None
        if cap is not None and not math.isfinite(cap):
            total_mv, cap = None, None
            _quality_note(quality, "invalid_optional_number", code, "total_mv", "field")
        record = {"ts_code": code, "trade_date": day.isoformat(), "close": close,
                        "total_mv": total_mv, "total_mv_unit": "CNY_10000", "market_cap_cny": cap,
                        "vendor_pe_ttm": pe, "vendor_pb": pb, "vendor_pe_ttm_definition": PE_DEFINITION,
                        "vendor_pb_definition": PB_DEFINITION, "provider": "stock_api", "upstream_provider": "unknown",
                        "fetched_at": stamp, "as_of": day.isoformat() + "T15:00:00+08:00",
                        "timestamp_quality": "daily_close"}
        record.update(turnover_rate=_optional_number(row.get("turnover_rate"), "turnover_rate", quality, code, minimum=0),
                      volume_ratio=_optional_number(row.get("volume_ratio"), "volume_ratio", quality, code, minimum=0),
                      circ_mv=_optional_number(row.get("circ_mv"), "circ_mv", quality, code, minimum=0),
                      date=day.isoformat(), available_at=stamp)
        _dedupe_record(records, blocked, record, quality)
    _finish_quality(quality, records)
    if not records:
        raise _Failure("no_valid_daily_basic_rows")
    return list(records.values())


def _apply_index(result, item, bars, stamp, primary=False):
    sources = item.setdefault("source_history", {})
    if not isinstance(sources, dict):
        sources = item["source_history"] = {}
    cache = {b["date"]: b for b in sources.get("stock_api", []) if isinstance(b, dict) and "date" in b}
    cache.update({b["date"]: b for b in bars})
    sources["stock_api"] = sorted(cache.values(), key=lambda row: row["date"])[-900:]
    history = {b["date"]: b for b in item.get("history", []) if isinstance(b, dict) and "date" in b}
    for bar in bars:
        old = history.get(bar["date"])
        if primary and old and old.get("source") not in (None, "stock_api"):
            old_source = old["source"]
            observations = sources.setdefault(old_source, [])
            observations[:] = [row for row in observations if row.get("date") != bar["date"]] + [copy.deepcopy(old)]
        if primary or old is None or old.get("close") is None or old.get("source") == "stock_api":
            history[bar["date"]] = bar
    item["history"] = sorted(history.values(), key=lambda row: row["date"])[-900:]
    result.setdefault("history", {})[item["id"]] = item["history"]
    latest = bars[-1]
    old_day = item.get("quote_date") or str(item.get("as_of") or "")[:10]
    try:
        old_day = _date(old_day).isoformat()
    except _Failure:
        old_day = None
    daily_quote = {"price": latest["close"], "change_pct": latest["change_pct"], "quote_date": latest["date"],
                   "as_of": latest["as_of"], "quote_timestamp": latest["as_of"], "timestamp_quality": "daily_close",
                   "provider": "stock_api", "upstream_provider": "unknown"}
    if primary and latest["date"] >= str(item.get("daily_quote", {}).get("quote_date") or ""):
        item["daily_quote"] = daily_quote
    if ((old_day and latest["date"] > old_day)
            or (primary and latest["date"] == old_day)
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


def _enrich_legacy(data, config=None, now=None):
    """Return a deep copy; at most four POSTs, no retries and no public credentials.

    Only the STOCK_API_TOKEN environment variable provides credentials. Endpoint,
    request and runtime overrides are constrained to the built-in safety ceilings.
    Run this adapter only in the full collection path, not during quote polling.
    """
    result = copy.deepcopy(data)
    config = config or {}
    cfg = config.get("stock_api", {})
    cfg = copy.deepcopy(cfg) if isinstance(cfg, dict) else {}
    cfg["max_requests"] = int(_bounded(cfg.get("max_requests"), 4, 0, 4))
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


def _clean_text(value, token, limit=200):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _Failure("invalid_text")
    value = value.replace(token, "[redacted]") if token else value
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", value)).strip()[:limit]


def _sw_code(value):
    return isinstance(value, str) and re.fullmatch(r"80\d{4}\.SI", value) is not None


def _classifications(rows, token):
    records, seen = [], set()
    for row in rows:
        code = row["index_code"]
        if not _sw_code(code) or code in seen or row["level"] != "L1" or row["src"] != "SW2021":
            raise _Failure("invalid_classification")
        if row["is_pub"] not in (1, "1"):
            continue
        name = _clean_text(row["industry_name"], token)
        if not name:
            raise _Failure("invalid_classification")
        seen.add(code)
        records.append({"index_code": code, "industry_name": name, "parent_code": _clean_text(row["parent_code"], token),
                        "industry_code": _clean_text(row["industry_code"], token), "is_pub": 1,
                        "level": "L1", "src": "SW2021"})
    if not records or len(records) > 31:
        raise _Failure("invalid_classification")
    return records


def _canonicalize(result, records):
    # Topic terms join only the stable official identifier; names never map
    # legacy boards onto the canonical industry universe.
    from pipeline.sentiment import SW2021

    existing = {row["id"]: row for row in result.get("sectors", []) if isinstance(row, dict) and "id" in row}
    archived = {row["id"]: row for row in result.get("archived_sectors", []) if isinstance(row, dict) and "id" in row}
    canonical = []
    for record in records:
        code = record["index_code"]
        previous = existing.get(code)
        item = previous if previous and previous.get("taxonomy") == "SW2021" else {}
        item.update(id=code, code=code, index_code=code, name=record["industry_name"],
                    taxonomy="SW2021", level="L1", basis="index", kind="industry", enabled=True,
                    price_kind="industry_index", price_label="申万行业指数", primary_provider="stock_api",
                    rank_basis="total_market_cap", keywords=list(SW2021.get(code[:6], (record["industry_name"], [record["industry_name"]]))[1]))
        for key in ("history", "market_cap_history", "stocks", "constituents", "drivers", "upstream", "downstream"):
            item.setdefault(key, [])
        for key in ("price", "change_pct", "market_cap", "as_of", "prediction"):
            item.setdefault(key, None)
        item.setdefault("membership_complete", False)
        # Version-2 caches briefly applied valuation observation time to the
        # ordinary historical OHLC record. Move only that exact legacy marker;
        # explicit price publication/revision constraints remain untouched.
        for bars in (item["history"], item.get("source_history", {}).get("stock_api", [])):
            for bar in bars:
                if (bar.get("api") == "sw_daily" and bar.get("available_at")
                        and bar.get("available_at") == bar.get("fetched_at") and not bar.get("price_available_at")):
                    bar.setdefault("valuation_available_at", bar["available_at"])
                    del bar["available_at"]
        result.setdefault("history", {})[code] = item["history"]
        canonical.append(item)
    canonical_ids = {item["id"] for item in canonical}
    for code, item in existing.items():
        if code not in canonical_ids:
            item["archived"] = True
            item["archive_reason"] = "迁移至申万2021一级行业体系；未按名称混合历史"
            archived[code] = item
    result["sectors"] = canonical
    result["archived_sectors"] = list(archived.values())


def _sw_bars(rows, allowed, start, end, stamp, token):
    grouped, seen = {}, set()
    for row in rows:
        code = row["ts_code"]
        if not isinstance(code, str) or not re.fullmatch(r"(?:80|85)\d{4}\.SI", code):
            raise _Failure("invalid_code")
        day = _date(row["trade_date"], compact=True)
        if (code, day) in seen:
            raise _Failure("duplicate_date")
        seen.add((code, day))
        if code not in allowed:
            continue
        values = {key: _number(row[key], minimum=0 if key in ("open", "high", "low", "close", "vol", "amount") else None)
                  for key in ("open", "high", "low", "close", "pct_change", "vol", "amount")}
        if (min(values[k] for k in ("open", "high", "low", "close")) <= 0
                or values["low"] > min(values["open"], values["close"])
                or values["high"] < max(values["open"], values["close"])):
            raise _Failure("invalid_bar")
        if not start <= day <= end or is_trading_day(day) is not True:
            continue
        total = _number(row["total_mv"], nullable=True, minimum=0)
        floating = _number(row["float_mv"], nullable=True, minimum=0)
        pe, pb = _number(row["pe"], nullable=True), _number(row["pb"], nullable=True)
        cap = total * 10000 if total is not None else None
        amount_cny = values["amount"] * 10000
        if (cap is not None and not math.isfinite(cap)) or not math.isfinite(amount_cny):
            raise _Failure("invalid_number")
        bar = {"date": day.isoformat(), "ts_code": code, "name": _clean_text(row["name"], token),
               "open": values["open"], "high": values["high"], "low": values["low"], "close": values["close"],
               "change_pct": values["pct_change"], "volume": values["vol"], "volume_unit": "万股",
               "amount": values["amount"], "amount_unit": "CNY_10000", "amount_cny": amount_cny,
               "vendor_pe": pe if pe and pe > 0 else None,
               "vendor_pb": pb, "total_mv": total, "float_mv": floating, "market_cap_cny": cap,
               "vendor_valuation_date": day.isoformat(), "source": "stock_api", "provider": "stock_api",
               "upstream_provider": "unknown", "api": "sw_daily", "adjustment": "unadjusted", "complete": True,
               "as_of": day.isoformat() + "T15:00:00+08:00", "fetched_at": stamp,
               "valuation_available_at": stamp, "timestamp_quality": "daily_close"}
        grouped.setdefault(code, []).append(bar)
    return {code: sorted(bars, key=lambda row: row["date"]) for code, bars in grouped.items()}


def _apply_sw(result, item, bars, stamp):
    _apply_index(result, item, bars, stamp, primary=True)
    item.update(price_kind="industry_index", price_label="申万行业指数", taxonomy="SW2021", basis="index")
    latest = bars[-1]
    if latest["date"] >= str(item.get("vendor_valuation_date") or ""):
        item.update(vendor_pe=latest["vendor_pe"], vendor_pb=latest["vendor_pb"],
                    vendor_valuation_date=latest["date"], vendor_pe_definition="申万指数供应商市盈率；未声明TTM，不用于归母聚合",
                    vendor_pb_definition="申万指数供应商市净率；指数口径，不用于归母权益聚合",
                    market_cap=latest["market_cap_cny"], market_cap_date=latest["date"], market_cap_unit="CNY",
                    market_cap_currency="CNY", market_cap_basis="total", market_cap_provider="stock_api")
    features = {row["date"]: row for row in item.get("feature_history", []) if isinstance(row, dict) and row.get("date")}
    caps = {row["date"]: row for row in item.get("market_cap_history", []) if isinstance(row, dict) and row.get("date")}
    for bar in bars:
        # First observation time is retained when revisiting an unchanged historical row.
        row = {key: bar.get(key) for key in ("date", "fetched_at", "vendor_pe", "vendor_pb",
                                            "market_cap_cny", "total_mv", "float_mv")}
        row.update(trade_date=bar["date"], available_at=bar.get("valuation_available_at") or stamp,
                   provider="stock_api", upstream_provider="unknown", amount_unit="CNY_10000")
        old = features.get(bar["date"])
        if old and all(old.get(key) == row.get(key) for key in ("vendor_pe", "vendor_pb", "market_cap_cny", "total_mv", "float_mv")):
            row["available_at"] = old.get("available_at", stamp)
            row["fetched_at"] = old.get("fetched_at", stamp)
        features[bar["date"]] = row
        if bar["market_cap_cny"] is not None:
            caps[bar["date"]] = {"date": bar["date"], "market_date": bar["date"], "date_basis": "trade_date",
                                 "value": bar["market_cap_cny"], "market_cap": bar["market_cap_cny"],
                                 "as_of": bar["as_of"], "fetched_at": stamp, "currency": "CNY", "source": "stock_api"}
    item["feature_history"] = sorted(features.values(), key=lambda row: row["date"])[-900:]
    item["market_cap_history"] = sorted(caps.values(), key=lambda row: row["date"])[-900:]


def _members(rows, l1_code, token, stamp, quality=None):
    quality = _quality(rows, quality)
    records, blocked = {}, set()
    today = _instant(stamp).date()
    for row in rows:
        raw_code = row.get("ts_code")
        if row.get("is_new") == "N":
            _quality_note(quality, "inactive_member", raw_code, "is_new", "filtered")
            continue
        try:
            if row.get("is_new") != "Y":
                raise _Failure("invalid_member_status")
            excluded = _date(row["out_date"], compact=True) if row.get("out_date") else None
            if excluded and excluded <= today:
                _quality_note(quality, "inactive_member", raw_code, "out_date", "filtered")
                continue
            if row.get("l1_code") != l1_code:
                raise _Failure("wrong_industry")
            if _is_non_a_stock(raw_code):
                _quality_note(quality, "out_of_scope_security", raw_code, "ts_code", "filtered")
                continue
            code = _stock_code(raw_code)
            if code != raw_code or not code:
                raise _Failure("invalid_code")
            included = _date(row["in_date"], compact=True) if row.get("in_date") else None
            if included and excluded and excluded < included:
                raise _Failure("invalid_member_dates")
            if included and included > today:
                _quality_note(quality, "future_member", raw_code, "in_date", "filtered")
                continue
            name = _clean_text(row.get("name"), token)
        except _Failure as exc:
            _quality_note(quality, exc.code, raw_code, "l1_code" if exc.code == "wrong_industry" else "ts_code" if exc.code == "invalid_code" else "membership")
            continue
        record = {"l1_code": l1_code, "is_new": "Y", "name": name}
        for key in ("l2_code", "l3_code"):
            value = row.get(key)
            if not isinstance(value, str) or not re.fullmatch(r"(?:80|85)\d{4}\.SI", value):
                record[key] = None
                _quality_note(quality, "invalid_optional_hierarchy", code, key, "field")
            else:
                record[key] = value
        for key in ("l1_name", "l2_name", "l3_name"):
            try:
                record[key] = _clean_text(row.get(key), token)
            except _Failure:
                record[key] = ""
                _quality_note(quality, "invalid_optional_text", code, key, "field")
        record.update(ts_code=code, code=code[:6], in_date=included.isoformat() if included else None,
                      out_date=excluded.isoformat() if excluded else None,
                      membership_fetched_at=stamp, membership_available_at=stamp)
        _dedupe_record(records, blocked, record, quality)
    _finish_quality(quality, records)
    if not records:
        raise _Failure("no_valid_membership_rows")
    return list(records.values())


def _attach_members(item, cached):
    existing = {row.get("ts_code") or _stock_code(row.get("code")): row for row in item.get("constituents", [])}
    members = []
    for record in cached["records"]:
        stock = existing.get(record["ts_code"], {})
        stock.update(copy.deepcopy(record))
        stock.setdefault("price", None)
        stock.setdefault("market_cap", None)
        members.append(stock)
    item["constituents"] = members
    item.update(constituent_count=len(members) if cached["complete"] else None,
                membership_count=len(members), membership_complete=cached["complete"],
                constituents_complete=cached["complete"], membership_as_of=cached["fetched_at"],
                membership_note="当前成分映射快照，不能当作历史时点已知成分")


def _rank_constituents(sector, cutoff):
    members = sector.get("constituents", [])
    candidates = [stock for stock in members if stock.get("market_cap_date") == cutoff.isoformat()
                  and isinstance(stock.get("market_cap"), (int, float)) and math.isfinite(stock["market_cap"]) and stock["market_cap"] > 0]
    if candidates:
        selected = sorted(candidates, key=lambda stock: stock["market_cap"], reverse=True)[:10]
        basis, rank_date = "total_market_cap", cutoff.isoformat()
    else:
        selected, basis, rank_date = members[:10], "membership_order", None
    sector["stocks"] = [copy.deepcopy(stock) for stock in selected]
    for stock in sector["stocks"]:
        stock["rank_basis"] = basis
        stock["rank_date"] = rank_date
        stock.setdefault("price", None)
    sector.update(rank_basis=basis, rank_date=rank_date)


def _apply_primary_basic(sectors, cache, cutoff):
    if cache.get("trade_date") != cutoff.isoformat():
        for sector in sectors:
            _rank_constituents(sector, cutoff)
            sector["valuation_coverage"] = {"matched_count": 0, "membership_count": len(sector.get("constituents", [])),
                                             "ratio": 0 if sector.get("constituents") else None, "trade_date": cutoff.isoformat(),
                                             "complete": False, "membership_complete": sector.get("membership_complete", False),
                                             "possibly_truncated": cache.get("possibly_truncated", False)}
        return
    lookup = {row["ts_code"]: row for row in cache.get("records", [])}
    for sector in sectors:
        matched = 0
        for stock in sector.get("constituents", []):
            row = lookup.get(stock["ts_code"])
            if row is None:
                continue
            matched += 1
            stock["stock_api_daily_basic"] = copy.deepcopy(row)
            stock.update(vendor_pe_ttm=row["vendor_pe_ttm"], vendor_pb=row["vendor_pb"],
                         vendor_valuation_date=row["trade_date"], vendor_pe_ttm_definition=PE_DEFINITION,
                         vendor_pb_definition=PB_DEFINITION, market_cap=row["market_cap_cny"],
                         market_cap_date=row["trade_date"], market_cap_unit="CNY", market_cap_currency="CNY",
                         market_cap_basis="total", market_cap_available_at=row["fetched_at"],
                         turnover_rate=row["turnover_rate"], volume_ratio=row["volume_ratio"],
                         circ_mv=row["circ_mv"], rank_basis="total_market_cap")
            old = _instant(stock.get("as_of"))
            if old is None or old <= _instant(row["as_of"]):
                stock.update(price=row["close"], as_of=row["as_of"], quote_date=row["trade_date"],
                             timestamp_quality="daily_close", provider="stock_api", upstream_provider="unknown")
            history = {value["date"]: value for value in stock.get("daily_basic_history", []) if value.get("date")}
            history[row["date"]] = copy.deepcopy(row)
            stock["daily_basic_history"] = sorted(history.values(), key=lambda value: value["date"])[-260:]
        _rank_constituents(sector, cutoff)
        total = len(sector.get("constituents", []))
        sector["valuation_coverage"] = {"matched_count": matched, "membership_count": total,
                                         "ratio": matched / total if total else None, "trade_date": cutoff.isoformat(),
                                         "possibly_truncated": cache.get("possibly_truncated", False),
                                         "membership_complete": sector.get("membership_complete", False),
                                         "quality": copy.deepcopy(cache.get("quality", {})),
                                         "complete": bool(total and matched == total and sector.get("membership_complete") and not cache.get("possibly_truncated")
                                                          and cache.get("quality", {}).get("complete", True))}


def _realtime(rows, allowed, now, api, token):
    quotes, seen = {}, set()
    for row in rows:
        code = row["ts_code"]
        if not isinstance(code, str) or not re.fullmatch(r"\d{6}\.(?:SH|SZ|SI)", code):
            raise _Failure("invalid_code")
        if code not in allowed:
            continue
        if code in seen:
            raise _Failure("duplicate_quote")
        seen.add(code)
        raw_time = row["trade_time"]
        if not isinstance(raw_time, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\+08:00)?", raw_time):
            raise _Failure("invalid_timestamp")
        timestamp = _instant(raw_time)
        if timestamp is None or timestamp > now or timestamp.date() != now.date() or is_trading_day(timestamp.date()) is not True:
            continue
        values = {key: _number(row[key], minimum=0) for key in ("close", "pre_close", "high", "open", "low", "vol", "amount")}
        if (min(values[key] for key in ("close", "pre_close", "high", "open", "low")) <= 0
                or values["low"] > min(values["open"], values["close"])
                or values["high"] < max(values["open"], values["close"])):
            raise _Failure("invalid_bar")
        change_pct = (values["close"] / values["pre_close"] - 1) * 100
        if not math.isfinite(change_pct):
            raise _Failure("invalid_number")
        quotes[code] = {"price": values["close"], "change_pct": change_pct,
                        "quote_date": timestamp.date().isoformat(), "quote_timestamp": timestamp.isoformat(timespec="seconds"),
                        "as_of": timestamp.isoformat(timespec="seconds"), "timestamp_quality": "exchange_timestamp",
                        "provider": "stock_api", "upstream_provider": "unknown", "api": api,
                        "name": _clean_text(row["name"], token), "open": values["open"], "high": values["high"], "low": values["low"],
                        "volume": values["vol"], "volume_unit": "share" if api == "rt_sw_k" else "unknown",
                        "amount": values["amount"], "amount_unit": "CNY", "complete": False}
    if not quotes:
        raise _Failure("empty_realtime")
    return quotes


def _apply_realtime(result, items, quotes, stamp):
    observations = result.setdefault("price_observations", [])
    collected = _instant(stamp)
    continuous = market_session(collected)["phase"] in ("morning", "afternoon", "opening_auction", "auction_complete", "closing_auction")
    for code, item in items.items():
        quote = quotes.get(code)
        if quote is None:
            continue
        quote["fetched_at"] = stamp
        age = (collected - _instant(quote["as_of"])).total_seconds()
        quote.update(entity_id=item["id"], ts_code=code, symbol=code, basis="index", timestamp=quote["as_of"],
                     trade_date=quote["quote_date"], is_live=continuous and 0 <= age <= 300, age_seconds=age)
        item["live_quote"] = copy.deepcopy(quote)
        entry = result.setdefault("intraday", {}).setdefault(item["id"], {"entity_id": item["id"], "name": item.get("name", ""),
                                                                         "symbol": code, "basis": "index", "points": []})
        entry.setdefault("providers", {})["stock_api"] = copy.deepcopy(quote)
        if quote["quote_timestamp"] >= (entry.get("latest", {}).get("quote_timestamp") or ""):
            entry["latest"] = copy.deepcopy(quote)
        points = {(point.get("provider"), point.get("quote_timestamp")): point for point in entry.get("points", [])}
        points[("stock_api", quote["quote_timestamp"])] = copy.deepcopy(quote)
        days = set(sorted({point.get("quote_timestamp", "")[:10] for point in points.values()})[-5:])
        entry["points"] = sorted((point for point in points.values() if point.get("quote_timestamp", "")[:10] in days),
                                 key=lambda point: point.get("quote_timestamp", ""))[-2000:]
        entry["note"] = "自开始采集以来的实际行情快照，不补造缺失分钟"
        old, fresh = _instant(item.get("as_of")), _instant(quote["as_of"])
        confirmed_close = item.get("daily_quote", {}).get("quote_date") == quote["quote_date"] and collected.time().replace(tzinfo=None) >= clock(15)
        if not confirmed_close and (old is None or fresh > old or (fresh == old and item.get("timestamp_quality") != "daily_close")):
            item.update({key: quote[key] for key in ("price", "change_pct", "quote_date", "as_of", "timestamp_quality",
                                                    "provider", "upstream_provider", "open", "high", "low")})
            item.update(source="stock_api", fetched_at=stamp, stale=False)
        observations[:] = [row for row in observations if not (row.get("entity_id") == item["id"] and row.get("provider") == "stock_api" and row.get("field") == "last")]
        observations.append({"entity_id": item["id"], "provider": "stock_api", "upstream_provider": "unknown", "field": "last",
                             "trade_date": quote["quote_date"], "price": quote["price"], "adjustment": "none", "observed_at": stamp,
                             "market_timestamp": quote["as_of"], "timestamp_quality": "exchange_timestamp"})
    result["price_observations"] = observations[-600:]


def _news_records(rows, now, stamp, token):
    records = []
    for row in rows:
        when = _instant(row["datetime"])
        if not when or when > now or (now - when).total_seconds() > 86400 * 7:
            continue
        channels = row["channels"]
        if isinstance(channels, list) and all(isinstance(value, str) for value in channels):
            channels = ",".join(channels)
        title, content = _clean_text(row["title"], token, 500), _clean_text(row["content"], token, 5000)
        if not title and not content:
            continue
        records.append({"datetime": when.isoformat(timespec="seconds"), "published_at": when.isoformat(timespec="seconds"),
                        "title": title, "content": content, "channels": _clean_text(channels, token, 500),
                        "provider": "stock_api", "upstream_provider": "unknown", "source": "cls", "source_name": "财联社快讯",
                        "fetched_at": stamp, "url": None})
    return sorted(records, key=lambda row: row["published_at"], reverse=True)[:300]


def _capability_seed():
    """Read only the bounded public capability report, never credentials."""
    path = Path(__file__).resolve().parents[1] / "web/data/tushare-capabilities.json"
    try:
        if path.stat().st_size > 1024 * 1024:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def enrich_with_stock_api(data, config=None, now=None):
    """Use the fixed compatible service as primary for SW2021 and index data.

    Full rounds share a 12-call / 50-second ceiling and a three-second interval.
    Quote-only rounds make at most two batched calls, cached for at least 60s.
    Permissions cool down per API; HTTP rate limits cool down the whole host.
    Set stock_api.primary=False explicitly to retain the older supplement mode.
    """
    config = config or {}
    cfg = config.get("stock_api", {})
    cfg = copy.deepcopy(cfg) if isinstance(cfg, dict) else {}
    if cfg.get("primary") is False:
        return _enrich_legacy(data, config, now)
    result = copy.deepcopy(data)
    now = _instant(now) if now is not None else shanghai_now()
    if now is None:
        raise ValueError("invalid_now")
    stamp = now.isoformat(timespec="seconds")
    old = result.get("stock_api_state", {})
    state = {key: copy.deepcopy(old[key]) for key in ("calendar", "classification", "daily_basic", "members", "member_attempts", "endpoints",
             "index_requests", "index_cursor", "sw_history_requests", "sw_history_cursor", "member_cursor", "cooldown_until", "news", "realtime") if key in old}
    state.update(version=2, primary=True, last_run_at=stamp, requests_this_run=0)
    result["stock_api_state"] = state
    states = {row["id"]: row for row in result.setdefault("sources", []) if isinstance(row, dict) and "id" in row}
    prior_success = states.get("stock_api", {}).get("last_success")
    source = {"id": "stock_api", "name": "Tushare兼容行情主源", "type": "market_primary", "url": ENDPOINT,
              "provider": "stock_api", "upstream_provider": "unknown", "status": "pending", "last_success": prior_success,
              "message": "申万2021一级行业与指数行情；兼容服务上游未知"}
    states["stock_api"] = source
    result["sources"] = list(states.values())
    token = os.environ.get("STOCK_API_TOKEN", "")
    if not token or not token.strip():
        source.update(error_code="not_configured", message="尚未配置股票数据凭据，保留现有数据")
        return result
    if not config.get("network_enabled", True) or not cfg.get("enabled", True):
        source.update(error_code="offline", message="离线或未启用股票数据采集，保留现有数据")
        return result
    until = _instant(state.get("cooldown_until"))
    if until and until > now:
        source.update(status="cooldown", retry_after=until.isoformat(timespec="seconds"), message="行情服务冷却中，保留已有数据")
        return result
    quotes_only = bool(cfg.get("quotes_only") or config.get("quotes_only"))
    if quotes_only:
        cfg["max_requests"] = min(2, int(_bounded(cfg.get("max_requests"), 2, 0, 12)))
    try:
        client = _Client(token, cfg, now)
    except _Failure as exc:
        source.update(status="error", error_code=exc.code, message="行情服务地址无效，未发起请求")
        return result
    endpoints = state.setdefault("endpoints", {})
    for group in _capability_seed().values():
        if not isinstance(group, dict):
            continue
        checked = _instant(group.get("checked_at"))
        if checked is None or checked > now or (now - checked).total_seconds() >= 86400:
            continue
        for record in group.get("results", []):
            if not isinstance(record, dict) or record.get("api") not in FIELDS or record.get("error_code") != "api_denied":
                continue
            api = record["api"]
            previous_times = [_instant(endpoints.get(api, {}).get(key)) for key in ("last_attempt", "last_success")]
            prior = max((value for value in previous_times if value is not None), default=None)
            if prior and prior > checked:
                continue
            endpoints[api] = {"last_attempt": checked.isoformat(timespec="seconds"), "status": "cooldown", "error_code": "api_denied",
                              "cooldown_until": (checked + timedelta(days=1)).isoformat(timespec="seconds"), "capability_probe": True}
    successes, errors = [], []
    stop = False

    def eligible(api, ttl=0):
        if stop or client.count >= client.max_requests or client.remaining() <= 0:
            return False
        status = endpoints.get(api, {})
        retry = _instant(status.get("cooldown_until"))
        last = _instant(status.get("last_success"))
        return not (retry and retry > now) and not (ttl and last and (now - last).total_seconds() < ttl)

    def attempt(api, params, consume, ttl=0):
        nonlocal stop
        if not eligible(api, ttl):
            return False
        status = endpoints.setdefault(api, {})
        status["last_attempt"] = stamp
        try:
            rows = client.request(api, params)
            consume(rows)
            status.update(last_success=stamp, status="success", cooldown_until=None, error_code=None)
            successes.append(api)
            return True
        except _Failure as exc:
            errors.append(exc.code)
            delay = exc.retry_seconds or (900 if exc.code != "budget_exhausted" else 0)
            retry = (now + timedelta(seconds=delay)).isoformat(timespec="seconds") if delay else None
            if api == "index_member_all" and exc.code == "no_valid_membership_rows":
                target = params["l1_code"]
                target_state = state.setdefault("member_attempts", {}).setdefault(target, {})
                target_state.update(last_attempt=stamp, status="cooldown", error_code=exc.code, cooldown_until=retry)
                # Invalid rows belong to this industry, not an API permission.
                # Existing API cooldowns are still checked before any request.
                status.update(status="partial", error_code=exc.code, cooldown_until=None)
            elif api == "sw_daily" and exc.code == "daily_not_published":
                status.update(status="pending", error_code=exc.code, snapshot_retry_after=retry,
                              snapshot_target_date=params.get("trade_date"), cooldown_until=None)
                status.setdefault("snapshot_cooldowns", {})[params.get("trade_date")] = retry
            else:
                status.update(status="cooldown" if delay else "pending", error_code=exc.code, cooldown_until=retry)
            if exc.code in ("rate_limited", "redirect_blocked", "transport_error"):
                state["cooldown_until"] = retry
                source["retry_after"] = retry
                stop = True
            elif exc.code == "budget_exhausted":
                stop = True
            return False
        except Exception:
            errors.append("invalid_payload")
            status.update(status="error", error_code="invalid_payload", cooldown_until=(now + timedelta(minutes=15)).isoformat(timespec="seconds"))
            return False
        finally:
            state["requests_this_run"] = client.count

    def complete():
        state["financial_status"] = "vendor_ratios_only" if any(sector.get("constituents") for sector in result.get("sectors", [])) else "pending_constituents"
        state["financial_message"] = "供应商指数估值与成分估值独立展示，归母财务待补充" if state["financial_status"] == "vendor_ratios_only" else "待成分：完整行业成员映射按轮次补齐"
        if state.get("target_trade_date"):
            for sector in result.get("sectors", []):
                if sector.get("taxonomy") == "SW2021":
                    sector["daily_stale"] = sector.get("daily_quote", {}).get("quote_date") != state["target_trade_date"]
        source["requests_this_run"] = client.count
        source["endpoint_status"] = {api: {key: record.get(key) for key in ("status", "error_code", "cooldown_until", "last_success")}
                                     for api, record in endpoints.items()}
        if successes:
            source.update(status="stale" if errors else "success", last_success=stamp,
                          message="已更新主源行情；部分接口待权限或数据" if errors else "已更新主源行情与申万行业数据")
        elif errors:
            source.update(status="cooldown" if stop else "error", error_code=errors[0], message="部分行情接口暂不可用，保留已有缓存")
        else:
            source.update(status="success" if prior_success else "pending", message="使用已缓存行情；尚未到接口刷新时间")
        return result

    classification = state.get("classification", {})
    if not quotes_only:
        cached_at = _instant(classification.get("fetched_at"))
        classification_complete = classification.get("complete") and len(classification.get("records", [])) == 31
        classification_ttl = 86400 * 7 if classification_complete else 900
        if not cached_at or (now - cached_at).total_seconds() >= classification_ttl:
            def save_classification(rows):
                records = _classifications(rows, token)
                old_records = {} if len(records) == 31 else {row["index_code"]: row for row in classification.get("records", [])}
                old_records.update({row["index_code"]: row for row in records})
                if len(old_records) > 31:
                    raise _Failure("invalid_classification")
                classification.update(records=list(old_records.values()), fetched_at=stamp, complete=len(old_records) == 31,
                                      expected_count=31, received_count=len(records))
                state["classification"] = classification
            attempt("index_classify", {"src": "SW2021", "level": "L1"}, save_classification)
        if classification.get("records"):
            _canonicalize(result, classification["records"])
    sectors = [sector for sector in result.get("sectors", []) if sector.get("taxonomy") == "SW2021" and _sw_code(sector.get("id"))]
    sector_map = {sector["id"]: sector for sector in sectors}
    index_map = {_index_code(item): item for item in result.get("market", []) if _index_code(item)}
    for item in index_map.values():
        item["primary_provider"] = "stock_api"
    # A source timestamp is required even when the payload is obtained after close.
    if is_trading_day(now.date()) is True:
        realtime = state.setdefault("realtime", {})
        for api, items in (("rt_idx_k", index_map), ("rt_sw_k", sector_map)):
            if not items:
                continue
            def save_realtime(rows, api=api, items=items):
                quotes = _realtime(rows, items, now, api, token)
                realtime[api] = {"fetched_at": stamp, "quotes": quotes}
                _apply_realtime(result, items, quotes, stamp)
            attempt(api, {"ts_code": ",".join(items)}, save_realtime,
                    ttl=_bounded(cfg.get("realtime_cache_seconds"), 60, 60, 3600))
            cached = realtime.get(api, {})
            if cached.get("quotes"):
                _apply_realtime(result, items, {code: quote for code, quote in cached["quotes"].items() if quote.get("quote_date") == now.date().isoformat()}, cached.get("fetched_at", stamp))
    if quotes_only:
        return complete()
    cutoff = now.date() if is_trading_day(now.date()) is True and now.time().replace(tzinfo=None) >= clock(15) else previous_trading_day(now.date())
    if cutoff is None:
        source.update(error_code="calendar_unverified", message="缺少已核实交易日历，保留行情缓存")
        return complete()
    state["target_trade_date"] = cutoff.isoformat()
    snapshot_status = endpoints.get("sw_daily", {})
    snapshot = snapshot_status.get("snapshot_date")
    snapshot_retry = _instant(snapshot_status.get("snapshot_cooldowns", {}).get(cutoff.strftime("%Y%m%d")))
    snapshot_waiting = snapshot_retry and snapshot_retry > now
    def save_snapshot(rows, target=cutoff):
            grouped = _sw_bars(rows, sector_map, target, target, stamp, token)
            if not grouped:
                raise _Failure("daily_not_published", 900)
            for code, bars in grouped.items():
                _apply_sw(result, sector_map[code], bars, stamp)
            endpoints.setdefault("sw_daily", {})["snapshot_date"] = target.isoformat()
            endpoints["sw_daily"]["snapshot_count"] = len(grouped)
            endpoints["sw_daily"]["snapshot_complete"] = len(grouped) == len(sectors)
    if sectors and (snapshot != cutoff.isoformat() or not snapshot_status.get("snapshot_complete")) and not snapshot_waiting:
        attempt("sw_daily", {"trade_date": cutoff.strftime("%Y%m%d")}, save_snapshot)
    # Before the vendor's scheduled publication, seed all industries from the
    # previous confirmed trading day; an empty target day never disables history.
    previous_day = previous_trading_day(cutoff)
    previous_retry = _instant(endpoints.get("sw_daily", {}).get("snapshot_cooldowns", {}).get(previous_day.strftime("%Y%m%d"))) if previous_day else None
    if sectors and previous_day and not (previous_retry and previous_retry > now) and not any(sector.get("daily_quote", {}).get("quote_date", "") >= previous_day.isoformat() for sector in sectors):
        attempt("sw_daily", {"trade_date": previous_day.strftime("%Y%m%d")},
                lambda rows: save_snapshot(rows, previous_day))
    for sector in sectors:
        daily = sector.get("daily_quote", {})
        sector["daily_stale"] = daily.get("quote_date") != cutoff.isoformat()
        if sector["daily_stale"] and not sector.get("live_quote"):
            sector["stale"] = True
    basic = state.get("daily_basic", {})
    basic_partial = basic.get("completeness") == "partial" or basic.get("quality", {}).get("complete") is False
    basic_parsed_at = _instant(basic.get("last_parse_at") or basic.get("fetched_at"))
    basic_refresh_partial = basic_partial and (basic.get("parser_version") != ROW_PARSER_VERSION
                                               or not basic_parsed_at or (now - basic_parsed_at).total_seconds() >= 900)
    if sectors and (basic.get("trade_date") != cutoff.isoformat() or basic_refresh_partial):
        def save_basic(rows):
            quality = {}
            try:
                records = _basic_records(rows, cutoff, stamp, quality)
            finally:
                basic["quality"] = quality
                basic.update(parser_version=ROW_PARSER_VERSION, last_parse_at=stamp)
                state["daily_basic"] = basic
            basic.update(trade_date=cutoff.isoformat(), fetched_at=stamp, records=records, row_count=len(rows),
                         possibly_truncated=len(rows) >= 6000, full_market_request=True,
                         completeness="possibly_truncated" if len(rows) >= 6000 else "partial" if not quality["complete"] else "not_verified")
            state["daily_basic"] = basic
        attempt("daily_basic", {"trade_date": cutoff.strftime("%Y%m%d")}, save_basic)
    cal = state.get("calendar", {})
    last_cal = _instant(cal.get("last_attempt") or cal.get("checked_at") or endpoints.get("trade_cal", {}).get("last_attempt"))
    if last_cal is None or (now - last_cal).total_seconds() >= 86400:
        start, end = now.date() - timedelta(days=7), now.date() + timedelta(days=7)
        def save_calendar(rows):
            records = _calendar(rows, start, end)
            state["calendar"] = {"records": records, "checked_at": stamp, "last_attempt": stamp,
                                 "official_rules_unchanged": True, "authority": "observation_only"}
        attempt("trade_cal", {"exchange": "SSE", "start_date": start.strftime("%Y%m%d"), "end_date": end.strftime("%Y%m%d")}, save_calendar)
    index_attempts = state.setdefault("index_requests", {})
    indices = list(index_map.values())
    cursor = int(_bounded(state.get("index_cursor"), 0, 0, 1000000)) % max(1, len(indices))
    count = 0
    for offset in range(len(indices)):
        if count >= int(_bounded(cfg.get("max_index_calls"), 2, 0, 6)) or not eligible("index_daily"):
            break
        position = (cursor + offset) % len(indices)
        item = indices[position]
        history = item.get("source_history", {}).get("stock_api", [])
        days = _completed_dates(history, cutoff)
        cached = index_attempts.setdefault(item["id"], {})
        cached["history_count"] = len(days)
        if len(days) < MIN_HISTORY_BARS:
            cached["complete"] = False
        if cached.get("target_date") == cutoff.isoformat() and cached.get("complete") and len(days) >= MIN_HISTORY_BARS:
            continue
        start = max(days) - timedelta(days=7) if len(days) >= MIN_HISTORY_BARS else cutoff - timedelta(days=365)
        def save_index(rows, item=item, start=start):
            bars = _index_bars(rows, _index_code(item), start, cutoff, stamp)
            _apply_index(result, item, bars, stamp, primary=True)
            count = len(_completed_dates(item.get("source_history", {}).get("stock_api", []), cutoff))
            index_attempts[item["id"]] = {"target_date": cutoff.isoformat(), "last_success": stamp,
                                         "history_count": count, "latest_trade_date": bars[-1]["date"],
                                         "complete": count >= MIN_HISTORY_BARS and bars[-1]["date"] == cutoff.isoformat()}
        attempt("index_daily", {"ts_code": _index_code(item), "start_date": start.strftime("%Y%m%d"), "end_date": cutoff.strftime("%Y%m%d")}, save_index)
        state["index_cursor"] = (position + 1) % len(indices)
        count += 1
    members = state.setdefault("members", {})
    member_attempts = state.setdefault("member_attempts", {})
    cursor = int(_bounded(state.get("member_cursor"), 0, 0, 1000000)) % max(1, len(sectors))
    count = 0
    for offset in range(len(sectors)):
        if count >= int(_bounded(cfg.get("max_member_calls"), 2, 0, 8)) or not eligible("index_member_all"):
            break
        position = (cursor + offset) % len(sectors)
        sector = sectors[position]
        cached = members.get(sector["id"], {})
        retry = _instant(member_attempts.get(sector["id"], {}).get("cooldown_until"))
        if retry and retry > now:
            continue
        fetched = _instant(cached.get("fetched_at"))
        ttl = 86400 * 7 if cached.get("complete") else 900
        parser_upgrade = bool(cached and not cached.get("complete") and cached.get("parser_version") != ROW_PARSER_VERSION)
        if fetched and (now - fetched).total_seconds() < ttl and not parser_upgrade:
            continue
        def save_members(rows, sector=sector):
            quality = {}
            try:
                records = _members(rows, sector["id"], token, stamp, quality)
            finally:
                member_attempts.setdefault(sector["id"], {}).update(last_attempt=stamp, quality=quality)
            complete = len(rows) < 2000 and quality["complete"]
            members[sector["id"]] = {"records": records, "fetched_at": stamp, "complete": complete,
                                     "possibly_truncated": len(rows) >= 2000, "received_count": len(rows), "quality": quality,
                                     "parser_version": ROW_PARSER_VERSION}
            member_attempts[sector["id"]].update(status="success" if complete else "partial", error_code=None,
                                                 last_success=stamp, cooldown_until=None)
        attempt("index_member_all", {"l1_code": sector["id"], "is_new": "Y"}, save_members)
        state["member_cursor"] = (position + 1) % len(sectors)
        count += 1
    for sector in sectors:
        if sector["id"] in members:
            _attach_members(sector, members[sector["id"]])
    _apply_primary_basic(sectors, basic, cutoff)
    if cfg.get("news_enabled", True):
        def save_news(rows):
            records = _news_records(rows, now, stamp, token)
            state["news"] = {"records": records, "fetched_at": stamp, "possibly_truncated": len(rows) >= 1500}
        attempt("news", {"src": "cls", "start_date": (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
                          "end_date": now.strftime("%Y-%m-%d %H:%M:%S")}, save_news, ttl=3600)
    if state.get("news", {}).get("records"):
        result["stock_api_news"] = copy.deepcopy(state["news"]["records"])
    history_requests = state.setdefault("sw_history_requests", {})
    cursor = int(_bounded(state.get("sw_history_cursor"), 0, 0, 1000000)) % max(1, len(sectors))
    count = 0
    for offset in range(len(sectors)):
        if count >= int(_bounded(cfg.get("max_sw_history_calls"), 2, 0, 8)) or not eligible("sw_daily"):
            break
        position = (cursor + offset) % len(sectors)
        sector = sectors[position]
        old_bars = sector.get("source_history", {}).get("stock_api", [])
        days = _completed_dates(old_bars, cutoff)
        cached = history_requests.setdefault(sector["id"], {})
        cached["history_count"] = len(days)
        if len(days) < MIN_HISTORY_BARS:
            cached["complete"] = False
        recent = _instant(cached.get("last_success"))
        if len(days) >= MIN_HISTORY_BARS and cached.get("target_date") == cutoff.isoformat() and (cached.get("complete") or (recent and (now - recent).total_seconds() < 21600)):
            continue
        start = max(days) - timedelta(days=7) if len(days) >= MIN_HISTORY_BARS else cutoff - timedelta(days=365)
        def save_history(rows, sector=sector, start=start):
            grouped = _sw_bars(rows, {sector["id"]}, start, cutoff, stamp, token)
            if not grouped:
                raise _Failure("empty_completed_bars")
            bars = grouped[sector["id"]]
            _apply_sw(result, sector, bars, stamp)
            count = len(_completed_dates(sector.get("source_history", {}).get("stock_api", []), cutoff))
            history_requests[sector["id"]] = {"target_date": cutoff.isoformat(), "last_success": stamp,
                                             "history_count": count, "latest_trade_date": bars[-1]["date"],
                                             "complete": count >= MIN_HISTORY_BARS and bars[-1]["date"] == cutoff.isoformat() and len(rows) < 4000}
        attempt("sw_daily", {"ts_code": sector["id"], "start_date": start.strftime("%Y%m%d"), "end_date": cutoff.strftime("%Y%m%d")}, save_history)
        state["sw_history_cursor"] = (position + 1) % len(sectors)
        count += 1
    return complete()
