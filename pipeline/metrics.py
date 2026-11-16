"""Transparent valuation aggregates and historical Sharpe ratios, offline only.

PE/PB are ratios of sums, not averages of constituent ratios. Unknown constituent
coverage is always labelled a sample portfolio. No dynamic PE substitution, ETF
look-through guess, or price/earnings shortcut is used.
"""
from copy import deepcopy
from datetime import date, datetime, time, timedelta
import math
import statistics

from .calendar import SHANGHAI, is_trading_day, next_trading_day, shanghai_now

VERSION = "quant-metrics-v1"
HELP = {
    "pe_ttm": {"title": "市盈率 PE TTM", "formula": "同一估值日可用成分总市值之和 ÷ 归母净利润TTM之和",
               "meaning": "用近期四个季度归母利润衡量当前市值，相当于市值是年利润的多少倍。",
               "caveats": "不是个股PE的算术平均；亏损贡献保留，合计利润≤0时不适用。动态PE不能替代TTM；覆盖不足仅表示样本组合。"},
    "pb": {"title": "市净率 PB", "formula": "同一估值日可用成分总市值之和 ÷ 同口径归母股东权益之和",
           "meaning": "衡量市值相对于账面净资产的倍数，需结合行业资产结构理解。",
           "caveats": "不是个股PB的算术平均；负净资产保留，合计权益≤0时不适用。市值、权益须匹配币种及归属口径。"},
    "sharpe": {"title": "夏普比率", "formula": "sqrt(252) × mean(日收益率 − 日无风险收益率) ÷ std(日收益率 − 日无风险收益率, ddof=1)",
               "meaning": "衡量历史每单位波动对应的超额收益。日无风险收益率=(1+年无风险利率)^(1/252)−1。",
               "caveats": "最近最多252个日收益样本，至少60个；默认年无风险利率2%是假设，可配置。未复权价格不含分红及公司行动调整；历史夏普不保证未来收益。"},
}


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def _date(value):
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def _datetime(value):
    if isinstance(value, datetime):
        return shanghai_now(value)
    if not value:
        return None
    try:
        text = str(value)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if len(text) == 10:
            parsed = datetime.combine(parsed.date(), time(23, 59, 59))
        return shanghai_now(parsed)
    except (ValueError, TypeError):
        return None


def _snapshot_date(row, specific=None):
    keys = ([specific] if specific else []) + ["valuation_date", "trade_date", "market_date", "date", "as_of"]
    for key in keys:
        if key == "as_of" and row.get("timestamp_quality") == "retrieved_only":
            continue
        parsed = _date(row.get(key))
        if parsed:
            return parsed
    return None


def _currency(row, entity, financial=False):
    if financial:
        return str(row.get("financial_currency") or row.get("currency") or entity.get("currency") or "CNY").upper()
    return str(row.get("market_cap_currency") or row.get("currency") or entity.get("currency") or "CNY").upper()


def _amount(row, key, unit_keys):
    value = _number(row.get(key))
    if value is None:
        return None
    unit = next((row.get(k) for k in unit_keys if row.get(k)), "CNY")
    scales = {"CNY": 1, "元": 1, "yuan": 1, "万元": 10000, "亿元": 100000000,
              "USD": 1, "HKD": 1, "EUR": 1}
    if unit not in scales:
        return None
    return value * scales[unit]


def _metric(name, label, as_of=None):
    return {"value": None, "status": "unavailable", "label": label,
            "units": "无量纲（年化）" if name == "sharpe" else "倍", "version": VERSION,
            "as_of": as_of, "sample_count": 0, "reason": "缺少可用数据"}


def _constituents(entity):
    source = entity.get("constituents")
    if not isinstance(source, list):
        source = entity.get("stocks", [])
    rows, seen = [], set()
    for i, row in enumerate(source):
        if not isinstance(row, dict):
            continue
        identity = row.get("code") or row.get("id") or row.get("symbol") or f"row-{i}"
        if identity not in seen:
            rows.append(row)
            seen.add(identity)
    return rows


def _denominator(stock, entity, metric, valuation_date, cap, now):
    """A direct parent financial amount or an explicitly named matching ratio."""
    if _currency(stock, entity, financial=True) != _currency(stock, entity):
        return None, "financial_currency_mismatch", None
    direct_keys = (("net_profit_parent_ttm", "net_profit_attributable_to_parent_ttm", "parent_net_profit_ttm")
                   if metric == "pe_ttm" else ("equity_parent", "parent_equity", "equity_attributable_to_parent"))
    general_key = "net_profit_ttm" if metric == "pe_ttm" else "shareholder_equity"
    basis = stock.get("profit_basis" if metric == "pe_ttm" else "equity_basis")
    if basis in ("parent", "attributable_to_parent", "归母"):
        direct_keys += (general_key,)
    direct = next((key for key in direct_keys if _number(stock.get(key)) is not None), None)
    if direct:
        available = _datetime(stock.get("financial_available_at") or stock.get("financial_as_of"))
        if available and available > now:
            return None, "financial_not_available_at_calculation", None
        known_at = _date(stock.get("financial_available_at") or stock.get("financial_as_of"))
        report_period = _date(stock.get("report_period") or stock.get("financial_period"))
        financial_snapshot = _date(stock.get("financial_snapshot_date"))
        if known_at and known_at > valuation_date:
            return None, "financial_not_available_at_valuation", None
        if report_period and report_period > valuation_date:
            return None, "future_financial_period", None
        if financial_snapshot and financial_snapshot != valuation_date:
            return None, "financial_snapshot_date_mismatch", None
        value = _amount(stock, direct, ("profit_unit" if metric == "pe_ttm" else "equity_unit", "financial_unit"))
        return value, "unknown_financial_unit" if value is None else None, "reported_parent_financial"
    ratio_available = _datetime(stock.get(metric + "_available_at"))
    if ratio_available and ratio_available > now:
        return None, "ratio_not_available_at_calculation", None
    ratio = _number(stock.get(metric))
    ratio_date = _snapshot_date(stock, metric + "_date")
    if ratio is None or ratio == 0:
        return None, "missing_explicit_ttm_pe" if metric == "pe_ttm" else "missing_explicit_pb", None
    if ratio_date != valuation_date:
        return None, "ratio_date_mismatch", None
    # Negative PE/PB implies a negative financial contribution and is retained.
    return cap / ratio, None, "implied_from_explicit_" + metric


def _valuation(entity, name, now):
    abbreviation = "PE TTM" if name == "pe_ttm" else "PB"
    result = _metric(name, "样本组合 " + abbreviation)
    stocks = _constituents(entity)
    total_count = next((_number(entity.get(k)) for k in ("constituent_count", "total_constituents", "stocks_total_count")
                        if _number(entity.get(k)) is not None), None)
    if total_count is not None:
        total_count = int(total_count) if total_count >= len(stocks) and total_count > 0 else None
    if total_count is None and entity.get("constituents_complete") is True:
        total_count = len(stocks) or None
    target = _snapshot_date(entity)
    valid_dates = [_snapshot_date(stock, "market_cap_date") for stock in stocks]
    valid_dates = [value for value in valid_dates if value and value <= now.date().isoformat()]
    target = target or max(valid_dates, default=None)
    result["as_of"] = target
    coverage = {"used_count": 0, "listed_count": len(stocks), "total_count": total_count,
                "count_ratio": 0.0 if total_count else None, "market_cap_ratio": None,
                "used_market_cap": 0.0, "total_market_cap": None, "complete": False,
                "valuation_date": target, "currency": str(entity.get("currency") or "CNY").upper()}
    result["coverage"] = coverage
    result["exclusions"] = []
    if not stocks:
        result["reason"] = "没有成分财务，不能根据指数点位或ETF价格推算估值"
        return result
    if target is None or target > now.date().isoformat():
        result["reason"] = "缺少已确认的估值日期，不能混合不同日期的成分数据"
        return result
    numerator, denominator, methods = 0.0, 0.0, set()
    for stock in stocks:
        identity = stock.get("code") or stock.get("id") or stock.get("name")
        reason = None
        cap = _amount(stock, "market_cap", ("market_cap_unit", "cap_unit"))
        cap_date = _snapshot_date(stock, "market_cap_date")
        available_times = [_datetime(stock.get(key)) for key in ("available_at", "observed_at", "fetched_at", "as_of", "market_cap_available_at") if stock.get(key)]
        if any(available and available > now for available in available_times):
            reason = "constituent_not_available_at_calculation"
        elif stock.get("data_quality") == "conflict" or stock.get("validation", {}).get("status") == "conflict":
            reason = "constituent_data_conflict"
        elif cap is None or cap <= 0:
            reason = "missing_or_invalid_total_market_cap"
        elif stock.get("market_cap_basis", "total") not in ("total", "total_market_cap", "总市值"):
            reason = "market_cap_basis_mismatch"
        elif cap_date != target:
            reason = "market_cap_date_mismatch"
        elif _currency(stock, entity) != coverage["currency"]:
            reason = "market_cap_currency_mismatch"
        if reason is None:
            financial, reason, method = _denominator(stock, entity, name, target, cap, now)
            if reason is None:
                numerator += cap
                denominator += financial
                coverage["used_count"] += 1
                methods.add(method)
        if reason:
            result["exclusions"].append({"constituent": identity, "reason": reason})
    coverage["used_market_cap"] = numerator
    coverage["count_ratio"] = coverage["used_count"] / total_count if total_count else None
    entity_total = _amount(entity, "market_cap", ("market_cap_unit", "cap_unit"))
    if entity_total and entity_total > 0 and _snapshot_date(entity, "market_cap_date") == target and entity.get("market_cap_basis", "total") in ("total", "total_market_cap", "总市值"):
        coverage["total_market_cap"] = entity_total
        coverage["market_cap_ratio"] = numerator / entity_total
    elif total_count and len(stocks) == total_count:
        # Every constituent's same-date market cap is known, even if some financials are missing.
        caps = [_amount(s, "market_cap", ("market_cap_unit", "cap_unit"))
                if _snapshot_date(s, "market_cap_date") == target and _currency(s, entity) == coverage["currency"]
                and s.get("market_cap_basis", "total") in ("total", "total_market_cap", "总市值") else None for s in stocks]
        if all(c is not None and c > 0 for c in caps):
            coverage["total_market_cap"] = sum(caps)
            coverage["market_cap_ratio"] = numerator / sum(caps)
    coverage["complete"] = bool(total_count and coverage["used_count"] == total_count and len(stocks) == total_count
                                and (coverage["market_cap_ratio"] is None or abs(coverage["market_cap_ratio"] - 1) <= .005))
    result["sample_count"] = coverage["used_count"]
    result["aggregation"] = {"market_cap_sum": numerator, "financial_sum": denominator, "currency": coverage["currency"],
                             "financial_definition": "parent_net_profit_ttm" if name == "pe_ttm" else "parent_equity",
                             "methods": sorted(methods)}
    result["label"] = ("板块整体 " if coverage["complete"] else "样本组合 ") + abbreviation + ("（自行汇总）" if coverage["complete"] else "")
    if not coverage["used_count"]:
        result["reason"] = "没有同日期、同币种且具备明确财务口径的成分；动态PE不用于TTM估值"
    elif denominator <= 0:
        result.update(status="not_applicable", reason="合计归母净利润TTM≤0，PE不适用" if name == "pe_ttm" else "合计归母股东权益≤0，PB不适用")
    else:
        result.update(value=numerator / denominator, status="ok" if coverage["complete"] else "partial",
                      reason="完整成分同口径自行汇总，非供应商官方估值" if coverage["complete"] else "仅为可用样本组合，覆盖不足或完整成分数未知，不能当作官方板块估值")
    return result


def _adjustment(value):
    raw = str(value or "none").lower()
    if raw in ("none", "unadjusted", "不复权", "raw", ""):
        return "unadjusted"
    if raw in ("total_return", "total-return", "总回报"):
        return "total_return"
    return raw


def _sharpe(entity, now, config):
    adjustment = _adjustment(entity.get("adjustment"))
    label = ("价格收益夏普（未含分红/公司行动影响）" if adjustment == "unadjusted"
             else "总回报夏普" if adjustment == "total_return" else "复权价格收益夏普（非保证总回报口径）")
    result = _metric("sharpe", label)
    annual_rf = _number(config.get("risk_free_rate_annual", config.get("rf_annual", 0.02)))
    result.update(rf_annual=annual_rf, rf_is_assumption=True, annualization_periods=252, ddof=1,
                  min_sample_count=60, max_sample_count=252, return_basis=adjustment)
    if annual_rf is None or annual_rf <= -1:
        result["reason"] = "无风险年收益假设无效，须为大于−100%的有限数值"
        return result
    bars = {}
    adjustments = set()
    for row in entity.get("history", []):
        day, close = _date(row.get("date")), _number(row.get("close"))
        if day is None or day > now.date().isoformat() or close is None or close <= 0:
            continue
        if row.get("complete") is False or row.get("is_complete") is False:
            continue
        if day == now.date().isoformat() and now.time().replace(tzinfo=None) < time(15, 15):
            continue
        available_times = [_datetime(row.get(key)) for key in ("available_at", "as_of", "fetched_at") if row.get(key)]
        if any(available and available > now for available in available_times):
            continue
        trading = is_trading_day(day)
        if trading is False or (trading is None and date.fromisoformat(day).weekday() >= 5):
            continue
        row_adjustment = _adjustment(row.get("adjustment", entity.get("adjustment")))
        bars[day] = (close, row_adjustment)
    days = sorted(bars)[-253:]
    adjustments = {bars[day][1] for day in days}
    if len(adjustments) > 1 or (adjustments and adjustment not in adjustments):
        result["reason"] = "历史价格混合不同复权口径，暂停计算夏普"
        return result
    returns, skipped = [], 0
    for previous, current in zip(days, days[1:]):
        expected = next_trading_day(previous)
        if expected is None:
            expected = date.fromisoformat(previous) + timedelta(days=1)
            while expected.weekday() >= 5:
                expected += timedelta(days=1)
        if expected.isoformat() != current:
            skipped += 1
            continue
        returns.append(bars[current][0] / bars[previous][0] - 1)
    result.update(as_of=days[-1] if days else None, sample_count=len(returns), skipped_nonconsecutive_intervals=skipped,
                  period_start=days[0] if days else None, rf_daily=(1 + annual_rf) ** (1 / 252) - 1)
    if len(returns) < 60:
        result["reason"] = f"需至少60个可用日收益样本，当前{len(returns)}个；跨缺失交易日的收益不作单日收益"
        return result
    excess = [value - result["rf_daily"] for value in returns]
    deviation = statistics.stdev(excess)
    if deviation <= 1e-12:
        result.update(status="not_applicable", reason="样本日收益波动为零或过小，夏普不适用")
        return result
    value = math.sqrt(252) * statistics.mean(excess) / deviation
    result.update(value=value, status="ok", mean_daily_excess_return=statistics.mean(excess),
                  sample_daily_standard_deviation=deviation,
                  reason="历史样本估计；年无风险利率是假设，未复权价格不是含分红的总回报" if adjustment == "unadjusted"
                  else "历史样本估计；年无风险利率是假设，复权或总回报口径以数据声明为准")
    return result


def compute_metrics(dashboard, config=None):
    """Deep-copy a dashboard; add quant_metrics to markets, sectors and proxies.

    Configuration: risk_free_rate_annual (alias rf_annual), default .02; as_of
    is an optional reproducible calculation timestamp, else dashboard.generated_at.
    No fiscal ratios are imputed from dynamic PE or missing constituent data.
    """
    result = deepcopy(dashboard)
    config = config or {}
    now = _datetime(config.get("as_of") or result.get("generated_at")) or shanghai_now()
    entities = []
    for collection in ("market", "sectors"):
        for entity in result.get(collection, []):
            entities.append(entity)
            if isinstance(entity.get("proxy"), dict):
                entities.append(entity["proxy"])
    for entity in entities:
        if entity.get("data_quality") == "conflict" or entity.get("validation", {}).get("status") == "conflict":
            metrics = {name: _metric(name, label, _snapshot_date(entity)) for name, label in
                       (("pe_ttm", "PE TTM"), ("pb", "PB"), ("sharpe", "夏普"))}
            for value in metrics.values():
                value.update(status="blocked", reason="行情交叉验证存在冲突，暂停量化指标")
        else:
            metrics = {"pe_ttm": _valuation(entity, "pe_ttm", now), "pb": _valuation(entity, "pb", now),
                       "sharpe": _sharpe(entity, now, config)}
        entity["quant_metrics"] = {"version": VERSION, "as_of": now.isoformat(), **metrics, "help": deepcopy(HELP)}
    return result
