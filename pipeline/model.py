"""Auditable forecasts, conservative daily-bar availability and delayed labels.

No article sentiment is converted into an invented probability. The intraday
forecast is explicitly a persistence baseline, not a trained intraday model.
Daily models use two chronological holdouts; a challenger must improve on the
incumbent and zero-return baseline on a later, untouched confirmation window.
"""
from copy import deepcopy
from datetime import date, datetime, time
import hashlib
import json
import math
import warnings

from .calendar import SHANGHAI, market_session, next_trading_day, previous_trading_day, shanghai_now

FEATURE_VERSION = "daily-ohlc-v1"
DEFAULTS = {"enable_mlp": False, "min_train": 60, "holdout_size": 20,
            "max_train": 504, "improvement_margin": 0.02,
            "quote_max_age_minutes": 20, "close_confirm_minutes": 15}


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _timestamp(value):
    if not value:
        return None
    try:
        return shanghai_now(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (ValueError, TypeError):
        return None


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
        available = _timestamp(raw.get("available_at") or raw.get("as_of"))
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


def _features(rows, index):
    if index < 20:
        return None
    window = rows[index - 20:index + 1]
    closes = [r["close"] for r in window]
    if any(v is None or v <= 0 for v in closes):
        return None
    returns = [100 * (b / a - 1) for a, b in zip(closes, closes[1:])]
    mean = sum(returns) / len(returns)
    opening = _number(window[-1].get("open"))
    high, low = _number(window[-1].get("high")), _number(window[-1].get("low"))
    return [returns[-1], sum(returns[-5:]) / 5, mean,
            (sum((r - mean) ** 2 for r in returns) / len(returns)) ** .5,
            100 * (closes[-1] / opening - 1) if opening and opening > 0 else 0,
            100 * (high - low) / closes[-1] if high and low and high >= low else 0]


def _samples(rows):
    samples = []
    for i in range(20, len(rows) - 1):
        expected = next_trading_day(rows[i]["date"])
        nxt = rows[i + 1]
        if expected is None or expected.isoformat() != nxt["date"]:
            continue
        x, opening = _features(rows, i), _number(nxt.get("open"))
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
    else:
        from sklearn.neural_network import MLPRegressor
        estimator = MLPRegressor(hidden_layer_sizes=(32, 16), activation="relu", alpha=1.0,
                                 max_iter=300, early_stopping=False, shuffle=False,
                                 random_state=17, learning_rate_init=.001)
    model = make_pipeline(StandardScaler(), estimator)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model.fit(np.asarray(x, dtype=float), np.asarray(y, dtype=float))
    return model


def _daily_model(rows, config, old_info):
    import numpy as np
    samples = _samples(rows)[-config["max_train"]:]
    count, holdout = len(samples), config["holdout_size"]
    required = config["min_train"] + 2 * holdout
    if count < required:
        return None, {"reason": f"隔夜模型需至少 {required} 个已完成交易样本，当前 {count} 个", "sample_count": count}
    x, y = np.asarray([r["x"] for r in samples]), np.asarray([r["y"] for r in samples])
    a, b = count - 2 * holdout, count - holdout
    predictions = {"zero_return": (np.zeros(holdout), np.zeros(holdout))}
    failures = []
    for name in (["ridge", "mlp"] if config["enable_mlp"] else ["ridge"]):
        try:
            selected = _fit(name, x[:a], y[:a]).predict(x[a:b])
            confirmed = _fit(name, x[:b], y[:b]).predict(x[b:])
            if not (np.isfinite(selected).all() and np.isfinite(confirmed).all()):
                raise ValueError("non-finite prediction")
            predictions[name] = (selected, confirmed)
        except (ImportError, ValueError, RuntimeError) as exc:
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
    latest_x = _features(rows, len(rows) - 1)
    if latest_x is None:
        return None, {"reason": "最新特征不足", "sample_count": count}
    predicted = 0.0
    if champion != "zero_return":
        predicted = float(_fit(champion, x, y).predict([latest_x])[0])
    residuals = y[b:] - predictions[champion][1]
    q10, q90 = [float(v) for v in np.quantile(residuals, [.1, .9])]
    validation = {"method": "two-stage chronological walk-forward", "selection_count": holdout,
                  "confirmation_count": holdout, "selection_start": samples[a]["label_date"],
                  "confirmation_start": samples[b]["label_date"], "scores": scores,
                  "mae_pct": scores[champion]["confirmation_mae_pct"],
                  "baseline_mae_pct": baseline_score,
                  "note": "历史样本外比较；区间来自确认窗口残差分位数，不保证未来覆盖率"}
    version = f"{champion}-{FEATURE_VERSION}-{_hash(samples)}"
    return {"predicted_return_pct": predicted, "lower_return_pct": predicted + q10,
            "upper_return_pct": predicted + q90, "model_version": version,
            "training_cutoff": samples[-1]["label_date"], "feature_values": latest_x,
            "validation": validation, "sample_count": count,
            "champion": champion, "trained": len(predictions) > 1,
            "failures": failures}, None


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
        reference, predicted = _number(record.get("reference_price")), _number(record.get("predicted_return_pct"))
        issued = _timestamp(record.get("issued_at"))
        if issued is None or issued >= now or actual is None or actual <= 0 or not reference or reference <= 0 or predicted is None:
            continue
        actual_return = 100 * (actual / reference - 1)
        baseline = _number(record.get("baseline_return_pct")) or 0.0
        sign = lambda value: 0 if abs(value) < 1e-10 else (1 if value > 0 else -1)
        record.update(status="settled", actual_price=actual, actual_return_pct=actual_return,
                      error_pct=predicted - actual_return, baseline_error_pct=baseline - actual_return,
                      direction_correct=sign(predicted) == sign(actual_return), settled_at=now.isoformat())


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
    a 15-minute Shanghai slot. Equal snapshots in a slot retain the original ID.
    """
    now = shanghai_now(now)
    settings = {**DEFAULTS, **(config or {})}
    settings["min_train"] = max(10, int(settings["min_train"]))
    settings["holdout_size"] = max(5, int(settings["holdout_size"]))
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
            instrument = {**proxy, "id": identity, "name": entity.get("name", entity["id"]) + "（参考ETF：" + proxy.get("name", proxy["code"]) + "）", "basis": "etf"}
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
               "news_features": "collecting" if news else "disabled", "news_snapshot_hash": _hash(news),
               "news_note": "新闻尚无足够真实时点样本，未加入价格模型"}
        if row["basis"] in ("etf", "stock"):
            row["target_label"] = "今日收盘价格" if kind == "today_close" else "下次开盘价格"
        predictions.append(row)
        if entity.get("data_quality") == "conflict":
            row["reason"] = "可比行情来源存在价格冲突，暂停预测，等待核验"
            continue
        if target is None:
            row["reason"] = "目标交易日历未核实，暂停预测"
            continue
        reference_day = previous_trading_day(target)
        if reference_day is None:
            row["reason"] = "目标前一交易日历未核实，无法确定参考收盘"
            continue
        bars = _history(entity, now, settings)
        bars = [b for b in bars if b["date"] <= reference_day.isoformat()]
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
        else:
            try:
                outcome, error = _daily_model(bars, settings, by_target.get(identity, {}))
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
                       features_as_of=reference["date"] + "T15:15:00+08:00",
                       training_cutoff=outcome["training_cutoff"],
                       reason="按已完成日K滚动重训；新闻尚未参与；区间为经验残差范围，不保证覆盖率")
            feature_values = outcome["feature_values"]
            by_target[identity] = {"champion": outcome["champion"], "model_version": outcome["model_version"],
                                   "sample_count": outcome["sample_count"], "training_cutoff": outcome["training_cutoff"],
                                   "validation": outcome["validation"], "failures": outcome["failures"],
                                   "last_trained_at": now.isoformat() if outcome["trained"] else by_target.get(identity, {}).get("last_trained_at")}
        row["feature_hash"] = _hash({"version": FEATURE_VERSION, "values": feature_values, "news_snapshot": news})
        row["id"] = "forecast-" + _hash({"entity": identity, "target": target, "kind": kind,
                                              "slot": issue_slot, "model": row["model_version"],
                                              "features": row["feature_hash"], "reference": reference["close"]})
        existing = next((r for r in ledger if r["id"] == row["id"]), None)
        if existing:
            row.clear()
            row.update(deepcopy(existing))
        else:
            ledger.append(deepcopy(row))
    models = {"champion": "per-target validated baseline/ridge", "challenger": "mlp-32-16" if settings["enable_mlp"] else "disabled",
              "status": "active" if any(r["status"] == "ready" for r in predictions) else "waiting",
              "news_features": "collecting" if news else "disabled", "by_target": by_target,
              "last_trained_at": now.isoformat() if trained else previous.get("models", {}).get("last_trained_at"),
              "training_policy": "rolling completed bars; chronological selection then confirmation; no random split",
              "interval_policy": "empirical residual quantiles; future coverage not guaranteed"}
    return {"predictions": predictions, "ledger": ledger, "metrics": _metrics(ledger), "models": models}
