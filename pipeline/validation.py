"""Cross-provider price checks without network calls or mutation of inputs.

An industry index and its ETF proxy are different entities. Only identical
entity/field/trading-date/adjustment keys can corroborate one another. A library
name is not an independent upstream, and retrieval time is not market time.
"""
from copy import deepcopy
from datetime import date, datetime, time
import math

from .calendar import is_trading_day, previous_trading_day, shanghai_now

RELATIVE_TOLERANCE = 0.0005
MINIMUM_TICK = 0.01
REALTIME_WINDOW_SECONDS = 60


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError):
        return None


def _timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        # A missing timezone cannot substantiate two quotes being simultaneous.
        if parsed.tzinfo is None:
            return None
        return shanghai_now(parsed)
    except (TypeError, ValueError):
        return None


def _provider(observation):
    explicit = observation.get("upstream_provider") or observation.get("source_provider")
    if not explicit and isinstance(observation.get("upstream"), str):
        explicit = observation["upstream"]
    raw = str(explicit or observation.get("provider") or "").strip().lower()
    compact = raw.replace("-", "_").replace(" ", "")
    for canonical, aliases in {
        "eastmoney": ("eastmoney", "east_money", "东方财富", "东方财富网"),
        "tencent": ("tencent", "腾讯", "qt.gtimg", "qt.qq"),
        "sina": ("sina", "新浪"),
        "baostock": ("baostock", "bao_stock"),
    }.items():
        if any(alias in compact for alias in aliases):
            return canonical
    exact = {"em": "eastmoney", "qq": "tencent", "akshare_em": "eastmoney",
             "akshare_sina": "sina", "akshare_tx": "tencent"}
    if compact in exact:
        return exact[compact]
    if not compact or compact in {"unknown", "akshare", "requests", "pandas", "urllib", "yfinance"}:
        return None
    if compact.startswith("akshare.") or compact.startswith("akshare_"):
        return "eastmoney" if compact.endswith("_em") else None
    # Unrecognized explicit vendor names retain their upstream identity.
    return compact


def _adjustment(value):
    normalized = str(value or "none").strip().lower()
    return "none" if normalized in ("none", "unadjusted", "3", "不复权") else normalized


def _entities(dashboard):
    objects = {}
    for collection in ("market", "sectors", "entities"):
        for entity in dashboard.get(collection, []):
            identity = entity.get("id")
            if not identity:
                continue
            objects.setdefault(str(identity), []).append(entity)
            proxy = entity.get("proxy")
            if isinstance(proxy, dict) and proxy.get("code"):
                proxy_id = f"{identity}:proxy:{proxy['code']}"
                objects.setdefault(proxy_id, []).append(proxy)
    return objects


def _target(now):
    trading = is_trading_day(now.date())
    if trading is None:
        return None, "close", "unknown_calendar"
    clock = now.time().replace(tzinfo=None)
    if trading and clock >= time(9, 15):
        return now.date().isoformat(), "close" if clock >= time(15, 15) else "last", None
    previous = previous_trading_day(now.date(), include_today=not trading)
    return (previous.isoformat() if previous else None), "close", None if previous else "unknown_calendar"


def _evidence(raw, now):
    observed, market_time = _timestamp(raw.get("observed_at")), _timestamp(raw.get("market_timestamp"))
    price, provider = _number(raw.get("price")), _provider(raw)
    result = {"entity_id": raw.get("entity_id"), "provider": raw.get("provider"),
              "upstream_provider": provider, "field": raw.get("field"),
              "trade_date": raw.get("trade_date"), "adjustment": _adjustment(raw.get("adjustment")),
              "price": price, "observed_at": observed.isoformat() if observed else raw.get("observed_at"),
              "market_timestamp": market_time.isoformat() if market_time else raw.get("market_timestamp"),
              "timestamp_quality": raw.get("timestamp_quality"), "used": False, "exclusion_reason": None}
    if price is None:
        result["exclusion_reason"] = "invalid_price"
    elif provider is None:
        result["exclusion_reason"] = "unknown_upstream"
    elif raw.get("field") not in ("close", "last"):
        result["exclusion_reason"] = "unsupported_field"
    elif observed is None or observed > now:
        result["exclusion_reason"] = "invalid_or_future_observation_time"
    else:
        try:
            parsed_day = date.fromisoformat(str(raw.get("trade_date", "")))
            if parsed_day > now.date():
                result["exclusion_reason"] = "future_trade_date"
        except ValueError:
            result["exclusion_reason"] = "invalid_trade_date"
    return result


def _check(identity, entity, observations, now, target_date, preferred_field, calendar_error):
    evidence = [_evidence(raw, now) for raw in observations]
    adjustment = _adjustment(entity.get("adjustment"))
    available = [e for e in evidence if not e["exclusion_reason"] and e["adjustment"] == adjustment]
    actual_date = max((e["trade_date"] for e in available), default=None)
    # Expose the older corroboratable date without upgrading the current check.
    historical_groups = {}
    for e in available:
        historical_groups.setdefault((e["field"], e["trade_date"], e["adjustment"]), []).append(e)
    comparable_dates = []
    for (historical_field, historical_date, _), group in historical_groups.items():
        if historical_field == "close" and len(set(e["upstream_provider"] for e in group)) >= 2:
            comparable_dates.append(historical_date)
        elif historical_field == "last":
            timed = [e for e in group if _timestamp(e["market_timestamp"]) and e["timestamp_quality"] != "retrieved_only"
                     and _timestamp(e["market_timestamp"]) <= now
                     and _timestamp(e["market_timestamp"]).date().isoformat() == historical_date]
            if any(a["upstream_provider"] != b["upstream_provider"]
                   and abs((_timestamp(a["market_timestamp"]) - _timestamp(b["market_timestamp"])).total_seconds()) <= REALTIME_WINDOW_SECONDS
                   for i, a in enumerate(timed) for b in timed[i + 1:]):
                comparable_dates.append(historical_date)
    field = preferred_field
    # Outside continuous/auction trading, a timestamped last quote can be checked
    # if no completed close exists. It remains a last quote, never a fabricated close.
    if field == "close" and not any(e["field"] == "close" and e["trade_date"] == target_date and not e["exclusion_reason"] for e in evidence):
        if any(e["field"] == "last" and e["trade_date"] == target_date for e in evidence):
            field = "last"
    check = {"entity_id": identity, "status": "not_comparable", "target_date": target_date,
             "actual_date": actual_date, "latest_comparable_date": max(comparable_dates, default=None),
             "field": field, "adjustment": adjustment, "providers": [], "used_sources": [],
             "independent_source_count": 0, "max_abs_diff": None, "max_rel_diff_pct": None,
             "tolerance_pct": RELATIVE_TOLERANCE * 100, "minimum_tick": MINIMUM_TICK,
             "comparison_count": 0, "comparisons": [], "evidence": evidence,
             "checked_at": now.isoformat(), "reason": "没有当前目标交易日的可比报价"}
    if calendar_error or target_date is None:
        check["reason"] = "目标交易日历未核实，不能确认数据是否属于最近交易日"
        for e in evidence:
            e["exclusion_reason"] = e["exclusion_reason"] or "unknown_calendar"
        return check
    eligible = []
    for e in evidence:
        if e["exclusion_reason"]:
            continue
        if e["trade_date"] != target_date:
            e["exclusion_reason"] = "not_current_target_date"
        elif e["field"] != field:
            e["exclusion_reason"] = "different_price_field"
        elif e["adjustment"] != adjustment:
            e["exclusion_reason"] = "different_adjustment"
        elif field == "last":
            market_time = _timestamp(e["market_timestamp"])
            if e["timestamp_quality"] == "retrieved_only":
                e["exclusion_reason"] = "retrieval_time_is_not_market_time"
            elif market_time is None:
                e["exclusion_reason"] = "missing_market_timestamp"
            elif market_time > now or market_time.date().isoformat() != target_date:
                e["exclusion_reason"] = "invalid_market_timestamp"
        if not e["exclusion_reason"]:
            eligible.append(e)
    # One freshest observation per upstream. Two AKShare routes to Eastmoney
    # cannot turn a single source into independent confirmation.
    per_provider = {}
    for e in eligible:
        provider = e["upstream_provider"]
        sort_key = (_timestamp(e["market_timestamp"]) if field == "last" else _timestamp(e["observed_at"]))
        old = per_provider.get(provider)
        old_key = (_timestamp(old["market_timestamp"]) if field == "last" else _timestamp(old["observed_at"])) if old else None
        if old is None or sort_key > old_key:
            if old:
                old["exclusion_reason"] = "duplicate_upstream"
            per_provider[provider] = e
        else:
            e["exclusion_reason"] = "duplicate_upstream"
    chosen = list(per_provider.values())
    original_provider_count = len(chosen)
    if field == "last" and chosen:
        newest = max(_timestamp(e["market_timestamp"]) for e in chosen)
        current = []
        for e in chosen:
            if (newest - _timestamp(e["market_timestamp"])).total_seconds() <= REALTIME_WINDOW_SECONDS:
                current.append(e)
            else:
                e["exclusion_reason"] = "market_timestamps_over_60_seconds_apart"
        chosen = current
        check["market_timestamp"] = newest.isoformat()
    for e in chosen:
        e["used"] = True
    check["providers"] = sorted(e["upstream_provider"] for e in chosen)
    check["used_sources"] = [{"provider": e["provider"], "upstream_provider": e["upstream_provider"],
                              "price": e["price"], "observed_at": e["observed_at"],
                              "market_timestamp": e["market_timestamp"]} for e in chosen]
    check["independent_source_count"] = len(chosen)
    if not chosen:
        return check
    if len(chosen) == 1:
        if original_provider_count > 1:
            check["reason"] = "多个来源的市场时间不同步，不能交叉验证"
        else:
            check["status"] = "single_source"
            check["reason"] = "当前目标日期只有一个独立上游，尚无交叉确认"
        return check
    tick = _number(entity.get("tick_size")) or (0.001 if entity.get("basis") == "etf" else MINIMUM_TICK)
    for i, first in enumerate(chosen):
        for second in chosen[i + 1:]:
            difference = abs(first["price"] - second["price"])
            scale = min(first["price"], second["price"])
            tolerance = max(scale * RELATIVE_TOLERANCE, tick)
            check["comparisons"].append({"providers": [first["upstream_provider"], second["upstream_provider"]],
                                          "absolute_difference": difference, "relative_difference_pct": 100 * difference / scale,
                                          "allowed_difference": tolerance,
                                          "within_tolerance": difference <= tolerance + 1e-10})
    check["minimum_tick"] = tick
    check["comparison_count"] = len(check["comparisons"])
    check["max_abs_diff"] = max(c["absolute_difference"] for c in check["comparisons"])
    check["max_rel_diff_pct"] = max(c["relative_difference_pct"] for c in check["comparisons"])
    conflict = any(not c["within_tolerance"] for c in check["comparisons"])
    check["status"] = "conflict" if conflict else "verified"
    check["reason"] = ("同口径独立来源差异超过容差，暂停使用该价格进行预测" if conflict
                       else "最近目标交易日、相同价格口径的独立来源在容差内一致")
    if field == "last" and not conflict:
        check["reason"] += "；仅证明该市场时间附近的一致性，不证明当前仍为最新行情"
    return check


def validate_prices(dashboard, now=None):
    """Return a deep-copied dashboard with entity checks and aggregate counts.

    Outputs: top-level data_checks/validation_summary, mirrored under metadata;
    market[]/sectors[]/entities[] and sector.proxy receive validation. Conflicts
    set data_quality='conflict'. No data or only one source never becomes verified.
    """
    output = deepcopy(dashboard)
    now = shanghai_now(now)
    target_date, field, calendar_error = _target(now)
    objects = _entities(output)
    observations = {}
    for raw in output.get("price_observations", []):
        if isinstance(raw, dict) and raw.get("entity_id"):
            observations.setdefault(str(raw["entity_id"]), []).append(raw)
    checks = []
    for identity, entities in objects.items():
        check = _check(identity, entities[0], observations.get(identity, []), now, target_date, field, calendar_error)
        checks.append(check)
        for entity in entities:
            entity["validation"] = deepcopy(check)
            if check["status"] == "conflict" or entity.get("data_quality") in (None, "conflict", "verified", "single_source", "not_comparable"):
                entity["data_quality"] = check["status"]
    # Unmapped observations are retained as evidence but cannot verify a different
    # entity merely because its price happens to resemble that observation.
    for identity, rows in observations.items():
        if identity not in objects:
            checks.append({"entity_id": identity, "status": "not_comparable", "target_date": target_date,
                           "reason": "观察值未匹配到网站中的独立实体或代理", "providers": [],
                           "evidence": [{**_evidence(raw, now), "exclusion_reason": "unknown_entity"} for raw in rows],
                           "comparison_count": 0, "max_abs_diff": None, "max_rel_diff_pct": None})
    summary = {"checked_at": now.isoformat(), "target_date": target_date, "entity_count": len(objects),
               "check_count": len(checks), "observation_count": sum(len(rows) for rows in observations.values()),
               **{status: sum(c["status"] == status for c in checks)
                  for status in ("verified", "single_source", "conflict", "not_comparable")}}
    output["data_checks"] = checks
    output["validation_summary"] = summary
    if not isinstance(output.get("metadata"), dict):
        output["metadata"] = {}
    output["metadata"]["data_checks"] = deepcopy(checks)
    output["metadata"]["validation_summary"] = deepcopy(summary)
    return output
