"""Auditable daily/PIT and same-clock intraday forecasts with delayed labels.

Optional valuation and sentiment observations need actual availability times.
Both tasks compare small models with a zero-return/persistence baseline using
chronological selection and confirmation; insufficient data never forces a fit.
"""
from copy import deepcopy
from datetime import date, datetime, time
import hashlib
import json
import math
import os
import warnings

from .calendar import SHANGHAI, is_trading_day, market_session, next_trading_day, previous_trading_day, shanghai_now

FEATURE_VERSION = "daily-pit-multifactor-v2"
DEFAULTS = {"enable_mlp": False, "training_enabled": True, "min_train": 60, "holdout_size": 20,
            "max_train": 504, "improvement_margin": 0.02,
            "enable_hgb": True, "hgb_min_train": 200, "intraday_min_train": 80,
            "quote_max_age_minutes": 20, "close_confirm_minutes": 15}

FEATURE_NAMES = (
    "return_1d_pct", "mean_return_5d_pct", "mean_return_20d_pct", "volatility_20d_pct",
    "open_close_return_pct", "range_pct", "return_5d_pct", "return_20d_pct", "price_to_ma20_pct",
    "volume_ratio_5d", "volume_ratio_20d", "amount_ratio_20d", "volume_change_1d_pct", "amount_change_1d_pct",
    "turnover_rate_pct", "vendor_volume_ratio", "log_vendor_pe", "log_vendor_pe_ttm", "log_vendor_pb",
    "log_market_cap_cny", "sentiment_score", "sentiment_confidence", "sentiment_article_count_log",
)
SENTIMENT_FEATURES = {"sentiment_score", "sentiment_confidence", "sentiment_article_count_log"}


def training_enabled(config=None):
    """An explicit environment or configuration prohibition always wins."""
    disabled = {"false", "0", "no", "off"}
    configured = (config or {}).get("training_enabled", True)
    return (str(configured).strip().lower() not in disabled
            and os.environ.get("RADAR_ALLOW_TRAINING", "true").strip().lower() not in disabled)


def _feature_cutoff(day):
    # Historical daily features must exist before the following opening label.
    return datetime.combine(date.fromisoformat(day), time(23, 59, 59), SHANGHAI)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _timestamp(value):
    if not value:
        return None
    try:
        return shanghai_now(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (ValueError, TypeError):
        return None


def _quote_available(quote, now):
    for key in ("available_at", "fetched_at"):
        if quote.get(key):
            available = _timestamp(quote[key])
            if available is None or available > now:
                return False
    return True


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, allow_nan=False).encode()).hexdigest()[:24]


def _history(entity, now, config, completed_only=True):
    """Ignore future bars, future publication timestamps and partial closes."""
    rows = {}
    for raw in entity.get("history", []):
        try:
            day = date.fromisoformat(str(raw.get("date", ""))[:10])
        except ValueError:
            continue
        if day > now.date():
            continue
        if is_trading_day(day) is False:
            continue
        available = _timestamp(raw.get("price_available_at") or raw.get("available_at") or raw.get("as_of"))
        if available and available > now:
            continue
        close, opening = _number(raw.get("close")), _number(raw.get("open"))
        if completed_only:
            if raw.get("complete") is False or raw.get("is_complete") is False:
                continue
            if day == now.date() and now.time().replace(tzinfo=None) < time(15, config["close_confirm_minutes"]):
                continue
            if close is None or close <= 0:
                continue
        elif day == now.date() and now.time().replace(tzinfo=None) < time(9, 30):
            continue
        rows[day] = {**raw, "date": day.isoformat(), "open": opening, "close": close}
    return [rows[d] for d in sorted(rows)]


def _pit_record(entity, day, cutoff, keys, max_age_days=0):
    """Only recorded, dated observations; current entity values are never backfilled."""
    records = []
    identity = str(entity.get("id") or entity.get("index_code") or entity.get("code") or "").split(":", 1)[0].split(".", 1)[0]
    for key in keys:
        for row in entity.get(key, []) or []:
            if not isinstance(row, dict):
                continue
            if key == "sentiment_history" and row.get("window_policy_version") != "published-and-observed-v1":
                # Earlier snapshots could admit old articles first discovered
                # today. Only the verified publication/observation window is a
                # valid training feature; legacy evidence stays in the audit log.
                continue
            if key == "sentiment_history" and identity in ("801950", "801980"):
                # v1 reversed the coal/beauty identifiers. Keep those snapshots
                # for audit, but never use them as historical model evidence.
                version = str(row.get("pipeline_version") or "")
                prefix = "sw2021-news-tone-v"
                number = version[len(prefix):] if version.startswith(prefix) else ""
                if not number.isdigit() or int(number) < 2:
                    continue
            available = _timestamp(row.get("available_at") or row.get("fetched_at"))
            raw_day = row.get("date") or row.get("trade_date")
            try:
                observed_day = date.fromisoformat(str(raw_day)[:10])
            except ValueError:
                continue
            # An as_of market timestamp alone is not evidence of publication time.
            if available is None or available > cutoff or observed_day > cutoff.date():
                continue
            age = (date.fromisoformat(day) - observed_day).days
            if max_age_days == 0 and age != 0:
                continue
            if max_age_days and not (0 <= (cutoff.date() - observed_day).days <= max_age_days):
                continue
            records.append((available, row))
    return max(records, key=lambda item: item[0])[1] if records else {}


def _flow_value(row, key, cutoff=None):
    available = _timestamp(row.get(key + "_available_at") or row.get("available_at"))
    if cutoff is not None and available is not None and available > cutoff:
        return None, None
    value = _number(row.get(key))
    if value is None or value < 0:
        return None, None
    unit = row.get(key + "_unit")
    scales = ({"share": 1, "shares": 1, "股": 1, "lot": 100, "手": 100, "万股": 10000,
               "10k_shares": 10000} if key == "volume" else
              {"CNY": 1, "元": 1, "CNY_thousand": 1000, "千元": 1000,
               "CNY_10000": 10000, "万元": 10000, "亿元": 1e8})
    if unit in scales:
        return value * scales[unit], "normalized"
    # Ratios are valid for one consistent provider/unit, even if its scale is unknown.
    return value, (row.get("provider") or row.get("source") or "unspecified", unit or "unspecified")


def _flow_ratio(window, key, periods, change=False, cutoff=None):
    latest, unit = _flow_value(window[-1], key, cutoff)
    prior = [_flow_value(row, key, cutoff) for row in window[-periods-1:-1]]
    if latest is None or len(prior) != periods or any(v is None or u != unit for v, u in prior):
        return None
    mean = sum(v for v, _ in prior) / periods
    if mean <= 0:
        return None
    ratio = latest / mean
    return 100 * (ratio - 1) if change else ratio


def _features(rows, index, entity=None, cutoff=None):
    if index < 20:
        return None
    window = rows[index - 20:index + 1]
    cutoff = cutoff or _feature_cutoff(window[-1]["date"])
    if any((_timestamp(row.get("price_available_at") or row.get("available_at")) or cutoff) > cutoff for row in window):
        return None
    adjustments = {row["adjustment"] for row in window if row.get("adjustment")}
    if len(adjustments) > 1:
        return None
    for left, right in zip(window, window[1:]):
        expected = next_trading_day(left["date"])
        if expected is not None and expected.isoformat() != right["date"]:
            return None
    closes = [r["close"] for r in window]
    if any(v is None or v <= 0 for v in closes):
        return None
    returns = [100 * (b / a - 1) for a, b in zip(closes, closes[1:])]
    mean = sum(returns) / len(returns)
    opening = _number(window[-1].get("open"))
    high, low = _number(window[-1].get("high")), _number(window[-1].get("low"))
    entity = entity or {}
    valuation = _pit_record(entity, window[-1]["date"], cutoff,
                            ("feature_history", "daily_basic_history", "valuation_history"))
    sentiment = _pit_record(entity, window[-1]["date"], cutoff, ("sentiment_history",), max_age_days=3)
    score = _number(sentiment.get("score"))
    articles = _number(sentiment.get("article_count"))
    if score is None or not -1 <= score <= 1 or articles is None or articles <= 0:
        sentiment, score, articles = {}, None, None
    def positive_log(value):
        value = _number(value)
        return math.log(value) if value is not None and value > 0 else None
    cap = valuation.get("market_cap_cny", valuation.get("total_market_cap"))
    if cap is None and _number(valuation.get("total_mv")) is not None:
        cap = _number(valuation["total_mv"]) * 10000
    confidence = _number(sentiment.get("confidence"))
    confidence = confidence if confidence is not None and 0 <= confidence <= 1 else None
    values = [returns[-1], sum(returns[-5:]) / 5, mean,
              (sum((r - mean) ** 2 for r in returns) / len(returns)) ** .5,
              100 * (closes[-1] / opening - 1) if opening and opening > 0 else None,
              100 * (high - low) / closes[-1] if high and low and high >= low else None,
              100 * (closes[-1] / closes[-6] - 1), 100 * (closes[-1] / closes[0] - 1),
              100 * (closes[-1] / (sum(closes[-20:]) / 20) - 1),
              _flow_ratio(window, "volume", 5, cutoff=cutoff), _flow_ratio(window, "volume", 20, cutoff=cutoff),
              _flow_ratio(window, "amount", 20, cutoff=cutoff), _flow_ratio(window, "volume", 1, True, cutoff),
              _flow_ratio(window, "amount", 1, True, cutoff),
              _number(valuation.get("turnover_rate")), _number(valuation.get("volume_ratio")),
              positive_log(valuation.get("vendor_pe")),
              positive_log(valuation.get("vendor_pe_ttm", valuation.get("pe_ttm"))),
              positive_log(valuation.get("vendor_pb", valuation.get("pb"))), positive_log(cap),
              score, confidence, math.log1p(articles) if articles else None]
    values = [_number(value) for value in values]
    return [v if v is not None else 0.0 for v in values] + [float(v is None) for v in values]


def _samples(rows, entity=None):
    samples = []
    for i in range(20, len(rows) - 1):
        expected = next_trading_day(rows[i]["date"])
        nxt = rows[i + 1]
        if expected is None or expected.isoformat() != nxt["date"]:
            continue
        x, opening = _features(rows, i, entity), _number(nxt.get("open"))
        if x is not None and opening and opening > 0:
            samples.append({"x": x, "y": 100 * (opening / rows[i]["close"] - 1),
                            "feature_date": rows[i]["date"], "label_date": nxt["date"]})
    return samples


def _fit(name, x, y):
    import numpy as np
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    if name == "ridge":
        from sklearn.linear_model import Ridge
        estimator = Ridge(alpha=10.0)
    elif name == "hgb":
        from sklearn.ensemble import HistGradientBoostingRegressor
        estimator = HistGradientBoostingRegressor(max_iter=60, max_leaf_nodes=7,
                    min_samples_leaf=20, l2_regularization=10.0, learning_rate=.05,
                    early_stopping=False, random_state=17)
    elif name == "mlp":
        from sklearn.neural_network import MLPRegressor
        estimator = MLPRegressor(hidden_layer_sizes=(32, 16), activation="relu", alpha=1.0,
                                 max_iter=300, early_stopping=False, shuffle=False,
                                 random_state=17, learning_rate_init=.001)
    else:
        raise ValueError("unknown model")
    model = make_pipeline(StandardScaler(), estimator)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model.fit(np.asarray(x, dtype=float), np.asarray(y, dtype=float))
    return model


def _feature_metadata(samples, latest, active, names):
    n, width = len(samples), len(names)
    coverage = []
    for i, name in enumerate(names):
        present = sum(s["x"][width + i] == 0 for s in samples)
        coverage.append({"name": name, "present_count": present, "total_count": n,
                         "coverage": present / n if n else 0,
                         "latest_available": latest[width + i] == 0,
                         "active": i in active or width + i in active})
    return {"version": FEATURE_VERSION, "names": list(names), "active_names": [c["name"] for c in coverage if c["active"]],
            "used_names": [], "selected_model": None,
            "coverage": coverage, "missing_policy": "zero placeholder plus explicit missing flag; activation uses initial training window only",
            "point_in_time_policy": "dated auxiliary records need actual available_at/fetched_at; no current-value historical backfill",
            "sentiment_training_sample_count": sum(s["x"][width + names.index("sentiment_score")] == 0 for s in samples) if "sentiment_score" in names else 0}


def _train_samples(samples, latest_x, config, old_info, names=FEATURE_NAMES, task="next_open"):
    import numpy as np
    samples = samples[-config["max_train"]:]
    count, holdout = len(samples), config["holdout_size"]
    required = (config["intraday_min_train"] if task == "today_close" else config["min_train"]) + 2 * holdout
    cache_key = _hash({"version": FEATURE_VERSION, "task": task, "samples": samples, "latest": latest_x,
                       "config": {k: config[k] for k in ("enable_mlp", "enable_hgb", "hgb_min_train", "min_train", "intraday_min_train", "holdout_size", "max_train", "improvement_margin")}})
    cached = old_info.get("cached_outcome")
    if old_info.get("input_hash") == cache_key and isinstance(cached, dict) and cached.get("model_version"):
        return {**deepcopy(cached), "trained": False, "cache_hit": True}, None
    if not training_enabled(config):
        return None, {"reason": "云端只推断，等待本机训练模型；当前输入没有完全匹配的已保存结果。",
                      "inference_only": True, "sample_count": count,
                      "feature_metadata": _feature_metadata(samples, latest_x, [], names)}
    if count < required:
        return None, {"reason": f"{'同时间盘中' if task == 'today_close' else '隔夜'}模型需至少 {required} 个已完成交易样本，当前 {count} 个",
                      "sample_count": count, "feature_metadata": _feature_metadata(samples, latest_x, [], names)}
    x, y = np.asarray([r["x"] for r in samples]), np.asarray([r["y"] for r in samples])
    a, b = count - 2 * holdout, count - holdout
    width = len(names)
    active = []
    for i in range(width):
        # No feature is enabled using holdout coverage or a newly arrived current value.
        present = np.count_nonzero(x[:a, width + i] == 0)
        if present >= max(10, math.ceil(a * .2)) and latest_x[width + i] == 0:
            if np.var(x[:a, i]) > 1e-16:
                active.append(i)
            if np.var(x[:a, width + i]) > 1e-16:
                active.append(width + i)
    metadata = _feature_metadata(samples, latest_x, active, names)
    x = x[:, active]
    predictions = {"zero_return": (np.zeros(holdout), np.zeros(holdout))}
    failures = []
    candidates = ["ridge"]
    if config["enable_hgb"] and a >= config["hgb_min_train"]:
        candidates.append("hgb")
    if config["enable_mlp"]:
        candidates.append("mlp")
    for name in candidates if active else []:
        try:
            selected = _fit(name, x[:a], y[:a]).predict(x[a:b])
            confirmed = _fit(name, x[:b], y[:b]).predict(x[b:])
            if not (np.isfinite(selected).all() and np.isfinite(confirmed).all()):
                raise ValueError("non-finite prediction")
            predictions[name] = (selected, confirmed)
        except (ImportError, ValueError, RuntimeError, ArithmeticError) as exc:
            failures.append(f"{name}: {type(exc).__name__}")
    scores = {name: {"selection_mae_pct": float(np.mean(np.abs(y[a:b] - pred[0]))),
                     "confirmation_mae_pct": float(np.mean(np.abs(y[b:] - pred[1])))}
              for name, pred in predictions.items()}
    incumbent = old_info.get("champion", "zero_return")
    if incumbent not in predictions:
        incumbent = "zero_return"
    candidate = min(scores, key=lambda k: scores[k]["selection_mae_pct"])
    margin = 1 - config["improvement_margin"]
    baseline_score = scores["zero_return"]["confirmation_mae_pct"]
    champion = incumbent
    # A later, untouched window confirms selection; it is not searched for a winner.
    if candidate != incumbent and scores[candidate]["confirmation_mae_pct"] < min(baseline_score, scores[incumbent]["confirmation_mae_pct"]) * margin:
        champion = candidate
    if scores[champion]["confirmation_mae_pct"] > baseline_score:
        champion = "zero_return"
    metadata["selected_model"] = champion
    metadata["used_names"] = metadata["active_names"] if champion != "zero_return" else []
    for item in metadata["coverage"]:
        item["used"] = item["active"] and champion != "zero_return"
    predicted = 0.0
    if champion != "zero_return":
        try:
            predicted = float(_fit(champion, x, y).predict([[latest_x[i] for i in active]])[0])
        except (ImportError, ValueError, RuntimeError, ArithmeticError) as exc:
            return None, {"reason": "最终训练失败，暂停该模型输出：" + type(exc).__name__,
                          "sample_count": count, "feature_metadata": _feature_metadata(samples, latest_x, [], names)}
    if not math.isfinite(predicted):
        return None, {"reason": "模型产生非有限预测，已拒绝输出", "sample_count": count}
    residuals = y[b:] - predictions[champion][1]
    q10, q90 = [float(v) for v in np.quantile(residuals, [.1, .9])]
    validation = {"method": "two-stage chronological walk-forward", "selection_count": holdout,
                  "confirmation_count": holdout, "selection_start": samples[a]["label_date"],
                  "confirmation_start": samples[b]["label_date"], "scores": scores,
                  "mae_pct": scores[champion]["confirmation_mae_pct"],
                  "baseline_mae_pct": baseline_score,
                  "note": "历史样本外比较；区间来自确认窗口残差分位数，不保证未来覆盖率"}
    version = f"{champion}-{task}-{FEATURE_VERSION}-{cache_key}"
    return {"predicted_return_pct": predicted, "lower_return_pct": predicted + q10,
            "upper_return_pct": predicted + q90, "model_version": version,
            "training_cutoff": samples[-1]["label_date"], "feature_values": latest_x,
            "validation": validation, "sample_count": count,
            "champion": champion, "trained": len(predictions) > 1,
            "failures": failures, "input_hash": cache_key, "cache_hit": False,
            "feature_metadata": metadata}, None


def _daily_model(rows, config, old_info, entity=None, now=None):
    latest = _features(rows, len(rows) - 1, entity, now)
    if latest is None:
        return None, {"reason": "最新特征不足", "sample_count": 0}
    return _train_samples(_samples(rows, entity), latest, config, old_info)


def _with_source_flows(rows, entity, now):
    """Supplement missing daily volume/amount only for matching prices/instruments."""
    result = deepcopy(rows)
    sources = entity.get("source_history", {})
    if not isinstance(sources, dict):
        return result
    supplements = {}
    for source_rows in sources.values():
        if not isinstance(source_rows, list):
            continue
        for bar in _history({"history": source_rows}, now, DEFAULTS):
            supplements.setdefault(bar["date"], []).append(bar)
    for row in result:
        for other in supplements.get(row["date"], []):
            if abs(other["close"] / row["close"] - 1) > .001:
                continue
            if row.get("adjustment") and other.get("adjustment") and row["adjustment"] != other["adjustment"]:
                continue
            for key in ("volume", "amount"):
                if _number(row.get(key)) is None and _number(other.get(key)) is not None:
                    value, unit = _flow_value(other, key)
                    # Unknown units cannot be relabelled as the primary provider's units.
                    if value is not None and unit == "normalized":
                        row[key], row[key + "_unit"] = value, "share" if key == "volume" else "CNY"
                        row[key + "_available_at"] = other.get(key + "_available_at") or other.get("price_available_at") or other.get("available_at") or other.get("as_of")
    return result


INTRADAY_NAMES = FEATURE_NAMES + ("live_return_pct", "live_open_gap_pct", "live_since_open_pct")


def _intraday_features(rows, reference_index, entity, point, cutoff):
    daily = _features(rows, reference_index, entity, cutoff)
    price, opening = _number(point.get("price")), _number(point.get("open"))
    if daily is None or price is None or price <= 0:
        return None
    reference = rows[reference_index]["close"]
    extra = [100 * (price / reference - 1),
             100 * (opening / reference - 1) if opening and opening > 0 else None,
             100 * (price / opening - 1) if opening and opening > 0 else None]
    width = len(FEATURE_NAMES)
    return daily[:width] + [v if v is not None else 0 for v in extra] + daily[width:] + [float(v is None) for v in extra]


def _intraday_model(entity, series, rows, quote, quote_time, now, config, old_info):
    """One truly observed snapshot per date, at/before the same clock time, then close."""
    latest = _intraday_features(rows, len(rows) - 1, entity, quote, now)
    if latest is None:
        return None, {"reason": "盘中模型缺少完整日线特征", "sample_count": 0}
    by_day, candidates = {b["date"]: (i, b) for i, b in enumerate(rows)}, {}
    for point in (series or {}).get("points", []):
        stamp = _timestamp(point.get("quote_timestamp") or point.get("timestamp"))
        observed = _timestamp(point.get("fetched_at"))
        if (stamp is None or observed is None or point.get("timestamp_quality") != "source_reported"
                or stamp.date() >= now.date() or stamp > observed or observed > now):
            continue
        quote_cutoff = stamp.replace(hour=quote_time.hour, minute=quote_time.minute, second=quote_time.second)
        cutoff = stamp.replace(hour=now.hour, minute=now.minute, second=now.second, microsecond=now.microsecond)
        lag = (quote_cutoff - stamp).total_seconds()
        # No later same-day point and no snapshot learned after this clock time.
        if not 0 <= lag <= 15 * 60 or observed > cutoff or stamp.time().replace(tzinfo=None) < time(9, 30):
            continue
        key = stamp.date().isoformat()
        if key not in candidates or stamp > candidates[key][0]:
            candidates[key] = (stamp, point, cutoff)
    samples = []
    for day, (stamp, point, cutoff) in sorted(candidates.items()):
        previous = previous_trading_day(day)
        if previous is None or previous.isoformat() not in by_day or day not in by_day:
            continue
        i, _ = by_day[previous.isoformat()]
        x = _intraday_features(rows, i, entity, point, cutoff)
        price = _number(point.get("price"))
        if x is not None and price and price > 0:
            samples.append({"x": x, "y": 100 * (by_day[day][1]["close"] / price - 1),
                            "feature_date": day, "label_date": day})
    return _train_samples(samples, latest, config, old_info, INTRADAY_NAMES, task="today_close")


def _model_record(outcome, old, now):
    return {"champion": outcome["champion"], "model_version": outcome["model_version"],
            "sample_count": outcome["sample_count"], "training_cutoff": outcome["training_cutoff"],
            "validation": outcome["validation"], "failures": outcome["failures"],
            "feature_metadata": outcome["feature_metadata"], "input_hash": outcome["input_hash"],
            "cache_hit": outcome["cache_hit"], "cached_outcome": deepcopy(outcome),
            "last_trained_at": now.isoformat() if outcome["trained"] else old.get("last_trained_at")}


def _apply_feature_metadata(row, outcome):
    metadata = outcome.get("feature_metadata", {})
    row["feature_metadata"] = metadata
    row["cache_hit"] = outcome.get("cache_hit", False)
    used = bool(SENTIMENT_FEATURES.intersection(metadata.get("used_names", [])))
    if used:
        row.update(news_features="active", news_note="只使用当时已采集的历史情绪快照；不代表新闻因果预测")
    else:
        row["news_note"] = "情绪快照用于展示与积累；当前选定价格模型尚未使用情绪特征"


def _news_snapshot(dashboard, now):
    # first_seen, not article publication time, governs what was actually knowable.
    result = []
    for item in dashboard.get("news", []):
        seen = _timestamp(item.get("first_seen_at"))
        if seen is not None and seen <= now:
            result.append({"id": item.get("id"), "first_seen_at": seen.isoformat(),
                           "content_hash": _hash({k: item.get(k) for k in ("title", "summary", "source", "verification")})})
    return sorted(result, key=lambda r: str(r["id"]))


def _settle(ledger, entities, now, config):
    for record in ledger:
        if record.get("status") == "settled" or record.get("actual_price") is not None:
            continue
        entity = entities.get(record.get("sector_id"))
        if entity is None or entity.get("data_quality") == "conflict" or not record.get("target_date"):
            continue
        completed = record.get("target_kind") == "today_close"
        bars = _history(entity, now, config, completed_only=completed)
        actual = next((b.get("close" if completed else "open") for b in bars if b["date"] == record["target_date"]), None)
        actual_source = "daily_bar"
        if actual is None and not completed:
            quote = entity.get("live_quote") or {}
            quote_time = _timestamp(quote.get("quote_timestamp"))
            if (quote.get("timestamp_quality") == "source_reported" and quote_time is not None
                    and _quote_available(quote, now)
                    and quote_time <= now and quote_time.date().isoformat() == record["target_date"]
                    and quote_time.time().replace(tzinfo=None) >= time(9, 30)
                    and is_trading_day(quote_time.date()) is True):
                actual = _number(quote.get("open"))
                actual_source = "source_reported_live_open"
        reference, predicted = _number(record.get("reference_price")), _number(record.get("predicted_return_pct"))
        issued = _timestamp(record.get("issued_at"))
        if issued is None or issued >= now or actual is None or actual <= 0 or not reference or reference <= 0 or predicted is None:
            continue
        try:
            target_day = date.fromisoformat(record["target_date"])
        except (ValueError, TypeError):
            continue
        deadline = datetime.combine(target_day, time(15) if completed else time(9, 25), SHANGHAI)
        if is_trading_day(target_day) is not True or issued >= deadline:
            continue
        actual_return = 100 * (actual / reference - 1)
        baseline = _number(record.get("baseline_return_pct")) or 0.0
        sign = lambda value: 0 if abs(value) < 1e-10 else (1 if value > 0 else -1)
        record.update(status="settled", actual_price=actual, actual_return_pct=actual_return,
                      error_pct=predicted - actual_return, baseline_error_pct=baseline - actual_return,
                      direction_correct=sign(predicted) == sign(actual_return), settled_at=now.isoformat(),
                      actual_source=actual_source)


def _metrics(ledger):
    # One observation per entity / target / task. Repeated refreshes do not inflate n.
    unique = {}
    for row in ledger:
        if row.get("status") == "settled" and _number(row.get("error_pct")) is not None:
            key = (row.get("sector_id"), row.get("target_date"), row.get("target_kind"))
            if key not in unique or (row.get("issued_at") or "") > (unique[key].get("issued_at") or ""):
                unique[key] = row
    rows = list(unique.values())
    mean = lambda values: sum(values) / len(values) if values else None
    return {"settled_count": len(rows), "mae_pct": mean([abs(r["error_pct"]) for r in rows]),
            "direction_accuracy": mean([float(r["direction_correct"]) for r in rows]),
            "baseline_mae_pct": mean([abs(r["baseline_error_pct"]) for r in rows]),
            "evaluation_policy": "latest forecast per target/date/task; tasks and issue times are not interchangeable",
            "independent_target_dates": len(set(r["target_date"] for r in rows)),
            "by_task": {kind: {"count": len([r for r in rows if r["target_kind"] == kind]),
                                "mae_pct": mean([abs(r["error_pct"]) for r in rows if r["target_kind"] == kind])}
                        for kind in ("next_open", "today_close")}}


def update_predictions(dashboard, previous=None, config=None, now=None):
    """Return predictions/ledger/metrics/models without mutating either input.

    previous is the last full dashboard (or this function's previous return).
    All timestamps use ISO 8601 with an explicit Shanghai offset. issue_slot is
    configurable. Equal snapshots in a slot retain the original ID.
    """
    now = shanghai_now(now)
    settings = {**DEFAULTS, **(config or {})}
    settings["min_train"] = max(10, int(settings["min_train"]))
    settings["holdout_size"] = max(5, int(settings["holdout_size"]))
    settings["intraday_min_train"] = max(20, int(settings["intraday_min_train"]))
    settings["hgb_min_train"] = max(60, int(settings["hgb_min_train"]))
    settings["max_train"] = max(20, int(settings["max_train"]))
    settings["close_confirm_minutes"] = min(59, max(0, int(settings["close_confirm_minutes"])))
    previous = previous or {}
    ledger = deepcopy(previous.get("ledger", []))
    # Collapse exact duplicate IDs without overwriting any issued forecast.
    ledger = list({row["id"]: row for row in reversed(ledger) if row.get("id")}.values())
    ledger.sort(key=lambda row: row.get("issued_at") or "")
    primary = [e for e in dashboard.get("market", []) + dashboard.get("sectors", []) if e.get("id")]
    entities = {e["id"]: e for e in primary}
    forecasting = {}
    for entity in primary:
        proxy = entity.get("proxy")
        chosen = entity
        if proxy and proxy.get("code"):
            identity = entity["id"] + ":proxy:" + proxy["code"]
            instrument = {**proxy, "id": identity, "name": entity.get("name", entity["id"]) + "（参考ETF：" + proxy.get("name", proxy["code"]) + "）", "basis": "etf",
                          "sentiment_history": entity.get("sentiment_history", []), "sentiment": entity.get("sentiment")}
            entities[identity] = instrument
            main_date = max((r.get("date", "") for r in entity.get("history", [])), default="")
            proxy_date = max((r.get("date", "") for r in proxy.get("history", [])), default="")
            if proxy_date and proxy_date > main_date:
                chosen = instrument
        forecasting[chosen["id"]] = chosen
    _settle(ledger, entities, now, settings)
    session = market_session(now)
    news = _news_snapshot(dashboard, now)
    slot_minutes = max(1, min(60, int(settings.get('prediction_interval_minutes', 15))))
    issue_slot = now.replace(minute=slot_minutes * (now.minute // slot_minutes), second=0, microsecond=0).isoformat()
    predictions, by_target = [], deepcopy(previous.get("models", {}).get("by_target", {}))
    trained = False
    for identity, entity in forecasting.items():
        target, kind = session["target_date"], session["target_kind"]
        row = {"id": None, "sector_id": identity, "sector_name": entity.get("name", identity),
               "issued_at": now.isoformat(), "issue_slot": issue_slot, "cutoff_at": now.isoformat(),
               "target_date": target, "target_kind": kind,
               "target_label": "今日收盘点位" if kind == "today_close" else "下次开盘点位",
               "status": "pending", "reason": "", "model_version": None, "reference_price": None,
               "predicted_price": None, "predicted_return_pct": None, "lower_price": None, "upper_price": None,
               "up_probability": None, "sample_count": 0, "validation": {}, "basis": entity.get("basis", "index"),
               "features_as_of": None, "training_cutoff": None, "feature_hash": None,
               "feature_metadata": {}, "cache_hit": False,
               "news_features": "collecting" if news else "disabled", "news_snapshot_hash": _hash(news),
               "news_note": "新闻尚无足够真实时点样本，未加入价格模型"}
        if row["basis"] in ("etf", "stock"):
            row["target_label"] = "今日收盘价格" if kind == "today_close" else "下次开盘价格"
        predictions.append(row)
        if entity.get("data_quality") == "conflict":
            row["reason"] = "可比行情来源存在价格冲突，暂停预测，等待核验"
            continue
        if target is None or is_trading_day(target) is not True:
            row["reason"] = "目标交易日历未核实，暂停预测"
            continue
        reference_day = previous_trading_day(target)
        if reference_day is None:
            row["reason"] = "目标前一交易日历未核实，无法确定参考收盘"
            continue
        bars = _with_source_flows(_history(entity, now, settings), entity, now)
        bars = [b for b in bars if b["date"] <= reference_day.isoformat()]
        row.update(required_reference_date=reference_day.isoformat(),
                   latest_available_close_date=bars[-1]["date"] if bars else None)
        reference = next((b for b in bars if b["date"] == reference_day.isoformat()), None)
        if reference is None:
            row["reason"] = f"缺少或尚未确认 {reference_day.isoformat()} 的参考收盘"
            continue
        row.update(reference_price=reference["close"], reference_date=reference_day.isoformat())
        if kind == "today_close":
            quote = entity.get('live_quote') or entity
            quote_time = _timestamp(quote.get('quote_timestamp') or quote.get('as_of'))
            price = _number(quote.get('price'))
            if quote.get("timestamp_quality") == "retrieved_only":
                row["reason"] = "只有抓取时间、无法核实行情交易时间；暂停盘中预测"
                continue
            if not _quote_available(quote, now):
                row["reason"] = "该报价在预测时点尚未采集到，暂停盘中预测"
                continue
            if quote_time is None or quote_time.date() != now.date() or quote_time > now or price is None or price <= 0:
                row["reason"] = "缺少当日可验证时间戳的行情；未接入分钟模型"
                continue
            elapsed = (now - quote_time).total_seconds() / 60
            # A quote at/near the morning close remains valid during lunch.
            lunch_ok = session["phase"] == "midday_break" and quote_time.time().replace(tzinfo=None) >= time(11, 25)
            if elapsed > settings["quote_max_age_minutes"] and not lunch_ok:
                row["reason"] = "盘中行情已过时，暂停基线预测"
                continue
            predicted = 100 * (price / reference["close"] - 1)
            row.update(status="ready", model_version="naive-now-v1", predicted_price=price,
                       predicted_return_pct=predicted, baseline_return_pct=predicted,
                       features_as_of=quote_time.isoformat(), reason="持平延续基线：假设收盘等于当前点位；无分钟模型、无校准概率",
                       expected_remaining_return_pct=0.0,
                       validation={"method": "persistence baseline", "note": "未训练的盘中基线，不代表模型有效性"})
            feature_values = {"quote": price, "quote_time": quote_time.isoformat(), "reference": reference["close"]}
            # Intraday learning requires real historical snapshots at the same clock time.
            bucket = quote_time.replace(minute=slot_minutes * (quote_time.minute // slot_minutes))
            model_key = identity + ":intraday:" + bucket.strftime("%H:%M")
            try:
                outcome, error = _intraday_model(entity, dashboard.get("intraday", {}).get(identity, {}), bars,
                                                 quote, quote_time, now, settings, by_target.get(model_key, {}))
            except ImportError:
                outcome, error = None, {"reason": "缺少训练依赖，保留盘中持平基线", "sample_count": 0}
            if outcome:
                remaining = outcome["predicted_return_pct"]
                point = price * (1 + remaining / 100)
                if math.isfinite(point) and point > 0:
                    row.update(model_version=outcome["model_version"], predicted_price=point,
                               predicted_return_pct=100 * (point / reference["close"] - 1),
                               expected_remaining_return_pct=remaining,
                               lower_price=max(0., price * (1 + outcome["lower_return_pct"] / 100)),
                               upper_price=max(0., price * (1 + outcome["upper_return_pct"] / 100)),
                               sample_count=outcome["sample_count"], validation=outcome["validation"],
                               training_cutoff=outcome["training_cutoff"],
                               features_as_of=now.isoformat(), price_as_of=quote_time.isoformat(),
                               reason="同时间真实快照预测剩余收益；按日期验证并与持平延续基线比较；区间为经验范围")
                    _apply_feature_metadata(row, outcome)
                    feature_values = outcome["feature_values"]
                    trained = trained or outcome["trained"]
                    by_target[model_key] = _model_record(outcome, by_target.get(model_key, {}), now)
            else:
                row.update(sample_count=error.get("sample_count", 0), feature_metadata=error.get("feature_metadata", {}),
                           intraday_training_note=error["reason"])
        else:
            try:
                outcome, error = _daily_model(bars, settings, by_target.get(identity, {}), entity, now)
            except ImportError:
                outcome, error = None, {"reason": "本地缺少 numpy 或 scikit-learn，无法训练", "sample_count": 0}
            if error:
                row.update(error)
                continue
            trained = trained or outcome["trained"]
            predicted = outcome["predicted_return_pct"]
            point = reference["close"] * (1 + predicted / 100)
            if not math.isfinite(point) or point <= 0:
                row["reason"] = "模型产生无效价格，已拒绝输出"
                continue
            row.update(status="ready", model_version=outcome["model_version"], predicted_price=point,
                       predicted_return_pct=predicted, baseline_return_pct=0.0,
                       lower_price=max(0.0, reference["close"] * (1 + outcome["lower_return_pct"] / 100)),
                       upper_price=reference["close"] * (1 + outcome["upper_return_pct"] / 100),
                       sample_count=outcome["sample_count"], validation=outcome["validation"],
                       features_as_of=now.isoformat(),
                       training_cutoff=outcome["training_cutoff"],
                       reason="按已完成日K与当时可用特征滚动验证；输入未变复用结果；区间为经验范围，不保证覆盖率")
            _apply_feature_metadata(row, outcome)
            feature_values = outcome["feature_values"]
            by_target[identity] = _model_record(outcome, by_target.get(identity, {}), now)
        row["feature_hash"] = _hash({"version": FEATURE_VERSION, "values": feature_values, "news_snapshot": news})
        row["id"] = "forecast-" + _hash({"entity": identity, "target": target, "kind": kind,
                                              "slot": issue_slot, "model": row["model_version"],
                                              "features": row["feature_hash"], "reference": reference["close"]})
        existing = next((r for r in ledger if r["id"] == row["id"]), None)
        if existing:
            current_metadata, cache_hit = row.get("feature_metadata", {}), row.get("cache_hit", False)
            row.clear()
            row.update(deepcopy(existing))
            row.update(feature_metadata=current_metadata, cache_hit=cache_hit)
        else:
            recorded = deepcopy(row)
            # Keep the used-feature contract in each immutable issued record;
            # full coverage tables live in current predictions/models, not repeated
            # in every 30-minute ledger row indefinitely.
            recorded["feature_metadata"] = {k: v for k, v in recorded.get("feature_metadata", {}).items()
                                            if k not in ("coverage", "names", "missing_policy", "point_in_time_policy")}
            ledger.append(recorded)
    models = {"champion": "per-target chronological baseline/ridge/hgb", "challenger": "mlp-32-16" if settings["enable_mlp"] else "disabled",
              "status": "active" if any(r["status"] == "ready" for r in predictions) else "waiting",
              "news_features": "active" if any(r.get("news_features") == "active" for r in predictions) else ("collecting" if news else "disabled"), "by_target": by_target,
              "last_trained_at": now.isoformat() if trained else previous.get("models", {}).get("last_trained_at"),
              "feature_version": FEATURE_VERSION, "candidate_features": list(FEATURE_NAMES),
              "training_policy": "rolling completed bars; PIT auxiliary features; chronological selection then confirmation; unchanged-input cache; no random split",
              "interval_policy": "empirical residual quantiles; future coverage not guaranteed"}
    if not training_enabled(settings):
        models.update(inference_only=True, training_mode="inference_only",
                      training_policy="云端只推断与结算；训练和调优在本机完成后上传。旧模型仅复用完全相同输入的已保存结果。")
    return {"predictions": predictions, "ledger": ledger, "metrics": _metrics(ledger), "models": models}
