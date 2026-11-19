"""Offline quoted-price and completed-session factors for stocks, indices and sectors."""
from datetime import date, datetime, timedelta
from functools import lru_cache
import math
from statistics import mean, pstdev, stdev

from pipeline.calendar import SHANGHAI, is_trading_day, previous_trading_day


INTRADAY = {
    "amplitude_pct": ("盘中振幅", "%", "（最高价 − 最低价）÷ 昨收 × 100", "最高价与最低价均取同一份盘中行情。"),
    "opening_gap_pct": ("开盘跳空", "%", "（开盘价 ÷ 昨收 − 1）× 100", "衡量当天开盘相对行情所报昨收的变化。"),
    "relative_hs300_pp": ("相对沪深300涨幅", "个百分点", "标的涨跌幅 − 沪深300涨跌幅", "比较同一交易日、源时点相差不超过90秒的两份行情。"),
}
DAILY = {
    "ma20_bias_pct": ("20日均线乖离率", "%", "（当前收盘水平 ÷ 最近20日收盘水平均值 − 1）× 100",
                      "正值表示高于20日均线。", "20个收盘", 20),
    "momentum20_pct": ("20日动量", "%", "（最近20个日收益的累计增长倍数 − 1）× 100",
                       "从20个交易日前收盘计算到本期收盘。", "20个日收益", 20),
    "volatility20_pct": ("20日年化波动率", "%", "20个日收益的样本标准差 × √252 × 100",
                         "以每年252个交易日折算，标准差的分母为19。", "20个日收益", 20),
    "drawdown60_pct": ("60日最大回撤", "%", "max［1 − 当日收盘水平 ÷ 窗口内此前最高收盘水平］× 100",
                       "按60个收盘点依次更新峰值，以正数表示窗口内最大跌幅。", "60个收盘", 60),
    "beta60": ("60日沪深300贝塔", "倍", "标的与沪深300日收益的样本协方差 ÷ 沪深300日收益样本方差",
               "每对日收益必须来自相同的相邻交易日，基准无波动时无法估计。", "60对日收益", 60),
}
BREADTH = {
    "breadth_up_pct": ("成分股上涨占比", "%", "日涨幅大于0的成分数 ÷ 有效成分数 × 100",
                       "平盘计入分母，以实际有成交且有日收益的成分股为样本。"),
    "breadth_dispersion_pp": ("成分股涨跌离散度", "个百分点", "√［Σ（个股涨跌幅 − 样本平均涨跌幅）² ÷ 有效成分数］",
                             "给出当日成分股涨跌幅的横截面标准差，涨跌幅以百分数计量。"),
}
METRICS = {
    **{key: dict(key=key, label=v[0], unit=v[1], formula=v[2], description=v[3],
                 group="intraday", window="同期两份行情" if key=="relative_hs300_pp" else "一份盘中行情",
                 required_samples=2 if key=="relative_hs300_pp" else 1) for key, v in INTRADAY.items()},
    **{key: dict(key=key, label=v[0], unit=v[1], formula=v[2], description=v[3], group="daily",
                 window=v[4], required_samples=v[5]) for key, v in DAILY.items()},
    **{key: dict(key=key, label=v[0], unit=v[1], formula=v[2], description=v[3], group="daily",
                 window="单日成分截面", required_samples=5) for key,v in BREADTH.items()},
}
STOCK_BASIS = "逐日按供应商参考昨收计算收益并连乘"
INDEX_BASIS = "指数发布方的实际收盘点位"


def _number(value, positive=False):
    return type(value) in (int, float) and math.isfinite(value) and (not positive or value > 0)


def _day(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        return None


def _stamp(quote):
    try:
        instant = datetime.fromisoformat(quote["quote_timestamp"])
    except (TypeError, ValueError, KeyError):
        return None
    if instant.tzinfo is None:
        return None
    instant = instant.astimezone(SHANGHAI)
    if _day(quote.get("quote_date")) != instant.date().isoformat() or is_trading_day(instant.date()) is not True:
        return None
    return instant


def _entry(key, value, samples, as_of, basis, reason=None, **details):
    if value is not None and not _number(value):
        value, reason = None, "计算结果超出有效数值范围"
    return {**METRICS[key], "value":value, "sample_count":samples, "as_of":as_of,
            "basis":basis, "missing_reason":reason, **details}


def _change(quote):
    price, prior = quote.get("price"), quote.get("previous_close")
    if _number(price, True) and _number(prior, True):
        return (price / prior - 1) * 100
    pct = quote.get("change_pct")
    return pct if _number(pct) else None


def _intraday(quote, benchmark):
    stamp, base_stamp = _stamp(quote), _stamp(benchmark)
    day = stamp.date().isoformat() if stamp else None
    basis = {"hot":"概念热榜涨跌幅", "estimate":"成分股估算涨跌幅",
             "constituent_estimate":"成分股估算涨跌幅"}.get(quote.get("basis"), "中转站盘中实际行情")
    direct = basis == "中转站盘中实际行情"
    previous, opening, high, low, price = (quote.get(k) for k in ("previous_close","open","high","low","price"))
    is_range = _number(high, True) and _number(low, True) and high >= low
    consistent = not is_range or all(low <= p <= high for p in (opening, price) if _number(p, True))
    values = {
        "amplitude_pct": (high-low)/previous*100 if direct and stamp and _number(previous, True) and is_range and consistent else None,
        "opening_gap_pct": (opening/previous-1)*100 if direct and stamp and _number(previous, True) and _number(opening, True) and consistent else None,
    }
    result = []
    for key, value in values.items():
        reason = None if value is not None else ("缺少有效的行情源时点" if not stamp else
            "该来源只提供涨跌幅参考" if not direct else "缺少有效的盘中高低价或昨收" if key=="amplitude_pct" else "缺少有效的开盘价或昨收")
        result.append(_entry(key, value, int(value is not None), day, basis, reason,
                             quote_timestamp=stamp.isoformat() if stamp else None))
    own, base = _change(quote), _change(benchmark)
    usable = [stamp is not None and own is not None, base_stamp is not None and base is not None]
    paired = all(usable) and stamp.date() == base_stamp.date() and abs(stamp-base_stamp) <= timedelta(seconds=90)
    reason = None if paired else "标的或沪深300缺少带有源时点的有效涨跌幅" if not all(usable) else "两份行情日期不同或源时点相差超过90秒"
    result.append(_entry("relative_hs300_pp", own-base if paired else None, 2 if paired else min(1,sum(usable)), day,
                         basis+"，基准为沪深300", reason, quote_timestamp=stamp.isoformat() if stamp else None,
                         benchmark_timestamp=base_stamp.isoformat() if base_stamp else None))
    return result


@lru_cache(maxsize=2048)
def _previous(day):
    prev = previous_trading_day(day)
    return prev.isoformat() if prev else None


def trusted_daily_source(source):
    return isinstance(source, str) and (
        source in {"stock_api","equity_cache","daily","sw_daily","index_daily","ths_daily","sxdata"}
        or source.startswith("sxdata_"))


def _bars(raw, sessions):
    """Trust only a single unambiguous close per date in the supplied completed sessions."""
    wanted = set(sessions)
    result = {}
    if isinstance(raw, dict):
        rows = ((day, raw.get(day)) for day in sessions if day in raw)
    else:
        rows = ((None, row) for row in (raw or []))
    for day_key, row in rows:
        if not isinstance(row, dict):
            continue
        day = _day(row.get("date") or row.get("trade_date") or day_key)
        if day not in wanted or (day_key and _day(day_key) != day) or row.get("complete") is False:
            continue
        source = row.get("provider") or row.get("source")
        if source and not trusted_daily_source(source):
            continue
        if not _number(row.get("close"), True):
            continue
        values = (row["close"], row.get("pre_close"), row.get("adjustment"))
        if day not in result:
            result[day] = values
        elif result[day] != values:
            result[day] = None
    return result


def _returns(days, bars, stock):
    values = []
    for prior_day, day in zip(days, days[1:]):
        prev, cur = bars.get(prior_day), bars.get(day)
        value = None
        if prev and cur and _previous(day) == prior_day:
            denominator = cur[1] if stock else prev[0]
            valid_basis = not stock or cur[2] in (None, "unadjusted", "none")
            if valid_basis and _number(denominator, True):
                candidate = cur[0]/denominator - 1
                if _number(candidate) and candidate > -1:
                    value = candidate
        values.append(value)
    return values


def _levels(returns):
    levels = [1.0]
    for change in returns:
        levels.append(levels[-1] * (1 + change))
    return levels


def _daily(kind, raw, base_raw, sessions):
    days = tuple(sorted({day for value in sessions if (day := _day(value)) and is_trading_day(day) is True}))[-61:]
    own, base = _bars(raw, days), _bars(base_raw, days)
    returns = _returns(days, own, kind == "stock")
    bench = _returns(days, base, False)
    basis = STOCK_BASIS if kind == "stock" else INDEX_BASIS
    as_of = next((day for day in reversed(days) if own.get(day)), None)
    output = []
    for key, spec in DAILY.items():
        needed = spec[5]
        levels_based = key in {"ma20_bias_pct","drawdown60_pct"}
        need_returns = needed - int(levels_based)
        selected = returns[-need_returns:]
        sample = sum(x is not None for x in selected)
        if levels_based and days:
            if kind == "stock":
                first = days[max(0,len(days)-needed)]
                sample += int(bool(own.get(first)))
            else:
                sample = sum(bool(own.get(day)) for day in days[-needed:])
        if key == "beta60":
            sample = sum(x is not None and y is not None for x,y in zip(selected,bench[-need_returns:]))
        value = reason = None
        if sample < needed or len(selected) < need_returns or any(r is None for r in selected):
            reason = "最近已收盘交易日缺少有效日线" if days and as_of != days[-1] else (
                "同期配对收益不足，窗口含缺失或不相邻交易日" if key=="beta60" else
                "连续交易日或参考昨收不足" if kind=="stock" else "连续交易日收盘样本不足")
        elif key == "ma20_bias_pct":
            levels = _levels(selected)
            value = (levels[-1]/mean(levels)-1)*100
        elif key == "momentum20_pct":
            value = (math.prod(1+r for r in selected)-1)*100
        elif key == "volatility20_pct":
            value = stdev(selected)*math.sqrt(252)*100
        elif key == "drawdown60_pct":
            peak, v = 1.0, 0.0
            for level in _levels(selected):
                peak = max(peak,level)
                v = max(v,1-level/peak)
            value = v*100
        else:
            matched = bench[-needed:]
            ex, ey = mean(matched), mean(selected)
            denominator = sum((x-ex)**2 for x in matched)
            if denominator <= 1e-20:
                reason = "同期沪深300日收益无可辨别的波动"
            else:
                value = sum((x-ex)*(y-ey) for x,y in zip(matched,selected))/denominator
        metric_as_of = next((day for day in reversed(days) if own.get(day) and base.get(day)), None) if key=="beta60" else as_of
        output.append(_entry(key,value,sample,metric_as_of,basis+"，基准为沪深300" if key=="beta60" else basis,
                             reason,through=days[-1] if days else None))
    return output


def compute(kind, quote, benchmark_quote, bars, benchmark_bars, *, sessions):
    """sessions supplies the actual exchange calendar up to the last completed target session."""
    if kind not in {"stock","index","sector"}:
        raise ValueError("kind must be stock, index or sector")
    return _intraday(quote or {}, benchmark_quote or {}) + _daily(kind, bars, benchmark_bars, sessions)


def board_breadth(members, returns, day, *, membership_as_of=None, checked_at=None, complete=False):
    """Descriptive cross section of a verified roster using actual traded stock returns in percent."""
    codes = set()
    invalid = False
    for member in members or []:
        code = (member.get("code") or member.get("ts_code")) if isinstance(member, dict) else member
        if not isinstance(code, str) or not code:
            invalid = True
        else:
            codes.add(code)
    requested = _day(day)
    observed = returns or {}
    actual = [observed[code] for code in codes if _number(observed.get(code)) and observed[code] >= -100]
    n, total = len(actual), len(codes)
    required = max(5,(4*total+4)//5)
    coverage = n/total*100 if total else 0.0
    roster_day = _day(membership_as_of)
    if roster_day:
        basis = "按"+roster_day+"成分名单计算"
        if requested and roster_day > requested:
            basis += "，使用后续名单回看历史截面"
    else:
        basis = "来源未给成分生效日，按当前核验名单回看该交易日截面"
    basis += f"，已获日收益 {n}/{total} 只"
    reason = None
    if not requested or is_trading_day(requested) is not True:
        reason = "缺少已核实的交易日期"
    elif complete is not True or invalid or not total:
        reason = "成分名单尚未完整核验"
    elif n < 5:
        reason = "有实际交易和有效日收益的成分不足5只"
    elif n < required:
        reason = "有效成分覆盖未达到80%"
    values = [None,None] if reason else [sum(x>0 for x in actual)/n*100, pstdev(actual)]
    return [_entry(key,value,n,requested if n and requested else None,basis,reason,through=requested,
                   total_count=total,required_samples=required,coverage_pct=coverage,
                   membership_as_of=roster_day,checked_at=checked_at) for key,value in zip(BREADTH,values)]
