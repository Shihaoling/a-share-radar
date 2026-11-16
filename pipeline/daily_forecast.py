"""Causal next-session index forecasts and portable, inference-only JSON assets.

Prices are retrospective daily observations, not historical arrival logs.  This
module never uses current memberships, valuations, news or future return labels
as inputs. Training lives in train_daily_forecast and is never called here.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time
import hashlib
import json
import math
from pathlib import Path
import statistics

from .calendar import SHANGHAI, is_trading_day, next_trading_day, shanghai_now


SCHEMA = "daily-index-sector-close-v3"
FEATURE_VERSION = "causal-index-volume-regime-v1"
BASE_NAMES = ["return_1", "overnight_1", "intraday_1", "range_1", "close_location"]
for _period in (5, 10, 20):
    BASE_NAMES += [f"{name}_{_period}" for name in
                   ("momentum", "return_mean", "volatility", "up_fraction", "trend_efficiency")]
BASE_NAMES += ["volume_ratio_5", "volume_missing_5", "volume_ratio_20", "volume_missing_20",
               "amount_ratio_5", "amount_missing_5", "amount_ratio_20", "amount_missing_20"]
LOCAL_WIDTH = len(BASE_NAMES)
BASE_NAMES += ["market_return_1", "market_momentum_5", "market_momentum_20", "market_volatility_20",
               "relative_return_1", "relative_momentum_5", "relative_momentum_20", "market_missing",
               "sector_mean_return", "sector_return_dispersion", "sector_up_fraction", "sector_missing"]


def canonical_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def clean_history(entity, through=None, available_by=None):
    rows = {}
    for raw in entity.get("history", []):
        day = str(raw.get("date", ""))[:10]
        try:
            date.fromisoformat(day)
        except ValueError:
            continue
        if (through and day > through) or is_trading_day(day) is not True:
            continue
        if raw.get("complete") is False or raw.get("is_complete") is False:
            continue
        if available_by is not None:
            timestamps_valid = True
            for key in ("price_available_at", "available_at", "fetched_at", "as_of"):
                if not raw.get(key):
                    continue
                try:
                    stamp = datetime.fromisoformat(str(raw[key]).replace("Z", "+00:00"))
                    if stamp.tzinfo is None or stamp.astimezone(SHANGHAI) > available_by:
                        timestamps_valid = False
                except ValueError:
                    timestamps_valid = False
            if not timestamps_valid:
                continue
        values = [raw.get(k) for k in ("open", "high", "low", "close")]
        if not all(_finite(v) and v > 0 for v in values):
            continue
        if raw["low"] > min(raw["open"], raw["close"]) or raw["high"] < max(raw["open"], raw["close"]):
            continue
        if day in rows and rows[day] != raw:
            raise ValueError(f"Conflicting historical bars: {entity.get('id')} {day}")
        rows[day] = dict(raw)
    return [rows[k] for k in sorted(rows)]


def _contiguous(rows):
    return all(next_trading_day(a["date"]) is not None and
               next_trading_day(a["date"]).isoformat() == b["date"] for a, b in zip(rows, rows[1:]))


def _returns(rows):
    return [100 * (b["close"] / a["close"] - 1) for a, b in zip(rows, rows[1:])]


def _flow_ratio(rows, key, period):
    block = rows[-period:]
    # A consistent source and declared unit are required. No raw-volume ratio
    # is allowed to cross a vendor/unit switch. A missing flag is an input.
    identities = {(b.get("source"), b.get(key + "_unit")) for b in block}
    vals = [b.get(key) for b in block]
    if (len(identities) != 1 or not all(_finite(v) and v > 0 for v in vals)
            or next(iter(identities))[1] is None):
        return [0.0, 1.0]
    average = statistics.fmean(vals)
    return [math.log(vals[-1] / average), 0.0]


def _own_features(rows):
    if len(rows) < 21 or not _contiguous(rows[-21:]):
        return None
    block, bar = rows[-21:], rows[-1]
    prev = rows[-2]["close"]
    changes = _returns(block)
    span = bar["high"] - bar["low"]
    x = [changes[-1], 100 * (bar["open"] / prev - 1),
         100 * (bar["close"] / bar["open"] - 1), 100 * span / prev,
         (bar["close"] - bar["low"]) / span if span else .5]
    for period in (5, 10, 20):
        r = changes[-period:]
        close = [b["close"] for b in block[-period - 1:]]
        path = sum(abs(b - a) for a, b in zip(close, close[1:]))
        x += [100 * (close[-1] / close[0] - 1), statistics.fmean(r),
              statistics.pstdev(r), statistics.fmean(v > 0 for v in r),
              abs(close[-1] - close[0]) / path if path else 0.0]
    for key in ("volume", "amount"):
        for period in (5, 20):
            x += _flow_ratio(block, key, period)
    return x


def feature_table(dashboard, through=None, entity_ids=None):
    """Build feature-date rows only. Labels are attached separately below."""
    entities = [(group, e) for group in ("market", "sectors") for e in dashboard.get(group, []) if e.get("id")]
    ids = sorted(entity_ids or {e["id"] for _, e in entities})
    histories = {e["id"]: clean_history(e, through) for _, e in entities}
    base = {}
    for identity, rows in histories.items():
        for i in range(20, len(rows)):
            own = _own_features(rows[:i + 1])
            if own is not None:
                base[(identity, rows[i]["date"])] = own
    sector_ids = {e["id"] for group, e in entities if group == "sectors"}
    sector_returns = {}
    for (identity, day), own in base.items():
        if identity in sector_ids:
            sector_returns.setdefault(day, []).append(own[0])
    output = {"market": [], "sectors": []}
    for group, entity in entities:
        identity = entity["id"]
        if identity not in ids:
            continue
        rows = histories[identity]
        for i in range(20, len(rows)):
            bar = rows[i]
            own = base.get((identity, bar["date"]))
            if own is None:
                continue
            market = base.get(("sh000300", bar["date"]))
            if market:
                context = [market[0], market[5], market[15], market[17],
                           own[0] - market[0], own[5] - market[5], own[15] - market[15], 0.0]
            else:
                context = [0.0] * 7 + [1.0]
            peers = sector_returns.get(bar["date"], [])
            context += ([statistics.fmean(peers), statistics.pstdev(peers),
                         statistics.fmean(v > 0 for v in peers), 0.0] if peers else [0., 0., 0., 1.])
            x = own + context + [float(identity == value) for value in ids]
            if not all(_finite(v) for v in x):
                continue
            row = {"id": identity, "name": entity.get("name", identity),
                   "feature_date": bar["date"], "reference_close": bar["close"], "x": x}
            if i + 1 < len(rows) and next_trading_day(bar["date"]) == date.fromisoformat(rows[i + 1]["date"]):
                row.update(label_date=rows[i + 1]["date"],
                           y=100 * (rows[i + 1]["close"] / bar["close"] - 1))
            output[group].append(row)
    output["entity_ids"] = ids
    output["feature_names"] = BASE_NAMES + ["identity_" + identity for identity in ids]
    return output


def _tree_value(node, x):
    for _ in range(100):
        if "leaf_value" in node:
            return float(node["leaf_value"])
        value = x[node["split_feature"]]
        go_left = node.get("default_left", True) if value is None else value <= node["threshold"]
        node = node["left_child"] if go_left else node["right_child"]
    raise ValueError("Invalid or excessively deep portable tree")


def predict_asset(asset, rows):
    """No fitting, deserialization of executable objects or LightGBM runtime."""
    family, out = asset["family"], []
    if family == "by_entity":
        for row in rows:
            out += predict_asset(asset["models"].get(row["id"], asset["fallback"]), [row])
        return out
    for row in rows:
        x = [row["x"][i] for i in asset.get("columns", range(len(row["x"])))]
        if family == "constant":
            value = asset.get("by_id", {}).get(row["id"], asset["value"])
        elif family == "ridge":
            if len(x) != len(asset["coef"]):
                raise ValueError("Portable Ridge feature width mismatch")
            value = asset["intercept"] + sum(c * (v - mean) / scale for c, v, mean, scale in
                                             zip(asset["coef"], x, asset["mean"], asset["scale"]))
        elif family == "lgb":
            value = sum(_tree_value(tree, x) for tree in asset["trees"]) * asset.get("output_scale", 1.)
        elif family == "signal":
            value = row["x"][asset["column"]] * asset.get("sign", 1.)
        else:
            raise ValueError("Unsupported portable model family")
        if not _finite(value):
            raise ValueError("Non-finite model output")
        out.append(float(value))
    return out


def load_bundle(model_root):
    path = Path(model_root)
    if path.is_dir() or path.suffix != ".json":
        path /= "bundle.json"
    bundle = json.loads(path.read_text(encoding="utf-8"))
    stated = bundle.pop("sha256", None)
    if not stated or digest(bundle) != stated:
        raise ValueError("Daily forecast bundle SHA256 mismatch")
    bundle["sha256"] = stated
    if bundle.get("schema_version") != SCHEMA or bundle.get("feature_version") != FEATURE_VERSION:
        raise ValueError("Daily forecast feature/schema mismatch")
    expected = BASE_NAMES + ["identity_" + value for value in bundle["entity_ids"]]
    if bundle.get("feature_names") != expected or bundle.get("horizon_trading_sessions") != 1:
        raise ValueError("Daily forecast feature/target contract mismatch")
    if bundle.get("target") != "next_close_return_pct" or bundle.get("unit") != "percentage_points":
        raise ValueError("Daily forecast target/unit mismatch")
    return bundle


def _ranks(values):
    indexed = sorted(enumerate(values), key=lambda p: p[1])
    ranks = [0.] * len(values)
    i = 0
    while i < len(indexed):
        j = i + 1
        while j < len(indexed) and indexed[j][1] == indexed[i][1]:
            j += 1
        for k in range(i, j):
            ranks[indexed[k][0]] = (i + j - 1) / 2 + 1
        i = j
    return ranks


def rank_ic(actual, predicted):
    if len(actual) < 3 or len(set(actual)) < 2 or len(set(predicted)) < 2:
        return None
    a, b = _ranks(actual), _ranks(predicted)
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    numerator = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    denominator = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return numerator / denominator if denominator else None


def evaluate(rows, predictions):
    if len(rows) != len(predictions) or not rows:
        raise ValueError("Nonempty paired evaluation required")
    by_date, by_id = {}, {}
    for row, prediction in zip(rows, predictions):
        by_date.setdefault(row["label_date"], []).append((row["y"], prediction))
        by_id.setdefault(row["id"], []).append((row["y"], prediction))
    per_date = [statistics.fmean(abs(y - p) for y, p in pairs) for pairs in by_date.values()]
    rank_values = [rank_ic([y for y, _ in pairs], [p for _, p in pairs]) for pairs in by_date.values()]
    valid_rank = [v for v in rank_values if v is not None]
    rmse = math.sqrt(statistics.fmean(statistics.fmean((y - p) ** 2 for y, p in pairs) for pairs in by_date.values()))
    return {"mae_pct": statistics.fmean(per_date), "rmse_pct": rmse,
            "mae_target_met": statistics.fmean(per_date) <= .1,
            "dates": len(by_date), "rows": len(rows), "first_label_date": min(by_date), "last_label_date": max(by_date),
            "daily_rank_ic": statistics.fmean(valid_rank) if valid_rank else None,
            "ranking_valid_dates": len(valid_rank), "ranking_coverage": len(valid_rank) / len(by_date),
            "by_id": {identity: {"mae_pct": statistics.fmean(abs(y - p) for y, p in pairs),
                                  "mae_target_met": statistics.fmean(abs(y - p) for y, p in pairs) <= .1,
                                  "count": len(pairs)} for identity, pairs in by_id.items()},
            "per_date_mae": {day: statistics.fmean(abs(y - p) for y, p in pairs) for day, pairs in by_date.items()}}


def _update_daily_forecasts(dashboard, model_root, now=None, previous=None):
    """Return a copied dashboard; retain and settle immutable first issues."""
    result = deepcopy(dashboard)
    now = shanghai_now(now)
    old = (previous or dashboard).get("daily_forecast", {})
    ledger = deepcopy(old.get("ledger", []))
    state = {"schema_version": SCHEMA, "status": "pending", "reason": "", "model_id": None,
             "issued_at": now.isoformat(), "trained_at": None, "target_date": None,
             "target": "next_close_return_pct", "unit": "percentage_points", "mae_target": .1,
             "horizon_trading_sessions": 1, "market": [], "sectors": [], "evaluation": {}, "ledger": ledger}
    result["daily_forecast"] = state
    try:
        bundle = load_bundle(model_root)
    except FileNotFoundError:
        state["reason"] = "等待本机训练的指数与板块收盘模型"
        return result
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        state.update(status="error", reason="模型资产校验失败：" + str(error))
        return result
    state.update(model_id=bundle["model_id"], trained_at=bundle["trained_at"],
                 training_cutoff=bundle["training_cutoff"], evaluation=deepcopy(bundle["evaluation"]),
                 model_sha256=bundle["sha256"], probability_status="not_fitted", interval_status="not_fitted")
    # Settle only against complete, exact target-date official index bars.
    entity_map = {e["id"]: e for group in ("market", "sectors") for e in dashboard.get(group, []) if e.get("id")}
    completed_through = now.date().isoformat()
    if now.time().replace(tzinfo=None) < time(15, 30):
        completed_through = (now.date()).isoformat()
    histories = {}
    for identity, entity in entity_map.items():
        bars = clean_history(entity, completed_through, available_by=now)
        if now.time().replace(tzinfo=None) < time(15, 30):
            bars = [b for b in bars if b["date"] != now.date().isoformat()]
        histories[identity] = {b["date"]: b for b in bars}
    for record in ledger:
        if record.get("settled_at"):
            continue
        bar = histories.get(record.get("id"), {}).get(record.get("target_date"))
        if bar and record.get("reference_close", 0) > 0:
            actual = 100 * (bar["close"] / record["reference_close"] - 1)
            record.update(actual_return_pct=actual, actual_close=bar["close"],
                          absolute_error_pct=abs(actual - record["predicted_return_pct"]), settled_at=now.isoformat())
    # Derive the next complete-session forecast from last verified reference.
    usable = deepcopy(dashboard)
    for group in ("market", "sectors"):
        for entity in usable.get(group, []):
            entity["history"] = list(histories.get(entity.get("id"), {}).values())
    table = feature_table(usable, entity_ids=bundle["entity_ids"])
    candidates = [row for group in ("market", "sectors") for row in table[group]]
    if not candidates:
        state["reason"] = "完整日线不足或交易日历未核实"
        return result
    reference_date = max(row["feature_date"] for row in candidates)
    target = next_trading_day(reference_date)
    if target is None or is_trading_day(now.date()) is None:
        state["reason"] = "目标交易日历未核实"
        return result
    if target < now.date() or (target == now.date() and now.time().replace(tzinfo=None) >= time(15, 30)):
        state["reason"] = "参考日线已过时，等待最近交易日完整收盘数据"
        return result
    if reference_date < bundle["training_cutoff"]:
        state["reason"] = "数据早于模型训练截止，拒绝回填历史预测"
        return result
    state.update(reference_date=reference_date, target_date=target.isoformat())
    for group, key in (("market", "market"), ("sectors", "sector")):
        rows = [r for r in table[group] if r["feature_date"] == reference_date]
        predictions = predict_asset(bundle["models"][key], rows)
        ranking = predict_asset(bundle["models"]["sector_ranking"], rows) if group == "sectors" else [None] * len(rows)
        for row, prediction, rank_score in zip(rows, predictions, ranking):
            metrics = bundle["evaluation"][key].get("by_id", {}).get(row["id"], {})
            item = {"id": row["id"], "name": row["name"], "reference_date": reference_date,
                    "target_date": target.isoformat(), "reference_close": row["reference_close"],
                    "predicted_return_pct": prediction, "predicted_close": row["reference_close"] * (1 + prediction / 100),
                    "model_family": bundle["selected_candidates"][key], "validation_mae_pct": metrics.get("mae_pct"),
                    "mae_target_met": metrics.get("mae_target_met", False), "up_probability": None,
                    "ranking_score": rank_score, "ranking_model_family": bundle["selected_candidates"].get("sector_ranking") if group == "sectors" else None,
                    "issued_at": now.isoformat(), "model_id": bundle["model_id"]}
            if not _finite(item["predicted_close"]) or item["predicted_close"] <= 0:
                raise ValueError("Invalid predicted index close")
            # A new model may not rewrite an already issued object/date forecast.
            existing = next((v for v in ledger if v.get("id") == row["id"]
                             and v.get("target_date") == target.isoformat()), None)
            if existing:
                item = deepcopy(existing)
            else:
                ledger.append(deepcopy(item))
            state[group].append(item)
    market_prediction = next((r["predicted_return_pct"] for r in state["market"] if r["id"] == "sh000300"), None)
    scores = [r["ranking_score"] for r in state["sectors"]]
    ranks = _ranks([-v for v in scores]) if scores and len(set(scores)) > 1 else [None] * len(scores)
    for row, rank in zip(state["sectors"], ranks):
        row.update(relative_return_pct=(row["predicted_return_pct"] - market_prediction) if market_prediction is not None else None,
                   rank=rank)
    settled = [v for v in ledger if v.get("settled_at") and v.get("model_id") == bundle["model_id"]]
    state["forward_evaluation"] = {"settled_rows": len(settled), "mae_pct": statistics.fmean(v["absolute_error_pct"] for v in settled) if settled else None,
                                   "note": "仅对实际留存后成熟的标签结算，不混入历史回溯分数"}
    state.update(status="ready" if state["market"] and state["sectors"] else "partial",
                 reason="收盘后的一交易日预测；历史回溯误差不代表未来保证；强弱排序与数值涨幅独立选模")
    return result


def update_daily_forecasts(dashboard, model_root, now=None, previous=None):
    """Contain corrupt asset/feature failures without breaking data collection."""
    try:
        return _update_daily_forecasts(dashboard, model_root, now, previous)
    except (ValueError, KeyError, TypeError, IndexError, OverflowError, ZeroDivisionError) as error:
        result = deepcopy(dashboard)
        old = (previous or dashboard).get("daily_forecast", {})
        result["daily_forecast"] = {"schema_version": SCHEMA, "status": "error",
                                    "reason": "收盘模型推断校验失败：" + str(error),
                                    "market": [], "sectors": [], "evaluation": {},
                                    "ledger": deepcopy(old.get("ledger", [])),
                                    "target": "next_close_return_pct", "unit": "percentage_points",
                                    "mae_target": .1, "horizon_trading_sessions": 1}
        return result
