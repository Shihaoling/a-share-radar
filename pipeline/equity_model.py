"""Shared stock forecasts, honest coverage and immutable prediction ledgers.

Only historical OHLCV enters training. Current news, valuation and membership
are not backfilled into history. Percent returns use the *label day's* vendor
pre_close, so ex-rights events are not mistaken for overnight alpha. These
returns do not imply an absolute price forecast before that basis is known.
"""
from copy import deepcopy
from datetime import date, datetime, time, timedelta
import gzip
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from .calendar import market_session, next_trading_day, previous_trading_day, shanghai_now

VERSION = "shared-equity-ohlcv-v1"
SEQUENCE_FEATURE_NAMES = ["return_pct", "open_return_pct", "range_pct", "close_open_pct",
                          "volume_relative", "amount_relative", "volume_change", "amount_change"]
FEATURE_NAMES = SEQUENCE_FEATURE_NAMES + ["mean_return_5", "mean_return_20", "volatility_5",
    "volatility_20", "compound_return_5", "compound_return_20", "mean_range_20", "mean_close_open_5"]
DEFAULTS = {"min_train_dates": 90, "min_validation_dates": 20, "min_test_dates": 20,
            "gap": 1, "min_history": 20, "improvement_margin": .02,
            "retrain_days": 7, "minimum_weight_coverage": .90, "max_train_stocks": None,
            "close_confirm_minutes": 15, "enable_training": True}


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _day(value):
    value = str(value or "")
    if len(value) == 8 and value.isdigit():
        value = f"{value[:4]}-{value[4:6]}-{value[6:8]}"
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def _code(value):
    value = str(value or "").upper()
    if len(value) == 6 and value.isdigit():
        return value + (".SH" if value.startswith("6") else ".BJ" if value.startswith(("4", "8", "9")) else ".SZ")
    return value


def _read(path):
    try:
        opener = gzip.open if str(path).endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, TypeError):
        return None


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
    if str(path).endswith(".gz"):
        with gzip.open(temporary, "wt", encoding="utf-8") as handle:
            handle.write(payload)
    else:
        temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


def load_history(history_dir):
    """Read complete daily batches; bad/future completeness is handled upstream.

    Both ``data/equity`` and its ``daily`` directory are accepted. A partial
    batch never silently replaces a complete daily observation.
    """
    directory = Path(history_dir)
    if (directory / "daily").is_dir():
        directory = directory / "daily"
    collected = {}
    for path in sorted(directory.glob("*.json*")):
        batch = _read(path)
        if not isinstance(batch, dict) or batch.get("complete") is not True:
            continue
        for raw in batch.get("rows", []):
            if not isinstance(raw, dict):
                continue
            code = _code(raw.get("ts_code") or raw.get("code"))
            day = _day(raw.get("trade_date") or raw.get("date") or batch.get("trade_date"))
            close = _number(raw.get("close"))
            if not code or day is None or close is None or close <= 0:
                continue
            row = {**raw, "ts_code": code, "date": day, "trade_date": day}
            for name in ("open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount", "adj_factor"):
                row[name] = _number(row.get(name))
            collected.setdefault(code, {})[day] = row
    return {code: [rows[day] for day in sorted(rows)] for code, rows in collected.items()}


def _daily_features(row, prior):
    close, opening, high, low, base = (_number(row.get(key)) for key in ("close", "open", "high", "low", "pre_close"))
    if any(value is None or value <= 0 for value in (close, opening, high, low, base)) or high < low:
        return None
    volume, amount = _number(row.get("vol")), _number(row.get("amount"))
    previous_volume = _number(prior[-1].get("vol")) if prior else None
    previous_amount = _number(prior[-1].get("amount")) if prior else None
    volumes = [v for r in prior[-20:] if (v := _number(r.get("vol"))) is not None and v > 0]
    amounts = [v for r in prior[-20:] if (v := _number(r.get("amount"))) is not None and v > 0]
    def ratio(value, reference):
        return float(np.clip(math.log(value / reference), -10, 10)) if value and value > 0 and reference and reference > 0 else 0.
    return [100 * (close / base - 1), 100 * (opening / base - 1), 100 * (high - low) / base,
            100 * (close / opening - 1), ratio(volume, np.mean(volumes) if volumes else None),
            ratio(amount, np.mean(amounts) if amounts else None), ratio(volume, previous_volume), ratio(amount, previous_amount)]


def sequence_features(bars, end_index, window=60):
    """Stationary features known by end_index; shape(window, 8), no future fits."""
    if end_index + 1 < window or end_index >= len(bars):
        return None
    rows = []
    for index in range(end_index - window + 1, end_index + 1):
        values = _daily_features(bars[index], bars[max(0, index - 20):index])
        if values is None:
            return None
        rows.append(values)
    array = np.asarray(rows, dtype=np.float32)
    return array if np.isfinite(array).all() else None


def _tabular_features(bars, index, window=20):
    sequence = sequence_features(bars, index, window)
    if sequence is None:
        return None
    return _tabular_from_sequence(sequence)


def _tabular_from_sequence(sequence):
    returns = sequence[:, 0].astype(float)
    values = list(sequence[-1]) + [float(np.mean(returns[-5:])), float(np.mean(returns)),
        float(np.std(returns[-5:])), float(np.std(returns)),
        100 * (float(np.prod(1 + returns[-5:] / 100)) - 1),
        100 * (float(np.prod(1 + returns / 100)) - 1),
        float(np.mean(sequence[:, 2])), float(np.mean(sequence[-5:, 3]))]
    return values if np.isfinite(values).all() else None


def build_samples(history, min_history=20, max_stocks=None):
    """Rows are stock/date observations, but splitting is by global date.

    A halted stock's next observed bar is not a next-day label. All-stock
    observed dates establish adjacency even for calendar years not bundled
    with the app; inference still requires the verified live calendar.
    """
    all_dates = sorted({row["date"] for bars in history.values() for row in bars})
    following = dict(zip(all_dates, all_dates[1:]))
    codes = sorted(history)
    if max_stocks:
        codes = codes[:int(max_stocks)]
    xs, opens, closes, dates, identities = [], [], [], [], []
    for code in codes:
        bars = history[code]
        daily = [_daily_features(row, bars[max(0, index - 20):index]) for index, row in enumerate(bars)]
        for index in range(min_history - 1, len(bars) - 1):
            current, nxt = bars[index], bars[index + 1]
            if following.get(current["date"]) != nxt["date"]:
                continue
            expected = next_trading_day(current["date"])
            if expected is not None and expected.isoformat() != nxt["date"]:
                continue
            base, opening, close = (_number(nxt.get(key)) for key in ("pre_close", "open", "close"))
            if any(value is None or value <= 0 for value in (base, opening, close)):
                continue
            sequence = daily[index - min_history + 1:index + 1]
            if any(row is None for row in sequence):
                continue
            features = _tabular_from_sequence(np.asarray(sequence, dtype=np.float32))
            if features is None:
                continue
            xs.append(features)
            opens.append(100 * (opening / base - 1))
            closes.append(100 * (close / base - 1))
            dates.append(current["date"])
            identities.append(code)
    return {"X": np.asarray(xs, dtype=np.float32).reshape((-1, len(FEATURE_NAMES))),
            "y_open": np.asarray(opens), "y_close": np.asarray(closes), "dates": dates,
            "codes": identities, "feature_names": FEATURE_NAMES}


def chronological_split(dates, min_train_dates=90, min_validation_dates=20, gap=1, min_test_dates=20):
    """Whole-date split with a one-label-horizon gap at both boundaries."""
    unique = sorted(set(dates))
    needed = min_train_dates + min_validation_dates + min_test_dates + 2 * gap
    if len(unique) < needed:
        return None
    test_start = len(unique) - min_test_dates
    validation_end = test_start - gap
    validation_start = validation_end - min_validation_dates
    train_end = validation_start - gap
    groups = {"train": unique[:train_end], "validation": unique[validation_start:validation_end],
              "test": unique[test_start:]}
    result = {name + "_mask": np.isin(dates, group) for name, group in groups.items()}
    result["date_ranges"] = {name: {"start": group[0], "end": group[-1], "dates": len(group)} for name, group in groups.items()}
    result["gap_dates"] = unique[train_end:validation_start] + unique[validation_end:test_start]
    return result


def _metrics(actual, predicted):
    if len(actual) == 0:
        return {"samples": 0, "mae_pct": None, "rmse_pct": None, "direction_accuracy": None}
    actual, predicted = np.asarray(actual), np.asarray(predicted)
    nonzero = (actual != 0) & (predicted != 0)
    return {"samples": len(actual), "mae_pct": float(np.mean(np.abs(actual - predicted))),
            "rmse_pct": float(np.sqrt(np.mean((actual - predicted) ** 2))),
            "direction_accuracy": float(np.mean(np.sign(actual[nonzero]) == np.sign(predicted[nonzero]))) if nonzero.any() else None,
            "direction_evaluated": int(nonzero.sum())}


def _portable_fit(x, y):
    from sklearn.linear_model import Ridge
    mean, scale = np.mean(x, axis=0), np.std(x, axis=0)
    scale = np.where(scale > 1e-8, scale, 1.)
    estimator = Ridge(alpha=100.)
    estimator.fit((x - mean) / scale, y)
    return {"mean": mean.tolist(), "scale": scale.tolist(), "coef": np.asarray(estimator.coef_).tolist(),
            "intercept": float(estimator.intercept_)}


def predict_portable(bundle, x):
    values = np.asarray(x, dtype=float)
    return ((values - np.asarray(bundle["mean"])) / np.asarray(bundle["scale"])) @ np.asarray(bundle["coef"]) + bundle["intercept"]


def train_shared_baseline(history, model_dir, config=None, now=None):
    """Train a portable Ridge candidate and retain zero unless both checks pass."""
    supplied = (config or {}).get('equity', config or {})
    settings = {**DEFAULTS, **supplied}
    now = shanghai_now(now)
    samples = build_samples(history, settings["min_history"], settings["max_train_stocks"])
    split = chronological_split(samples["dates"], settings["min_train_dates"], settings["min_validation_dates"],
                                settings["gap"], settings["min_test_dates"])
    report = {"version": VERSION, "status": "insufficient_history", "promoted": False, "model": "zero_return",
              "trained_at": now.isoformat(), "train_samples": 0, "validation_samples": 0, "test_samples": 0,
              "total_samples": len(samples["dates"]), "stock_count": len(set(samples["codes"])),
              "feature_dates": len(set(samples["dates"])), "feature_names": FEATURE_NAMES,
              "metrics": {}, "candidate_metrics": {}, "date_ranges": {},
              "note": "共享股票模型；仅历史量价特征。未超过零收益基线时不晋升。当前股票池筛选可能产生幸存偏差。"}
    artifact = {"version": VERSION, "trained_at": now.isoformat(),
                "data_cutoff": max(samples["dates"], default=None), "feature_names": FEATURE_NAMES, "targets": {}, "report": report}
    artifact["label_cutoff"] = max((row["date"] for bars in history.values() for row in bars), default=None)
    if split is None:
        report["required_feature_dates"] = settings["min_train_dates"] + settings["min_validation_dates"] + settings["min_test_dates"] + 2 * settings["gap"]
        return artifact
    report.update(status="evaluated", date_ranges=split["date_ranges"], gap_dates=split["gap_dates"])
    x = samples["X"]
    for part in ("train", "validation", "test"):
        report[part + "_samples"] = int(split[part + "_mask"].sum())
    for target, y in (("next_open", samples["y_open"]), ("next_close", samples["y_close"])):
        candidate = _portable_fit(x[split["train_mask"]], y[split["train_mask"]])
        checks = {}
        for part in ("validation", "test"):
            mask = split[part + "_mask"]
            checks[part] = {"ridge": _metrics(y[mask], predict_portable(candidate, x[mask])),
                            "zero_return": _metrics(y[mask], np.zeros(mask.sum()))}
        promoted = all(checks[part]["ridge"]["mae_pct"] < checks[part]["zero_return"]["mae_pct"] * (1 - settings["improvement_margin"]) for part in checks)
        selected = "ridge" if promoted else "zero_return"
        artifact["targets"][target] = {"model": selected, "promoted": promoted,
                                        "parameters": _portable_fit(x, y) if promoted else None}
        report["candidate_metrics"][target] = checks
        report["metrics"][target] = {part: checks[part][selected] for part in checks}
    report["promoted"] = any(value["promoted"] for value in artifact["targets"].values())
    report["model"] = "shared_ridge_or_zero"
    _write(Path(model_dir) / "shared_baseline.json", artifact)
    _write(Path(model_dir) / "baseline_report.json", report)
    return artifact


def _universe(dashboard, history):
    """Current constituent universe for publishing, never historical labels."""
    stocks, sector_members = {}, {}
    for sector in dashboard.get("sectors", []):
        sector_id = sector.get("id")
        members = sector.get("constituents", []) or sector.get("stocks", [])
        sector_members[sector_id] = []
        for member in members:
            if not isinstance(member, dict):
                continue
            code = _code(member.get("ts_code") or member.get("code"))
            if not code:
                continue
            stocks.setdefault(code, {"code": code, "name": member.get("name", code), "sector_id": sector_id,
                                     "daily_basic": member.get("stock_api_daily_basic", {})})
            sector_members[sector_id].append(code)
    # Global daily_basic includes listed stocks outside cached sector membership.
    basic = dashboard.get("stock_api_state", {}).get("daily_basic", {})
    records = basic.get("records", {})
    if isinstance(records, list):
        records = {row.get("ts_code"): row for row in records if isinstance(row, dict)}
    for identity, row in records.items():
        if not isinstance(row, dict):
            continue
        code = _code(row.get("ts_code") or identity)
        if code:
            stocks.setdefault(code, {"code": code, "name": row.get("name", code), "sector_id": None})
            stocks[code]["daily_basic"] = {"trade_date": basic.get("trade_date"), **row}
    if basic.get('full_market_request') and basic.get('quality', {}).get('complete') and records:
        # A cached industry membership may still contain former members or old
        # exchange codes. Today's validated A-share list owns the live universe.
        eligible = {_code(row.get('ts_code') or identity) for identity, row in records.items() if isinstance(row, dict)}
        stocks = {code: row for code, row in stocks.items() if code in eligible}
        sector_members = {key: [code for code in codes if code in eligible] for key, codes in sector_members.items()}
    # Do not resurrect delisted historical names in today's publishing universe.
    if not stocks:
        for code in history:
            stocks[code] = {"code": code, "name": code, "sector_id": None, "daily_basic": {}}
    return stocks, sector_members


def aggregate_predictions(predictions, identities, weights, context, minimum_coverage=.9):
    codes = sorted(set(identities))
    valid = [code for code in codes if code in predictions and predictions[code].get("predicted_return_pct") is not None
             and all(predictions[code].get(key) == context.get(key) for key in ("target_date", "target_kind", "reference_date"))]
    known = {code: weights[code] for code in codes if _number(weights.get(code)) is not None and weights[code] > 0}
    denominator = sum(known.values())
    covered = sum(known.get(code, 0) for code in valid)
    coverage = covered / denominator if denominator > 0 else None
    count_coverage = len(valid) / len(codes) if codes else 0.
    complete_weights = len(known) == len(codes) and bool(codes)
    value = sum(known[code] * predictions[code]["predicted_return_pct"] for code in valid if code in known) / covered if covered > 0 else None
    status = "pending" if value is None else "ready" if complete_weights and coverage >= minimum_coverage else "partial"
    if status == "ready" and all(predictions[code]["model"] == "zero_return" for code in valid):
        status = "baseline"
    return {**context, "status": status, "predicted_return_pct": value,
            "universe_count": len(codes), "predicted_count": len(valid), "known_weight_count": len(known),
            "weight_coverage": coverage, "count_coverage": count_coverage, "weights_complete": complete_weights,
            "weight_method": "available_circ_mv", "official_index_forecast": False,
            "note": "当前成分股研究组合；按已知流通市值加权，不等同官方指数。权重覆盖率以有权重股票为分母；缺失权重时仅展示已覆盖部分。历史回测不使用当前成员权重。"}


def _ledger(directory, forecast, history, now):
    """One immutable pre-target snapshot per day/task; settle labels separately."""
    directory = Path(directory)
    target = forecast.get("target_date")
    available = [stock for stock in forecast.get("stocks", []) if stock.get("predicted_return_pct") is not None]
    if target and available:
        ledger_id = f"{target.replace('-', '')}-{forecast['target_kind']}"
        path = directory / (ledger_id + ".json.gz")
        if not path.exists():
            snapshot = {"schema_version": 1, "id": ledger_id, "issued_at": forecast["as_of"],
                        "target_date": target, "target_kind": forecast["target_kind"], "model": forecast["model"],
                        "predictions": available, "settlements": {}}
            snapshot["prediction_hash"] = hashlib.sha256(json.dumps(available, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
            _write(path, snapshot)
    total, absolute_errors, signs, daily = 0, [], [], []
    indexed = {code: {row["date"]: row for row in bars} for code, bars in history.items()}
    for path in sorted(directory.glob("*.json.gz")):
        snapshot = _read(path)
        if not isinstance(snapshot, dict):
            continue
        day = _day(snapshot.get("target_date"))
        if day is None or day > now.date().isoformat():
            continue
        kind = snapshot.get("target_kind")
        if day == now.date().isoformat() and now.time().replace(tzinfo=None) < (time(15, 15) if kind in ("today_close", "next_close") else time(9, 30)):
            continue
        settlement = snapshot.setdefault("settlements", {})
        changed = False
        for prediction in snapshot.get("predictions", []):
            code = prediction.get("code")
            if code in settlement:
                continue
            row = indexed.get(code, {}).get(day, {})
            price = _number(row.get("open" if kind == "next_open" else "close"))
            base = _number(row.get("pre_close"))
            predicted = _number(prediction.get("predicted_return_pct"))
            if price is None or base is None or predicted is None or min(price, base) <= 0:
                continue
            actual = 100 * (price / base - 1)
            settlement[code] = {"actual_return_pct": actual, "absolute_error_pct": abs(actual - predicted),
                                "direction_correct": (actual * predicted > 0) if actual != 0 and predicted != 0 else None,
                                "settled_at": now.isoformat(), "actual_price": price, "reference_pre_close": base}
            changed = True
        if changed:
            _write(path, snapshot)
        rows = list(settlement.values())
        errors = [row["absolute_error_pct"] for row in rows]
        correct = [row["direction_correct"] for row in rows if row.get("direction_correct") is not None]
        total += len(rows)
        absolute_errors.extend(errors)
        signs.extend(correct)
        if rows:
            daily.append({"date": day, "target_kind": kind, "settled_count": len(rows),
                          "predicted_count": len(snapshot.get("predictions", [])),
                          "mae_pct": float(np.mean(errors)), "direction_accuracy": float(np.mean(correct)) if correct else None,
                          "direction_evaluated": len(correct)})
    return {"settled_count": total, "mae_pct": float(np.mean(absolute_errors)) if absolute_errors else None,
            "direction_accuracy": float(np.mean(signs)) if signs else None, "direction_evaluated": len(signs),
            "daily": daily[-60:], "note": "每日首次预测固定留档；零收益不作上涨/下跌判断，不计入方向准确率。"}


def update_equity_predictions(dashboard, history_dir, model_dir, now=None, config=None):
    """Publish individual forecasts before sector/market aggregates.

    Daily-only models deliberately abstain during an intraday target: a next
    close task trained at yesterday's close is not a live same-clock forecast.
    """
    result = deepcopy(dashboard)
    supplied = (config or {}).get('equity', config or {})
    settings = {**DEFAULTS, **supplied}
    now = shanghai_now(now)
    session = market_session(now)
    target_date, target_kind = session.get("target_date"), session.get("target_kind")
    reference = previous_trading_day(target_date) if target_date else None
    reference_day = reference.isoformat() if reference else None
    if settings.get("reuse_only"):
        cached = result.get("equity_forecast", {})
        if (cached.get("target_date"), cached.get("target_kind"), cached.get("reference_date")) == (target_date, target_kind, reference_day) and target_kind != "today_close":
            cached["reuse_only"] = True
            return result
        expired = {"status": "expired", "as_of": now.isoformat(), "target_date": target_date,
                   "target_kind": target_kind, "reference_date": reference_day, "predicted_return_pct": None,
                   "model": "unavailable", "universe_count": cached.get("coverage", {}).get("universe_count", 0),
                   "predicted_count": 0, "weight_coverage": None, "count_coverage": 0,
                   "note": "上次日线预测已过期；行情刷新不重训模型，等待下一次完整研究更新。"}
        result["equity_forecast"] = {**expired, "reuse_only": True, "stocks": [], "market": deepcopy(expired),
                "coverage": {"universe_count": expired["universe_count"], "predicted_count": 0, "weight_coverage": None, "count_coverage": 0},
                "training": cached.get("training", {}), "evaluation": cached.get("evaluation", {})}
        for sector in result.get("sectors", []):
            sector["equity_prediction"] = deepcopy(expired)
        return result
    loaded = load_history(history_dir)
    completed_day = now.date().isoformat()
    if now.time().replace(tzinfo=None) < time(15, settings["close_confirm_minutes"]):
        completed_day = (now.date() - timedelta(days=1)).isoformat()
    history = {code: [bar for bar in bars if bar["date"] <= completed_day] for code, bars in loaded.items()}
    stocks, members = _universe(result, history)
    cutoff = max((bar["date"] for bars in history.values() for bar in bars), default=None)
    model_path = Path(model_dir) / "shared_baseline.json"
    artifact = _read(model_path)
    if not isinstance(artifact, dict) or artifact.get("version") != VERSION:
        artifact = None
    if artifact and ((artifact.get("label_cutoff") and reference_day and artifact["label_cutoff"] > reference_day)
                     or (_day(artifact.get("trained_at")) or "") > now.date().isoformat()):
        artifact = None
    trained = _day(artifact.get("trained_at")) if artifact else None
    retrain = artifact is None or (trained and (now.date() - date.fromisoformat(trained)).days >= settings["retrain_days"])
    if settings["enable_training"] and retrain and history and cutoff and reference_day and cutoff <= reference_day:
        artifact = train_shared_baseline(history, model_dir, settings, now)
    report = deepcopy(artifact.get("report", {})) if artifact else {"status": "not_trained", "promoted": False, "note": "等待完整历史日线后训练共享股票模型。"}
    neural_report = _read(Path(model_dir) / "training-report.json") or _read(Path(model_dir) / "training_report.json")
    if isinstance(neural_report, dict):
        report["neural_candidates"] = {key: neural_report[key] for key in ("status", "device", "runtime_device_name", "models",
            "metrics", "candidates", "promoted", "created_at", "trained_at", "note", "experiment_status", "samples", "stocks",
            "archive_stocks", "selected_stocks", "selection", "split", "confirmation", "selected_by_validation",
            "eligible_for_promotion", "warnings", "promotion_policy") if key in neural_report}
        report["neural_candidates"]["note"] = "神经网络为离线候选试验，尚未接入正式逐股预测；CPU报告不表示用户本机GPU已训练。"
    selected = artifact.get("targets", {}).get("next_open", {}) if artifact else {}
    model_name = selected.get("model", "unavailable")
    training_ready = bool(artifact and artifact.get("targets"))
    context = {"as_of": now.isoformat(), "target_date": target_date, "target_kind": target_kind,
               "reference_date": reference_day, "model": model_name}
    predictions, weights = {}, {}
    for code, stock in sorted(stocks.items()):
        bars = history.get(code, [])
        note = "收益以目标日Tushare前收盘基准计算；该除权基准尚未确定，不发布绝对预测价。"
        prediction = {"code": code, "name": stock.get("name", code), "sector_id": stock.get("sector_id"),
                      "status": "pending", "predicted_return_pct": None, "reference_price": None,
                      "reference_date": reference_day, "target_date": target_date, "target_kind": target_kind,
                      "model": model_name, "note": note}
        if bars and bars[-1]['date'] == reference_day:
            prediction['reference_price'] = bars[-1]['close']
        features = _tabular_features(bars, len(bars) - 1, settings["min_history"]) if bars else None
        if target_kind == "today_close":
            prediction["note"] = "盘中目标需要带时间戳的盘中历史；当前日线模型暂停该目标，避免将昨日模型冒充实时预测。"
        elif not target_date or not reference_day:
            prediction["note"] = "交易日历未核实，暂停预测。"
        elif not bars or bars[-1]["date"] != reference_day:
            prediction["note"] = "缺少目标前一交易日完整行情；停牌或缺失数据不沿用旧价。"
        elif not training_ready:
            prediction["note"] = "共享模型历史覆盖不足，尚未完成按日期验证。"
        elif features is None:
            prediction["note"] = "量价历史窗口不足或字段无效。"
        else:
            value = float(predict_portable(selected["parameters"], [features])[0]) if model_name == "ridge" else 0.
            prediction.update(status="ready" if model_name == "ridge" else "baseline", predicted_return_pct=value)
        predictions[code] = prediction
        basic = stock.get("daily_basic", {}) or {}
        weight_date = _day(basic.get("trade_date") or basic.get("date"))
        weight = _number(basic.get("circ_mv"))
        available = basic.get("available_at") or basic.get("fetched_at")
        try:
            available_in_time = not available or shanghai_now(datetime.fromisoformat(str(available).replace("Z", "+00:00"))) <= now
        except (TypeError, ValueError):
            available_in_time = False
        if weight and weight > 0 and weight_date and reference_day and weight_date <= reference_day and available_in_time:
            weights[code] = weight
    market = aggregate_predictions(predictions, list(stocks), weights, context, settings["minimum_weight_coverage"])
    forecast = {**context, "status": market["status"], "model_version": VERSION, "market": market,
                "coverage": {key: market[key] for key in ("universe_count", "predicted_count", "known_weight_count", "weight_coverage", "count_coverage", "weights_complete")},
                "stocks": list(predictions.values()), "training": report,
                "note": "先逐股预测，再按可得流通市值汇总；当前分类/上市股票池可能含幸存偏差，不声称历史全市场无偏回测。"}
    for sector in result.get("sectors", []):
        sector["equity_prediction"] = aggregate_predictions(predictions, members.get(sector.get("id"), []), weights, context, settings["minimum_weight_coverage"])
    history_root = Path(history_dir).parent if Path(history_dir).name == "daily" else Path(history_dir)
    forecast["evaluation"] = _ledger(history_root / "forecasts", forecast, history, now)
    result["equity_forecast"] = forecast
    return result
