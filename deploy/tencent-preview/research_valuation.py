"""Dated closing valuations and explicitly labelled constituent estimates."""

from datetime import date, time
import fcntl
import json
import math
import re

from pipeline.calendar import is_trading_day, previous_trading_day
from pipeline.stock_api import FIELDS, _Failure
from relay_common import relay_client
from relay_quotes import publish

APIS = {
    "index": ("index_dailybasic", ("pe", "pe_ttm", "pb", "total_mv", "turnover_rate")),
    "sector": ("sw_daily", ("pe", "pb", "total_mv")),
    "stock": ("daily_basic", ("pe_ttm", "pb", "ps_ttm", "dv_ttm", "total_mv", "turnover_rate")),
}
SOURCE = {kind: f"SXData · {api} 收盘数据" for kind, (api, _) in APIS.items()}
FIELDS_BY_METRIC = {"market_cap": "total_mv", **{key: key for key in
    ("pe", "pe_ttm", "pb", "ps_ttm", "dv_ttm", "turnover_rate")}}
DEFINITIONS = {
    "pe": ("市盈率 PE", "倍", "市值 ÷ 数据源采用的利润", "衡量市场价格相对于利润的倍数。以供应商发布的利润口径为准。"),
    "pe_ttm": ("滚动市盈率 PE TTM", "倍", "总市值 ÷ 最近四个季度净利润之和", "表示市值相当于过去十二个月利润的多少倍。亏损或利润为零时通常不适用。"),
    "pb": ("市净率 PB", "倍", "总市值 ÷ 同口径账面净资产", "衡量市场估值相对于账面权益的倍数，解释时要考虑行业的资产结构。"),
    "ps_ttm": ("滚动市销率 PS TTM", "倍", "总市值 ÷ 最近四个季度营业收入之和", "表示市值相当于过去十二个月收入的多少倍，不能反映利润率。"),
    "dv_ttm": ("近12个月股息率", "%", "近12个月每股现金分红合计 ÷ 收盘价 × 100", "反映历史现金分红与当前价格的比例。"),
    "market_cap": ("总市值", "亿元", "收盘价 × 总股本；组合为成分股总市值之和", "反映市场为公司全部股本给出的估值；指数采用供应商汇总口径。"),
    "turnover_rate": ("换手率", "%", "当日成交量 ÷ 可流通股数 × 100", "衡量当天交易活跃度。指数按供应商发布的流通口径。"),
    "sample_pe_ttm": ("盈利样本滚动市盈率", "倍", "Σ盈利样本总市值 ÷ Σ（同日样本总市值 ÷ 个股PE TTM）",
                      "按同一估值日反推盈利样本利润后汇总。亏损或PE缺失的成分不计入，只反映盈利样本。"),
    "sample_pb": ("成分样本市净率", "倍", "Σ样本总市值 ÷ Σ（同日样本总市值 ÷ 个股PB）",
                  "按同一估值日反推样本账面净资产后汇总，保留源中有效的负净资产。合计权益不为正时不适用。"),
    "member_market_cap": ("成分股总市值合计", "亿元", "Σ同一收盘日各成分股总市值", "汇总板块实际成分股的全股本市值，不按指数权重加权。"),
}
METRICS = {key: dict(key=key, group="valuation", label=label, unit=unit, formula=formula,
                     description=meaning, window="收盘快照") for key, (label, unit, formula, meaning) in DEFINITIONS.items()}
KEYS = {
    "stock": ("pe_ttm", "pb", "ps_ttm", "dv_ttm", "market_cap", "turnover_rate"),
    "index": ("pe_ttm", "pe", "pb", "market_cap", "turnover_rate"),
    "sector": ("pe", "pb", "market_cap"),
}
BOARD_KEYS = ("sample_pe_ttm", "sample_pb", "member_market_cap")


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def valid_value(key, value):
    if not number(value):
        return False
    return value >= 0 if key in {"dv_ttm", "turnover_rate"} else value != 0 if key == "pb" else value > 0


def normalized_rows(kind, rows, day):
    patterns = {"stock": r"[0-9]{6}\.(?:SH|SZ|BJ)", "index": r"[0-9]{6}\.(?:SH|SZ)", "sector": r"[0-9]{6}\.SI"}
    result, conflicts = {}, set()
    for row in rows:
        code = row.get("ts_code")
        if not isinstance(code, str) or not re.fullmatch(patterns[kind], code) or row.get("trade_date") != day.replace("-", ""):
            continue
        values = {}
        for key in KEYS[kind]:
            value = row.get(FIELDS_BY_METRIC[key])
            if valid_value(key, value):
                if key == "market_cap":
                    value /= 1e8 if kind == "index" else 1e4
                values[key] = value
        if code in result and result[code] != values:
            conflicts.add(code)
        result[code] = values
    return {code: values for code, values in result.items() if code not in conflicts and values}


def snapshot_name(kind):
    return f"valuation-{kind}.json"


def read_packet(path):
    try:
        value = json.loads(path.read_bytes())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_snapshot(path, kind, day, values):
    if not values:
        return False
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        saved = read_packet(path)
        receipt = saved.get("trade_date", "")
        if receipt > day or (receipt < day and len(values) < {"index":4, "sector":25, "stock":4000}[kind]):
            return False
        old = saved.get("items", {}) if receipt == day else {}
        # Preserve previously published fields if a receipt is partial.
        combined = dict(old)
        for code, row in values.items():
            combined[code] = {**row, **old.get(code, {})}
        if combined != old:
            publish(path, {"trade_date": day, "items": combined})
        return True


def collect(output, token, now, *, attempts=None, request=None):
    """Refresh one dated market-wide input per call, reusing the shared rate limit."""
    if is_trading_day(now.date()) and time(7) <= now.time() < time(17, 10):
        return 120
    day = previous_trading_day(now.date(), include_today=now.time() >= time(17, 10))
    if not day:
        return 600
    target = day.isoformat()
    attempts = attempts if attempts is not None else {}
    for kind, (api, fields) in APIS.items():
        path = output / snapshot_name(kind)
        # Successful current receipts do not require reparsing the full market.
        mtime = path.stat().st_mtime_ns if path.is_file() else None
        dated_key = (kind, "receipt")
        if attempts.get(dated_key, (None, None))[0] != mtime or dated_key not in attempts:
            attempts[dated_key] = (mtime, read_packet(path).get("trade_date"))
        if (attempts[dated_key][1] or "") >= target or attempts.get(kind, 0) > now.timestamp():
            continue
        FIELDS[api] = ",".join(("ts_code", "trade_date", *fields))
        try:
            params = {"trade_date": day.strftime("%Y%m%d")}
            if request is None:
                with relay_client(token, timeout=22) as client:
                    rows = client.request(api, params)
            else:
                rows = request(api, params)
        except _Failure as error:
            attempts[kind] = now.timestamp() + max(error.retry_seconds, 60 if error.deferred else 900)
            print("research valuation", api, error.code, flush=True)
            return 30
        values = normalized_rows(kind, rows, target)
        if save_snapshot(path, kind, target, values):
            attempts[dated_key] = (path.stat().st_mtime_ns, target)
        else:
            attempts[kind] = now.timestamp() + 900
        print("research valuation", api, target, len(values), flush=True)
        return 30
    return 300


def entry(key, value, day, basis, reason=None, **coverage):
    return {**METRICS[key], "value": value if number(value) else None, "as_of": day or None,
            "basis": basis, "missing_reason": reason, **coverage}


def dated_items(packet, through):
    day = packet.get("trade_date")
    try:
        known = isinstance(day, str) and date.fromisoformat(day).isoformat() == day and is_trading_day(day) is True
    except ValueError:
        known = False
    items = packet.get("items")
    return (day, items) if known and day <= through and isinstance(items, dict) else (None, {})


def asset_metrics(kind, code, through, load, roster=None):
    """Project valuation metrics from site snapshots without requesting new data."""
    if kind == "sector" and not code.endswith(".SI"):
        return constituent_metrics(load(snapshot_name("stock")), through, roster or {})
    day, items = dated_items(load(snapshot_name(kind)), through)
    values = items.get(code, {})
    output = []
    for key in KEYS[kind]:
        value = values.get(key)
        basis = SOURCE[kind]
        if kind == "sector" and key == "pe":
            basis += "；申万行业PE，供应商字段未标明TTM"
        if value is None:
            reason = ("供应商未提供；亏损时该项通常留空" if key == "pe_ttm" and values else "该标的暂无对应收盘估值")
        else:
            reason = None
        output.append(entry(key, value, day if values else None, basis, reason))
    return output


def constituent_metrics(packet, through, roster):
    day, items = dated_items(packet, through)
    codes = {row["code"] for row in roster.get("members", [])}
    total = len(codes)
    observed = [items[code] for code in codes if code in items]
    membership = roster.get("as_of")
    basis = "SXData 个股收盘数据 · 按已核验成分汇总"
    if not membership:
        basis += "；使用当前名单回看，成分生效日未提供"
    elif day and membership > day:
        basis += "；使用后续名单回看"
    output = []
    for key, field in zip(BOARD_KEYS, ("pe_ttm", "pb", None)):
        samples = [row for row in observed if number(row.get("market_cap")) and row["market_cap"] > 0
                   and (field is None or valid_value(field, row.get(field)))]
        count = len(samples)
        minimum = total if field is None else min(total, max(3, math.ceil(total * .5)))
        reason = None
        value = None
        if not day:
            reason = "尚无可用个股收盘估值"
        elif roster.get("status") != "ready" or not total:
            reason = "成分名单尚未完整核验"
        elif count < minimum:
            reason = "成分估值覆盖不足"
        elif field is None:
            value = math.fsum(row["market_cap"] for row in samples)
        else:
            numerator = math.fsum(row["market_cap"] for row in samples)
            denominator = math.fsum(row["market_cap"] / row[field] for row in samples)
            if denominator > 0:
                value = numerator / denominator
            else:
                reason = "样本净资产或利润合计不为正"
        output.append(entry(key, value, day, basis, reason, sample_count=count, total_count=total or None,
                            required_samples=minimum or None, membership_as_of=membership,
                            coverage_pct=count / total * 100 if total else None))
    return output
