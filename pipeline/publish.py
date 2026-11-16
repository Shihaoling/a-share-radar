"""Small browser snapshots and operational summaries, separate from training state."""
from collections import Counter
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile


_PRIVATE_KEYS = {
    "source_history", "archived_sectors", "sentiment_state", "intraday_state",
    "feature_history", "sentiment_history", "daily_basic_history", "valuation_history",
    "cached_outcome", "constituents", "stock_api_news",
}
_STATE_CACHES = {"daily_basic", "members", "calendar", "news", "realtime", "index_requests", "sw_history_requests"}
_DAILY_FIELDS = {
    "date", "open", "close", "high", "low", "volume", "amount", "volume_unit", "amount_unit",
    "source", "provider", "adjustment", "as_of", "complete", "is_complete",
    "change_pct", "previous_close", "available_at", "price_available_at",
}
_CAP_FIELDS = {"date", "value"}
_MODULE_STATE_FIELDS = {"version", "last_run_at", "status", "requests_this_run", "cooldown_until"}
_FUND_SUMMARY_FIELDS = {"status", "covered_sectors", "total_sectors", "candidate_count", "requests_this_run", "note"}
_EQUITY_COLLECTION_FIELDS = {"status", "source", "endpoint", "requested_days", "available_calendar_days",
    "cached_days", "collected_days", "requests_this_run", "partial_data_days", "excluded_rows",
    "latest_date", "missing_days", "cooldown_until", "error_code", "provider_code", "provider_reason", "reference_note", "universe_basis"}
_EQUITY_COVERAGE_FIELDS = {"universe_count", "predicted_count", "known_weight_count", "weight_coverage",
    "count_coverage", "weights_complete"}


def _select(value, fields):
    return {key: deepcopy(item) for key, item in value.items() if key in fields} if isinstance(value, dict) else {}


def _public_module_state(state):
    """Whitelist small operational facts; never send fund catalogs to browsers."""
    if not isinstance(state, dict):
        return {}
    result = _select(state, _MODULE_STATE_FIELDS)
    result["endpoints"] = {key: _endpoint(value) for key, value in (state.get("endpoints") or {}).items()}
    result["cache_summary"] = {
        key: {"fetched_at": value.get("fetched_at"), "target_date": value.get("target_date"),
              "cached_count": len(value.get("rows", [])) if isinstance(value.get("rows"), list) else 0}
        for key, value in (state.get("cache") or {}).items() if isinstance(value, dict)
    }
    return result


def _chart_rows(rows, fields):
    # The browser never recalculates metrics from these rows. Formula inputs and
    # explanations remain in entity.quant_metrics; per-row vendor valuation,
    # duplicated names/identifiers/URLs and fetch labels are training-state data.
    return [{key: value for key, value in row.items() if key in fields} if isinstance(row, dict) else row
            for row in rows[-252:]]


def _public_state(state):
    if not isinstance(state, dict):
        return {}
    result = {key: _clean(value) for key, value in state.items() if key not in _STATE_CACHES}
    classification = state.get("classification")
    if isinstance(classification, dict):
        result["classification"] = {key: deepcopy(value) for key, value in classification.items() if key != "records"}
        if isinstance(classification.get("records"), list):
            result["classification"]["cached_count"] = len(classification["records"])
    return result


def _clean(value):
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in _PRIVATE_KEYS:
            continue
        if key == "stock_api_state":
            result[key] = _public_state(item)
        elif key == "fund_state":
            result[key] = _public_module_state(item)
        elif key == "equity_collection":
            result[key] = _select(item, _EQUITY_COLLECTION_FIELDS)
        elif key in ("history", "market_cap_history", "cap_history") and isinstance(item, list):
            # Canonical entity histories are chronological; preserve exact bars.
            result[key] = _chart_rows(item, _DAILY_FIELDS if key == "history" else _CAP_FIELDS)
        elif key == "quant_metrics" and isinstance(item, dict):
            metrics = result[key] = _clean(item)
            for metric in metrics.values():
                if not isinstance(metric, dict):
                    continue
                exclusions = metric.pop("exclusions", None)
                if isinstance(exclusions, list):
                    metric["exclusion_count"] = len(exclusions)
                    metric["exclusion_reason_counts"] = dict(Counter(row.get("reason", "unknown")
                        for row in exclusions if isinstance(row, dict)))
        else:
            result[key] = _clean(item)
    return result


def publish_view(dashboard, output_path):
    """Atomically write compact strict JSON without changing the full dashboard.

    Unknown presentation fields are retained. Intraday points, news, predictions
    and ledger observations are not truncated. This view is never a model cache.
    """
    if not isinstance(dashboard, dict):
        raise TypeError("dashboard must be an object")
    # The top-level history duplicates entity histories, unlike entity.history.
    view = _clean({key: value for key, value in dashboard.items() if key != "history"})
    metadata = view.get("metadata")
    if isinstance(metadata, dict) and metadata.get("data_checks") == view.get("data_checks"):
        metadata.pop("data_checks", None)
    view["publication"] = {"kind": "browser_view", "daily_history_limit": 252, "training_state_included": False}
    payload = json.dumps(view, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return view


def _endpoint(record):
    if not isinstance(record, dict):
        return {}
    return {key: record.get(key) for key in ("status", "error_code", "last_success", "last_attempt")} | {
        "next_retry": record.get("cooldown_until") or record.get("retry_after") or record.get("snapshot_retry_after")}


def _entity_summary(entity):
    sentiment = entity.get("sentiment") or {}
    live = entity.get("live_quote") or {}
    members = entity.get("constituents")
    return {
        "id": entity.get("id"), "name": entity.get("name"), "taxonomy": entity.get("taxonomy"),
        "quote": entity.get("price"), "quote_date": entity.get("quote_date") or entity.get("as_of"),
        "provider": entity.get("provider"), "data_quality": entity.get("data_quality"),
        "live_quote": {key: live.get(key) for key in ("price", "quote_timestamp", "provider", "timestamp_quality")} if live else None,
        "bars": len(entity.get("history") or []),
        "primary_bars": len(entity.get("source_history", {}).get("stock_api", [])),
        "latest_bar_date": (entity.get("history") or [{}])[-1].get("date"),
        "constituents": len(members) if isinstance(members, list) else entity.get("membership_count"),
        "membership_complete": entity.get("membership_complete"), "stock_count": len(entity.get("stocks") or []),
        "valuation_coverage": {key: (entity.get("valuation_coverage") or {}).get(key)
                               for key in ("matched_count", "membership_count", "ratio", "trade_date", "complete")},
        "pe": {key: entity.get(key) for key in ("vendor_pe", "vendor_pe_ttm", "vendor_valuation_date")},
        "emotion_label": sentiment.get("label"), "emotion_n": sentiment.get("article_count"),
        "emotion_scored_n": sentiment.get("scored_article_count"), "emotion_method": sentiment.get("method"),
    }


def health_summary(dashboard):
    """Return bounded operational facts; no raw responses, member lists or caches."""
    state = dashboard.get("stock_api_state") or {}
    models = dashboard.get("models") or {}
    classification = state.get("classification") or {}
    funds = dashboard.get("fund_state") or {}
    forecast = dashboard.get("equity_forecast") or {}
    equity_collection = dashboard.get("equity_collection") or {}
    evaluation = forecast.get("evaluation") or {}
    training = forecast.get("training") or {}
    return {
        "generated_at": dashboard.get("generated_at"), "status": dashboard.get("status"),
        "market_session": deepcopy(dashboard.get("market_session")),
        "requests_this_run": state.get("requests_this_run", 0),
        "target_trade_date": state.get("target_trade_date"), "cooldown_until": state.get("cooldown_until"),
        "endpoints": {key: _endpoint(value) for key, value in (state.get("endpoints") or {}).items()},
        "sources": [{"id": source.get("id"), **_endpoint(source), "requests_this_run": source.get("requests_this_run")}
                    for source in dashboard.get("sources", [])],
        "classification": {key: classification.get(key) for key in ("complete", "expected_count", "received_count", "fetched_at")},
        "financial_status": state.get("financial_status"), "financial_message": state.get("financial_message"),
        "daily_basic_quality": deepcopy(state.get("daily_basic", {}).get("quality", {})),
        "members_quality": {key: deepcopy(value.get("quality", {})) for key, value in state.get("members", {}).items()
                            if isinstance(value, dict) and value.get("quality") and not value.get("complete")},
        "market": [_entity_summary(entity) for entity in dashboard.get("market", [])],
        "sectors": [_entity_summary(entity) for entity in dashboard.get("sectors", [])],
        "prediction_status_counts": dict(Counter(row.get("status", "unknown") for row in dashboard.get("predictions", []))),
        "models": {key: models.get(key) for key in ("status", "champion", "challenger", "news_features", "last_trained_at", "feature_version")},
        "sentiment_summary": deepcopy(dashboard.get("sentiment_summary", {})),
        "fund_summary": {**_select(dashboard.get("fund_summary"), _FUND_SUMMARY_FIELDS),
                         "state": _public_module_state(funds)},
        "equity_summary": {
            **_select(forecast, {"status", "as_of", "target_date", "target_kind", "reference_date", "model", "model_version", "note"}),
            "coverage": _select(forecast.get("coverage"), _EQUITY_COVERAGE_FIELDS),
            "collection": _select(equity_collection, _EQUITY_COLLECTION_FIELDS),
            "stock_status_counts": dict(Counter(row.get("status", "unknown") for row in forecast.get("stocks", []) if isinstance(row, dict))),
            "evaluation": _select(evaluation, {"settled_count", "mae_pct", "direction_accuracy", "direction_evaluated", "note"}),
            "training": _select(training, {"status", "promoted", "trained_at", "note"}),
        },
        "news_count": len(dashboard.get("news", [])), "ledger_count": len(dashboard.get("ledger", [])),
    }
