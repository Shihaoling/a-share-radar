"""Independent public-data fallbacks; ETF proxies never replace sector prices.

BaoStock's documented anonymous API is a separate non-HTTP data service and is
used serially under its published access rules. HTTP fallbacks retain the same
robots / 429 / timeout guard as the primary sources. Nothing bypasses an upstream
denial, and missing source timestamps are never replaced with retrieval times.
"""
from __future__ import annotations

import copy
import importlib
import json
import random
import socket
import threading
import time
from datetime import timedelta
from pathlib import Path

from pipeline.calendar import is_trading_day, previous_trading_day
from pipeline.sources import _Gate, _dt, _history, _incremental_start, _merge, _number, _records, _text, SourceUnavailable

ROOT = Path(__file__).resolve().parents[1]
_BAOSTOCK_LOCK = threading.RLock()


def _rows(result):
    if isinstance(result, list) or hasattr(result, "to_dict"):
        return _records(result)
    if str(getattr(result, "error_code", "")) != "0":
        raise SourceUnavailable("BaoStock: " + str(getattr(result, "error_msg", "未知错误")))
    rows = []
    while result.next():
        rows.append(dict(zip(result.fields, result.get_row_data())))
        if len(rows) > 10000:
            raise SourceUnavailable("BaoStock 响应超出本轮行数预算")
    return rows


def _closed_day(now):
    # Conservative: current-day daily data is requested only after 18:00 CST.
    # It still must be confirmed by the returned bar's own date.
    if is_trading_day(now.date()) is True and now.hour >= 18:
        return now.date()
    return previous_trading_day(now.date())


def _bs_symbol(symbol):
    return symbol[:2] + "." + symbol[2:]


def _fresh(item, cutoff):
    bars = item.get("history") or []
    return bool(bars and bars[-1].get("date", "") >= cutoff.isoformat())


def _apply_bars(item, bars, provider, now, url):
    if not bars:
        raise SourceUnavailable("来源未返回有效已完成日K")
    prior = {r["date"]: r for r in item.get("history", [])}
    prior.update({r["date"]: r for r in bars})
    item["history"] = sorted(prior.values(), key=lambda x: x["date"])
    latest = item["history"][-1]
    previous = item["history"][-2] if len(item["history"]) > 1 else None
    pct = _number(latest.get("change_pct"))
    if pct is None and previous and previous.get("close"):
        pct = (latest["close"] / previous["close"] - 1) * 100
    stamp = latest["date"] + "T15:00:00+08:00"
    item.update(price=latest["close"], open=latest.get("open"), change_pct=pct,
                as_of=stamp, quote_date=latest["date"], fetched_at=now.isoformat(timespec="seconds"),
                timestamp_quality="daily_close", provider=provider, source=provider,
                source_url=url, history_updated_at=now.isoformat(timespec="seconds"),
                stale=latest["date"] != now.date().isoformat(), adjustment="unadjusted",
                price_label="参考ETF收盘价" if item.get("basis") == "etf" else "最近日K收盘点位")


def enrich_with_fallback(data, config=None, now=None):
    """Deep-copy and supplement missing index histories and sector.proxy objects.

    Optional config overrides live below config['fallback']; network_enabled=False
    guarantees no network calls. Requires optional baostock and akshare packages.
    The original sector price, history, market cap and constituents stay untouched.
    """
    result = copy.deepcopy(data)
    cfg = _merge(json.loads((ROOT / "config/proxies.json").read_text()), (config or {}).get("fallback", {}))
    if "verify_secondary" in (config or {}):
        cfg["verify_secondary"] = bool(config["verify_secondary"])
    now = _dt(now)
    if now is None:
        raise ValueError("无效 now")
    stamp = now.isoformat(timespec="seconds")
    states = {s["id"]: s for s in result.setdefault("sources", [])}
    definitions = {
        "baostock_fallback": ("BaoStock 日线后备", "https://www.baostock.com/"),
        "tencent_fallback": ("腾讯指数日线后备 / AKShare", "https://akshare.akfamily.xyz/data/index/index.html"),
        "sina_index_fallback": ("新浪指数日线后备 / AKShare", "https://akshare.akfamily.xyz/data/index/index.html"),
        "sina_etf_fallback": ("新浪参考ETF历史 / AKShare", "https://akshare.akfamily.xyz/data/fund/fund_public.html"),
    }
    for sid, (name, url) in definitions.items():
        states.setdefault(sid, {"id": sid, "name": name, "url": url, "type": "market_fallback", "status": "pending", "last_success": None, "message": "等待后备数据源采集"})
    result["sources"] = list(states.values())
    sectors = {s["id"]: s for s in result.get("sectors", [])}
    proxies = []
    for spec in cfg["proxies"]:
        sector = sectors.get(spec["sector_id"])
        if not sector:
            continue
        proxy = sector.setdefault("proxy", {})
        proxy.update(spec)
        proxy["code"] = spec["symbol"][2:]
        proxy["instrument_id"] = spec["sector_id"] + ":proxy:" + proxy["code"]
        proxy.setdefault("price", None)
        proxy.setdefault("change_pct", None)
        proxy.setdefault("as_of", None)
        proxy.setdefault("history", [])
        proxy["adjustment"] = "unadjusted"
        proxy["risk_note"] = "参考ETF与板块覆盖不同；未复权价格受分红、拆分、折溢价影响，历史收益不可直接视为板块总回报。"
        proxy["price_label"] = "参考ETF收盘价"
        proxies.append(proxy)
    if not cfg.get("enabled", True) or not (config or {}).get("network_enabled", True):
        return result
    cutoff = _closed_day(now)
    if cutoff is None:
        for sid in definitions:
            states[sid].update(status="pending", message="缺少已核实交易日历，未猜测最近交易日")
        return result
    started = time.monotonic()
    bs_calls = 0
    errors = {sid: [] for sid in definitions}
    counts = {sid: 0 for sid in definitions}
    observations = { (o.get("entity_id"), o.get("provider"), o.get("field"), o.get("trade_date"), o.get("adjustment")): o for o in result.get("price_observations", []) }

    def observe(entity_id, bars, provider):
        if not bars:
            raise SourceUnavailable("来源未返回可校验的已完成日K")
        for bar in bars[-2:]:
            price = _number(bar.get("close"))
            if price is None or price <= 0:
                continue
            observation = {"entity_id": entity_id, "provider": provider, "upstream_provider": provider,
                           "field": "close", "trade_date": bar["date"], "price": price,
                           "adjustment": "none", "observed_at": bar.get("as_of") or stamp,
                           "market_timestamp": bar["date"] + "T15:00:00+08:00", "timestamp_quality": "daily_close"}
            observations[(entity_id, provider, "close", bar["date"], "none")] = observation

    for item in result.get("market", []):
        bars = item.get("history", [])
        if bars:
            provider = bars[-1].get("source") or item.get("source") or item.get("provider")
            if provider in {"eastmoney", "tencent", "sina", "baostock"}:
                observe(item["id"], bars, provider)

    def independent_count(entity_id):
        return len({o["provider"] for o in observations.values() if o.get("entity_id") == entity_id and o.get("trade_date") == cutoff.isoformat() and o.get("field") == "close"})

    verification_targets = []
    if cfg.get("verify_secondary", True):
        for item in sorted(result.get("market", []), key=lambda m: m.get("verification_attempted_at") or ""):
            if independent_count(item["id"]) >= 2 or str(item.get("verification_attempted_at", ""))[:10] == now.date().isoformat():
                continue
            verification_targets.append(item)
            if len(verification_targets) >= int(cfg.get("max_secondary_verifications", 2)):
                break
    verification_ids = {m["id"] for m in verification_targets}

    def needs_channel(item, provider):
        if not _fresh(item, cutoff):
            return True
        return item["id"] in verification_ids and independent_count(item["id"]) < 2 and not any(o.get("entity_id") == item["id"] and o.get("provider") == provider and o.get("trade_date") == cutoff.isoformat() for o in observations.values())

    def allowed(sid):
        retry = _dt(states[sid].get("retry_after")) if states[sid].get("retry_after") else None
        return not (retry and retry > now)

    def budget():
        if time.monotonic() - started >= cfg["request_budget_seconds"]:
            raise SourceUnavailable("后备来源达到时间预算")

    def incremental_start(item):
        return _incremental_start(item, cutoff - timedelta(days=cfg["history_days"]), cutoff, cfg.get("history_overlap_days", 7))

    def serial_pause():
        pause = float(cfg["min_interval_seconds"]) + random.uniform(0, min(0.8, max(0, float(cfg.get("request_jitter_seconds", 0.8)))))
        if time.monotonic() - started + pause >= cfg["request_budget_seconds"]:
            raise SourceUnavailable("剩余预算不足以满足低频请求间隔")
        time.sleep(pause)

    def full_history_allowed(item):
        value = item.get("sina_full_history_attempt_at")
        last = _dt(value) if value else None
        if last and (last.date() == now.date() or (now - last).total_seconds() < float(cfg.get("full_history_cache_seconds", 86400))):
            return False
        return True

    def note_error(sid, exc):
        errors[sid].append(_text(exc, 220))
        delay = getattr(exc, "retry_seconds", 1800)
        states[sid]["retry_after"] = (now + timedelta(seconds=delay)).isoformat(timespec="seconds")

    missing_indices = [m for m in result.get("market", []) if needs_channel(m, "baostock")]
    if cfg["providers"]["baostock"].get("enabled", True) and allowed("baostock_fallback") and (missing_indices or any(not _fresh(p, cutoff) for p in proxies)):
        bs = None
        logged_in = False
        with _BAOSTOCK_LOCK:
            old_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(float(cfg["socket_timeout_seconds"]))
            try:
                bs = importlib.import_module("baostock")
                login = bs.login()
                if str(getattr(login, "error_code", "")) != "0":
                    raise SourceUnavailable("BaoStock登录失败：" + str(getattr(login, "error_msg", "未知错误")))
                logged_in = True
                for item in missing_indices[:6]:
                    budget()
                    if bs_calls >= int(cfg["max_baostock_calls"]):
                        break
                    try:
                        if bs_calls:
                            serial_pause()
                        bs_calls += 1
                        rows = _rows(bs.query_history_k_data_plus(_bs_symbol(item["id"]), "date,code,open,high,low,close,volume,amount,pctChg", start_date=incremental_start(item).isoformat(), end_date=cutoff.isoformat(), frequency="d", adjustflag="3"))
                        bars = _history(rows, "baostock", now, 900)
                        bars = [b for b in bars if b["date"] <= cutoff.isoformat()]
                        observe(item["id"], bars, "baostock")
                        if not _fresh(item, cutoff):
                            _apply_bars(item, bars, "baostock", now, "https://www.baostock.com/")
                        result.setdefault("history", {})[item["id"]] = item["history"]
                        counts["baostock_fallback"] += 1
                    except Exception as exc:
                        note_error("baostock_fallback", exc)
                        if isinstance(exc, (OSError, TimeoutError)) or any(word in str(exc).lower() for word in ("blacklist", "频率", "限制访问", "connection")):
                            break
                # This documented batch returns all ETFs for one trading day.
                # It is not assumed to provide full ETF history.
                fn = getattr(bs, "query_daily_history_k_ETF", None)
                if fn and any(not _fresh(p, cutoff) for p in proxies):
                    budget()
                    if bs_calls < int(cfg["max_baostock_calls"]):
                        serial_pause()
                        bs_calls += 1
                        rows = _rows(fn(date=cutoff.isoformat()))
                        lookup = {str(r.get("code", "")).replace(".", ""): r for r in rows}
                        for proxy in proxies:
                            row = lookup.get(proxy["symbol"])
                            if not row or str(row.get("date", ""))[:10] != cutoff.isoformat():
                                continue
                            bars = _history([row], "baostock", now, 900)
                            if bars:
                                bars[0]["change_pct"] = _number(row.get("pctChg"))
                                _apply_bars(proxy, bars, "baostock", now, cfg["providers"]["baostock"]["etf_daily_docs"])
                                observe(proxy["instrument_id"], bars, "baostock")
                                counts["baostock_fallback"] += 1
                elif not fn:
                    errors["baostock_fallback"].append("当前BaoStock包未提供ETF批量接口；指数可独立更新")
            except Exception as exc:
                note_error("baostock_fallback", exc)
            finally:
                if logged_in and bs:
                    try:
                        bs.logout()
                    except Exception:
                        pass
                socket.setdefaulttimeout(old_timeout)

    remaining = max(0, cfg["request_budget_seconds"] - (time.monotonic() - started))
    http_cfg = json.loads((ROOT / "config/sources.json").read_text())
    http_cfg.update(max_http_requests=cfg["max_http_calls"], request_budget_seconds=remaining, max_retries=0)
    gate = _Gate(http_cfg)
    ak = None

    def ak_call(names, **kwargs):
        nonlocal ak
        budget()
        if ak is None:
            ak = importlib.import_module("akshare")
        fn = next((getattr(ak, n) for n in names if hasattr(ak, n)), None)
        if fn is None:
            raise SourceUnavailable("当前AKShare缺少 " + "/".join(names))
        with gate.guard_requests():
            return fn(**kwargs)

    if cfg["providers"]["tencent"].get("enabled", True) and allowed("tencent_fallback"):
        for item in [m for m in result.get("market", []) if needs_channel(m, "tencent")][:int(cfg["max_index_http_calls"])]:
            try:
                frame = ak_call(["index_zh_daily_tx", "stock_zh_index_daily_tx"], symbol=item["id"], start_date=incremental_start(item).strftime("%Y%m%d"), end_date=cutoff.strftime("%Y%m%d"))
                bars = [b for b in _history(frame, "tencent", now, 900) if b["date"] <= cutoff.isoformat()]
                observe(item["id"], bars, "tencent")
                if not _fresh(item, cutoff):
                    _apply_bars(item, bars, "tencent", now, "https://gu.qq.com/" + item["id"] + "/zs")
                result.setdefault("history", {})[item["id"]] = item["history"]
                counts["tencent_fallback"] += 1
            except Exception as exc:
                note_error("tencent_fallback", exc)
                # Don't hammer a provider after denial, cooldown or transport failure.
                break
    if cfg["providers"]["sina"].get("enabled", True) and allowed("sina_index_fallback"):
        for item in [m for m in result.get("market", []) if needs_channel(m, "sina")][:int(cfg["max_index_http_calls"])]:
            if not full_history_allowed(item):
                continue
            item["sina_full_history_attempt_at"] = stamp
            try:
                frame = ak_call(["index_zh_daily_sina", "stock_zh_index_daily"], symbol=item["id"])
                bars = [b for b in _history(frame, "sina", now, 900) if b["date"] <= cutoff.isoformat()]
                observe(item["id"], bars, "sina")
                if not _fresh(item, cutoff):
                    _apply_bars(item, bars, "sina", now, "https://finance.sina.com.cn/realstock/company/" + item["id"] + "/nc.shtml")
                result.setdefault("history", {})[item["id"]] = item["history"]
                counts["sina_index_fallback"] += 1
            except Exception as exc:
                note_error("sina_index_fallback", exc)
                break
    if cfg["providers"]["sina"].get("enabled", True) and allowed("sina_etf_fallback"):
        candidates = sorted(proxies, key=lambda p: p.get("fallback_history_attempt_at") or "")
        calls = 0
        seen_symbols = {}
        for proxy in candidates:
            if calls >= int(cfg["max_etf_history_calls"]):
                break
            if len(proxy.get("history", [])) >= 120 and _fresh(proxy, cutoff):
                continue
            if proxy["symbol"] in seen_symbols:
                _apply_bars(proxy, seen_symbols[proxy["symbol"]], "sina", now, "https://finance.sina.com.cn/fund/quotes/" + proxy["code"] + "/bc.shtml")
                proxy["sina_full_history_attempt_at"] = stamp
                continue
            if not full_history_allowed(proxy):
                continue
            calls += 1
            proxy["fallback_history_attempt_at"] = stamp
            proxy["sina_full_history_attempt_at"] = stamp
            try:
                frame = ak_call(["fund_etf_hist_sina"], symbol=proxy["symbol"])
                bars = [b for b in _history(frame, "sina", now, 900) if b["date"] <= cutoff.isoformat()]
                _apply_bars(proxy, bars, "sina", now, "https://finance.sina.com.cn/fund/quotes/" + proxy["code"] + "/bc.shtml")
                observe(proxy["instrument_id"], bars, "sina")
                seen_symbols[proxy["symbol"]] = bars
                counts["sina_etf_fallback"] += 1
            except Exception as exc:
                note_error("sina_etf_fallback", exc)
                break
    for sid in definitions:
        if counts[sid]:
            states[sid].update(status="stale" if errors[sid] else "success", last_success=stamp,
                               message=f"取得 {counts[sid]} 个指数/参考ETF日线结果；" + (errors[sid][0] if errors[sid] else "日期来自日K，非盘中实时行情"))
            if not errors[sid]:
                states[sid]["retry_after"] = None
        elif errors[sid]:
            states[sid].update(status="stale" if states[sid].get("last_success") else "error", message=errors[sid][0], last_attempt=stamp)
    for item in verification_targets:
        item["verification_attempted_at"] = stamp
    result["price_observations"] = sorted(observations.values(), key=lambda o: (o.get("trade_date", ""), o.get("observed_at", "")))[-600:]
    coverage = [{"entity_id": m["id"], "trade_date": cutoff.isoformat(), "independent_sources": independent_count(m["id"])} for m in result.get("market", [])]
    result["fallback_collection"] = {"baostock_calls": bs_calls, "http_requests": gate.count, "cutoff_date": cutoff.isoformat(), "proxy_count": sum(p.get("price") is not None for p in proxies), "verification_coverage": coverage, "verification_attempted_ids": list(verification_ids), "note": "ETF价格仅位于sector.proxy，不改写板块指数、板块市值或股票成分；日线为未复权。独立源比较按同实体/同交易日/同复权口径，不足两源不视为已验证。"}
    return result
