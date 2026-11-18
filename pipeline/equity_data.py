"""Resumable, low-frequency all-A-share daily history collection.

One request covers a whole trading date. Credentials and provider response text
never enter checkpoints. Daily bars are unadjusted; the next day's ``pre_close``
is the vendor's ex-rights reference, not necessarily this day's raw close.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import nullcontext
from datetime import date, datetime, time as clock, timedelta
import gzip
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

from pipeline.stock_api import (
    ENDPOINT, _service_before, token_for_api, credential_matches, _Failure, _NoRedirect, _provider_reason, _reject_constant,
    _http_failure, _service_errors, _stock_code,
)

ROOT = Path(__file__).resolve().parents[1]
SHANGHAI = ZoneInfo("Asia/Shanghai")
FIELDS = "ts_code,trade_date,open,high,low,close,pre_close,pct_chg,vol,amount"
ROW_LIMIT = 6000
MAX_BYTES = 8 * 1024 * 1024
REFERENCE_NOTE = (
    "日线价格未复权；下一交易日开盘/收盘收益以该日供应商pre_close为参考。"
    "除权日该参考可能不同于上一交易日原始收盘价；没有除权事件信息时不承诺绝对价格。"
)


def _day(value):
    if not isinstance(value, str):
        return None
    try:
        if re.fullmatch(r"\d{8}", value):
            return datetime.strptime(value, "%Y%m%d").date().isoformat()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value).isoformat()
    except ValueError:
        pass
    return None


def _now(value=None):
    value = value or datetime.now(SHANGHAI)
    return value.replace(tzinfo=SHANGHAI) if value.tzinfo is None else value.astimezone(SHANGHAI)


def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _atomic_json(path, value, compressed=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    handle = tempfile.NamedTemporaryFile(dir=path.parent, prefix="." + path.name, delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(gzip.compress(payload, mtime=0) if compressed else payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def trading_dates(dashboard, now=None, days=180):
    """Reuse observed trading dates, never manufacture a weekday-only calendar."""
    now = _now(now)
    cutoff = now.date()
    if now.time().replace(tzinfo=None) < clock(16, 15):
        cutoff -= timedelta(days=1)
    dates = set()
    for entity in dashboard.get("market", []):
        histories = [entity.get("history", [])]
        histories.extend((entity.get("source_history") or {}).values())
        for history in histories:
            if not isinstance(history, list):
                continue
            for bar in history:
                if not isinstance(bar, dict) or bar.get("complete") is False or bar.get("is_complete") is False:
                    continue
                day = _day(bar.get("date") or bar.get("trade_date"))
                if day and day <= cutoff.isoformat():
                    dates.add(day)
    calendar = dashboard.get("stock_api_state", {}).get("calendar", {}).get("records", [])
    for item in calendar:
        if not isinstance(item, dict) or str(item.get("is_open")) != "1":
            continue
        day = _day(item.get("cal_date"))
        if day and day <= cutoff.isoformat():
            dates.add(day)
    return sorted(dates, reverse=True)[:max(0, min(int(days), 1500))]


def _parse_payload(payload, day):
    if not isinstance(payload, dict) or type(payload.get("code")) is not int:
        raise _Failure("invalid_payload", 900)
    if payload["code"] != 0:
        if _provider_reason(payload)=="rate_limited":raise _Failure("rate_limited",60,provider_code=429,provider_reason="rate_limited")
        reason = _provider_reason(payload)
        if payload['code'] in (401, 403) and reason == 'provider_error':
            reason = 'authorization_denied'
        raise _Failure("api_denied" if reason != 'provider_error' else 'provider_error', 86400 if reason in {
            "authorization_denied", "permission_denied", "points_insufficient"
        } or payload["code"] in (401, 403) else 300 if reason == 'provider_error' else 3600,
            provider_code=payload["code"], provider_reason=reason)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise _Failure("invalid_payload", 900)
    fields, items = data.get("fields"), data.get("items")
    if (not isinstance(fields, list) or not all(isinstance(field, str) for field in fields)
            or set(fields) != set(FIELDS.split(","))
            or len(fields) != len(FIELDS.split(",")) or not isinstance(items, list)
            or len(items) > ROW_LIMIT):
        raise _Failure("invalid_payload", 900)
    rows, rejected, filtered, duplicate_count = {}, 0, 0, 0
    structural_failure = False
    reasons, examples = {}, []

    def note(reason, code=None, field=None):
        reasons[reason] = reasons.get(reason, 0) + 1
        if len(examples) < 5:
            example = {"reason": reason}
            if isinstance(code, str) and re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", code):
                example["ts_code"] = code
            if field:
                example["field"] = field
            examples.append(example)

    for item in items:
        if not isinstance(item, list) or len(item) != len(fields):
            rejected += 1
            structural_failure = True
            note("invalid_row_shape")
            continue
        raw = dict(zip(fields, item))
        raw_code = raw.get("ts_code")
        if _day(raw.get("trade_date")) != day:
            rejected += 1
            structural_failure = True
            note("wrong_trade_date", raw_code)
            continue
        code = _stock_code(raw_code)
        if not code:
            # Explicitly identifiable B shares, funds and domestic indices are
            # outside this A-share sample, rather than malformed A-share bars.
            non_a = isinstance(raw_code, str) and re.fullmatch(
                r"(?:900\d{3}\.SH|200\d{3}\.SZ|5\d{5}\.SH|1\d{5}\.SZ|000\d{3}\.SH|399\d{3}\.SZ)", raw_code)
            if non_a:
                filtered += 1
                note("excluded_out_of_scope", raw_code)
                continue
            rejected += 1
            note("invalid_security_code", raw_code)
            continue
        # Some whole-market feeds retain a suspended security as zero OHLC /
        # volume with the old close. It is neither a traded bar nor bad numeric
        # data. Excluding it prevents a synthetic zero open from becoming a label.
        if (all(type(raw.get(name)) in (int, float) and raw[name] == 0
                for name in ("open", "high", "low", "vol", "amount"))
                and all(type(raw.get(name)) in (int, float) and math.isfinite(raw[name]) and raw[name] > 0
                        for name in ("close", "pre_close"))):
            filtered += 1
            note("suspended_no_trade_excluded", code)
            continue
        values = {}
        invalid_field = None
        for name in ("open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount"):
            value = raw.get(name)
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or (name != "pct_chg" and value < 0)
                    or (name in ("open", "high", "low", "close", "pre_close") and value == 0)):
                invalid_field = name
                break
            values[name] = float(value)
        else:
            if (values["low"] <= min(values["open"], values["close"])
                    and values["high"] >= max(values["open"], values["close"])):
                if code in rows:
                    duplicate_count += 1
                    note("duplicate_security", code)
                else:
                    rows[code] = {"ts_code": code, "trade_date": day, "date": day, **values,
                                  "adj_factor": None, "adjustment": "unadjusted",
                                  "volume_unit": "hand_100_shares", "amount_unit": "CNY_1000"}
                continue
        rejected += 1
        note("invalid_number" if invalid_field else "invalid_ohlc", code, invalid_field)
    # The service may silently apply its own lower default row limit. Exact
    # common boundaries are treated conservatively; no pagination storm follows.
    possible_truncation = len(items) in {2000, 3000, 5000, ROW_LIMIT}
    rejected_fraction = rejected / max(1, len(items) - filtered)
    complete = bool(rows) and not (structural_failure or duplicate_count or possible_truncation) and rejected_fraction < .05
    quality = {"received_count": len(items), "accepted_count": len(rows),
               "rejected_count": rejected, "duplicate_count": duplicate_count,
               "filtered_count": filtered,
               "excluded_out_of_scope": reasons.get("excluded_out_of_scope", 0),
               "suspended_no_trade_excluded": reasons.get("suspended_no_trade_excluded", 0),
               "rejected_fraction": rejected_fraction,
               "reason_counts": reasons, "exclusion_sample": examples,
               "status": "accepted_partial" if complete and (rejected or filtered) else ("accepted" if complete else "rejected_batch"),
               "possibly_truncated": possible_truncation,
               "membership_completeness": "not_verified",
               "note": "完整批次指未发现截断、结构/日期错误或重复。非A证券和少量无效行剔除；停牌股票可能不返回，历史股票池完整性未经验证。"}
    return sorted(rows.values(), key=lambda row: row["ts_code"]), quality, complete


class DailyClient:
    """Separate batch budget; never modifies the production supplement client."""
    def __init__(self, token, max_requests=2, interval=3, timeout=15, budget=3600, now=None):
        self._token = token
        self.max_requests = max(0, min(int(max_requests), 1500))
        self.interval = max(3, min(float(interval), 60))
        self.timeout = max(.1, min(float(timeout), 20))
        self.budget = max(0, min(float(budget), 7200))
        self.now = _now(now)
        self.started, self.last, self.count = time.monotonic(), None, 0
        self.opener = urllib.request.build_opener(_NoRedirect())

    def remaining(self):
        return self.budget - (time.monotonic() - self.started)

    def request(self, day):
        day = _day(day)
        if not day:
            raise _Failure("invalid_date")
        if self.count >= self.max_requests or self.remaining() <= 0:
            raise _Failure("budget_exhausted")
        if self.last is not None:
            delay = max(0, self.interval - (time.monotonic() - self.last))
            if delay >= self.remaining():
                raise _Failure("budget_exhausted")
            if delay:
                time.sleep(delay)
        if not credential_matches(self._token):raise _Failure("credential_mismatch",3600)
        _service_before("daily",self.remaining())
        body = json.dumps({"api_name": "daily", "token": token_for_api(self._token,"daily"),
                           "params": {"trade_date": day.replace("-", ""), "limit": ROW_LIMIT},
                           "fields": FIELDS}, separators=(",", ":")).encode()
        request = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": "A-share-radar/equity-history"})
        self.count += 1
        self.last = time.monotonic()
        with _service_errors("daily", self.now, transport_retry=900, redirect_retry=86400):
            with self.opener.open(request, timeout=min(self.timeout, self.remaining())) as response:
                if response.geturl() != ENDPOINT:
                    raise _Failure("redirect_blocked", 86400)
                status = getattr(response, "status", 200)
                if status != 200:
                    raise _http_failure(status, response.headers, self.now, 86400)
                size = response.headers.get("Content-Length") if response.headers else None
                if size and (not str(size).isdigit() or int(size) > MAX_BYTES):
                    raise _Failure("response_too_large", 3600)
                raw = response.read(MAX_BYTES + 1)
                if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
                    raise _Failure("response_too_large", 3600)
                if self.remaining() <= 0:
                    raise _Failure("budget_exhausted")
            return _parse_payload(json.loads(raw.decode("utf-8"), parse_constant=_reject_constant), day)


def _read_batch(path):
    try:
        with gzip.open(path, "rb") as handle:
            raw = handle.read(24 * 1024 * 1024 + 1)
        if len(raw) > 24 * 1024 * 1024:
            return None
        value = json.loads(raw, parse_constant=_reject_constant)
        if not isinstance(value, dict) or not isinstance(value.get("rows"), list):
            return None
        return value
    except (OSError, ValueError, EOFError, _Failure):
        return None


def load_history(directory=None):
    """Return {ts_code: sorted bars}; incomplete/malformed batches stay excluded."""
    directory = Path(directory or ROOT / "data/equity")
    daily_dir = directory if directory.name == "daily" else directory / "daily"
    result = defaultdict(dict)
    for path in sorted(daily_dir.glob("????????.json.gz")):
        batch = _read_batch(path)
        if not batch or batch.get("complete") is not True:
            continue
        day = _day(batch.get("trade_date"))
        if not day or path.name != day.replace("-", "") + ".json.gz":
            continue
        for row in batch["rows"]:
            if (not isinstance(row, dict) or not _stock_code(row.get("ts_code"))
                    or _day(row.get("trade_date") or row.get("date")) != day):
                continue
            result[row["ts_code"]][day] = {**row, "date": day, "trade_date": day,
                                           "fetched_at": batch.get("fetched_at"),
                                           "source": "stock_api"}
    return {code: [bars[day] for day in sorted(bars)] for code, bars in sorted(result.items())}


def _future(value, now):
    try:
        instant = datetime.fromisoformat(value)
        return _now(instant) > now
    except (TypeError, ValueError):
        return False


def collect_equity_history(dashboard, config=None, now=None, max_requests=None, output_dir=None, *, request_guard=nullcontext):
    """Collect only missing complete dates; persist after each request for resume."""
    now = _now(now)
    config = config or {}
    cfg = config.get("equity", {})
    directory = Path(output_dir or cfg.get("data_dir") or ROOT / "data/equity")
    state_path = directory / "state.json"
    state = _read_json(state_path, {})
    if not isinstance(state, dict):
        state = {}
    if state.get("service_endpoint") and state["service_endpoint"]!=ENDPOINT or not state.get("service_endpoint") and config.get("stock_api",{}).get("provider_migration"):
        for key in ("cooldown_until","error_code","provider_code","provider_reason"):state.pop(key,None)
    state["service_endpoint"]=ENDPOINT
    days = max(1, min(int(cfg.get("history_days", 180)), 1500))
    target_dates = trading_dates(dashboard, now, days)
    summary = {"status": "pending", "source": "stock_api", "endpoint": "daily",
               "requested_days": days, "available_calendar_days": len(target_dates),
               "cached_days": 0, "collected_days": 0, "requests_this_run": 0,
               "partial_data_days": 0, "excluded_rows": 0,
               "latest_date": target_dates[0] if target_dates else None,
               "reference_note": REFERENCE_NOTE, "data_dir": str(directory),
               "universe_basis": "observed_daily_A_shares; historical_membership_not_verified"}
    missing = []
    for day in target_dates:
        batch = _read_batch(directory / "daily" / (day.replace("-", "") + ".json.gz"))
        if batch and batch.get("complete") is True and _day(batch.get("trade_date")) == day:
            summary["cached_days"] += 1
            quality = batch.get("quality") or {}
            summary["partial_data_days"] += int(quality.get("status") == "accepted_partial")
            summary["excluded_rows"] += quality.get("rejected_count", 0) + quality.get("filtered_count", 0)
        else:
            missing.append(day)
    summary["missing_days"] = len(missing)
    if not missing:
        summary["status"] = "ready" if target_dates else "missing_trading_dates"
        return summary
    if not config.get("network_enabled", True) or not cfg.get("enabled", True):
        summary["status"] = "offline_cache"
        return summary
    if _future(state.get("cooldown_until"), now):
        summary.update(status="cooldown", cooldown_until=state["cooldown_until"], error_code=state.get("error_code"))
        summary.update({key: state[key] for key in ('provider_code', 'provider_reason') if state.get(key) is not None})
        return summary
    token = os.environ.get("STOCK_API_TOKEN")
    if not token:
        summary["status"] = "missing_credentials"
        return summary
    requested = max_requests if max_requests is not None else cfg.get("max_requests", 2)
    client = DailyClient(token, max_requests=requested,
                         interval=cfg.get("min_interval_seconds", 3),
                         timeout=cfg.get("timeout_seconds", 15),
                         budget=cfg.get("budget_seconds", 3600), now=now)
    summary["status"] = "partial"
    transient_retries = 0
    for day in missing:
        if client.count >= client.max_requests:
            break
        try:
            try:
                with request_guard():
                    rows, quality, complete = client.request(day)
            except _Failure as transient:
                # At most ONE extra request in the entire bounded batch. Never
                # retry authentication, permission or explicit frequency errors.
                retryable = transient.code == 'transport_error' or transient.provider_reason == 'provider_error'
                if transient.deferred or not retryable or transient_retries >= 1 or client.count >= client.max_requests or client.remaining() <= 60:
                    raise
                transient_retries += 1
                if cfg.get('progress', False):
                    print(json.dumps({'equity_retry_date': day, 'reason': transient.code,
                                      'provider_code': transient.provider_code, 'wait_seconds': 60}), flush=True)
                time.sleep(60)
                with request_guard():
                    rows, quality, complete = client.request(day)
            fetched_at = _now().isoformat()
            batch = {"schema_version": 1, "trade_date": day, "fetched_at": fetched_at,
                     "complete": complete, "source": "stock_api", "service_endpoint":ENDPOINT, "upstream_provider": "unknown",
                     "row_count": len(rows), "quality": quality,
                     "reference_note": REFERENCE_NOTE, "rows": rows}
            _atomic_json(directory / "daily" / (day.replace("-", "") + ".json.gz"), batch, compressed=True)
            if not complete:
                reason = "possible_truncation" if quality["possibly_truncated"] else "incomplete_batch"
                raise _Failure(reason, 3600)
            summary["collected_days"] += 1
            summary["missing_days"] -= 1
            summary["partial_data_days"] += int(quality.get("status") == "accepted_partial")
            summary["excluded_rows"] += quality.get("rejected_count", 0) + quality.get("filtered_count", 0)
            state = {"service_endpoint":ENDPOINT,"last_success": fetched_at, "last_trade_date": day,
                     "cooldown_until": None, "error_code": None}
        except _Failure as failure:
            summary["error_code"] = failure.code
            for key in ('provider_code', 'provider_reason'):
                value = getattr(failure, key, None)
                if value is not None:
                    summary[key] = state[key] = value
            summary["status"] = "budget_exhausted" if failure.code == "budget_exhausted" else "unavailable"
            if failure.retry_seconds:
                state.update(cooldown_until=(_now() + timedelta(seconds=failure.retry_seconds)).isoformat(),
                             error_code=failure.code, last_attempt=_now().isoformat())
                summary["cooldown_until"] = state["cooldown_until"]
            summary["requests_this_run"] = client.count
            _atomic_json(state_path, state)
            break
        summary["requests_this_run"] = client.count
        _atomic_json(state_path, state)
        # Bounded diagnostics, useful for a long first fill; no stock rows or HTTP body.
        if cfg.get("progress", False):
            print(json.dumps({"equity_collected_date": day, "rows": len(rows),
                              "requests": client.count, "remaining_dates": summary["missing_days"]}), flush=True)
    if summary["missing_days"] == 0:
        summary["status"] = "ready"
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--max-requests", type=int, default=2)
    parser.add_argument("--min-interval", type=float, default=6, help="Seconds between historical batches; minimum 3")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/equity")
    parser.add_argument("--dashboard", type=Path, default=ROOT / "web/data/dashboard.json")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    dashboard = _read_json(args.dashboard, {})
    config = {"network_enabled": not args.offline,
              "equity": {"history_days": args.days, "max_requests": args.max_requests, "min_interval_seconds": args.min_interval, "progress": True}}
    result = collect_equity_history(dashboard, config, output_dir=args.output_dir)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
    return 0 if result["status"] in {"ready", "partial", "offline_cache"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
