"""Two additional local optimization rounds, preserving all V3 evidence.

The old final 20 dates have already been inspected. They are a repeated
historical reference in V4 and never a new independent confirmation set.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics

import numpy as np

from .daily_forecast import (BASE_NAMES, LOCAL_WIDTH, FEATURE_VERSION, SCHEMA,
                             digest, evaluate, feature_table, load_bundle, predict_asset)
from .train_daily_forecast import CANDIDATES, SIGNALS, _prune_tree, fit_asset, save_json


ROUND1 = [
    {"name": "recent60_median_entity", "kind": "median_entity", "features": "core", "recent_days": 60},
    {"name": "recent60_ridge", "kind": "ridge", "features": "full", "alpha": 100., "recent_days": 60},
    {"name": "recent120_ridge", "kind": "ridge", "features": "full", "alpha": 100., "recent_days": 120},
    {"name": "recent252_ridge", "kind": "ridge", "features": "full", "alpha": 100., "recent_days": 252},
    {"name": "recent120_l1_core", "kind": "quantile", "features": "core", "alpha": .03, "recent_days": 120},
    {"name": "recent120_l1_regime", "kind": "quantile", "features": "full", "alpha": .03, "recent_days": 120},
    {"name": "all_l1_regularized", "kind": "quantile", "features": "full", "alpha": .1},
    {"name": "recent120_l1_weak", "kind": "quantile", "features": "full", "alpha": .01, "recent_days": 120},
]
RANK_ROUND1 = [
    {"name": "ranker120", "kind": "ranker", "features": "full", "recent_days": 120, "leaves": 7, "min_leaf": 40, "rounds": 100},
    {"name": "ranker60", "kind": "ranker", "features": "full", "recent_days": 60, "leaves": 7, "min_leaf": 40, "rounds": 100},
]
RANK_ROUND2 = [
    {"name": "ranker120_shallow", "kind": "ranker", "features": "full", "recent_days": 120, "leaves": 3, "min_leaf": 60, "rounds": 120},
    {"name": "ranker60_regularized", "kind": "ranker", "features": "full", "recent_days": 60, "leaves": 3, "min_leaf": 40, "rounds": 80, "reg_lambda": 40.},
    {"name": "ranker_all", "kind": "ranker", "features": "full", "leaves": 7, "min_leaf": 60, "rounds": 100},
]


def training_subset(rows, recent_days=None):
    if recent_days:
        dates = sorted({r["label_date"] for r in rows})[-recent_days:]
        return [r for r in rows if r["label_date"] >= dates[0]]
    return rows


def relevance_labels(values):
    """Equal realized returns get equal relevance; identities never break ties."""
    ranks = {value: i for i, value in enumerate(sorted(set(values)))}
    return [ranks[value] for value in values]


def adjust_asset(asset, scale=1., shift=0.):
    """Affine output adjustment using existing portable families only."""
    value = deepcopy(asset)
    family = value["family"]
    if family == "constant":
        value["value"] = value["value"] * scale + shift
        value["by_id"] = {k: v * scale + shift for k, v in value.get("by_id", {}).items()}
    elif family == "ridge":
        value["coef"] = [v * scale for v in value["coef"]]
        value["intercept"] = value["intercept"] * scale + shift
    elif family == "lgb":
        value["output_scale"] = value.get("output_scale", 1.) * scale
        if shift:
            if value["output_scale"] == 0:
                return {"family": "constant", "value": shift}
            value["trees"].append({"leaf_value": shift / value["output_scale"]})
    elif family == "by_entity":
        value["fallback"] = adjust_asset(value["fallback"], scale, shift)
        value["models"] = {k: adjust_asset(v, scale, shift) for k, v in value["models"].items()}
    elif family == "signal":
        if shift:
            return {"family": "ridge", "columns": [value["column"]], "coef": [value.get("sign", 1.) * scale],
                    "intercept": shift, "mean": [0.], "scale": [1.]}
        value["sign"] = value.get("sign", 1.) * scale
    else:
        raise ValueError("Unknown portable family")
    return value


def remap_asset(asset, columns):
    value = deepcopy(asset)
    if value["family"] == "by_entity":
        value["fallback"] = remap_asset(value["fallback"], columns)
        value["models"] = {k: remap_asset(v, columns) for k, v in value["models"].items()}
    elif value["family"] == "signal":
        value["column"] = columns[value["column"]]
    elif "columns" in value:
        value["columns"] = [columns[i] for i in value["columns"]]
    return value


def fit_candidate(spec, rows, names, threads=16):
    if "feature_columns" in spec:
        columns = spec["feature_columns"]
        if not columns or len(set(columns)) != len(columns) or any(not isinstance(i, int) or i < 0 or i >= len(names) for i in columns):
            raise ValueError("Invalid feature-column mask")
        narrowed = [{**r, "x": [r["x"][i] for i in columns]} for r in rows]
        sub_spec = {k: v for k, v in spec.items() if k != "feature_columns"}
        sub_spec["features"] = "full"
        return remap_asset(fit_candidate(sub_spec, narrowed, [names[i] for i in columns], threads), columns)
    rows = training_subset(rows, spec.get("recent_days"))
    if spec.get("winsor"):
        lower, upper = np.quantile([r["y"] for r in rows], spec["winsor"])
        rows = [{**r, "y": float(np.clip(r["y"], lower, upper))} for r in rows]
    kind = spec["kind"]
    columns = list(range(len(names))) if spec.get("features") == "full" else list(range(LOCAL_WIDTH)) + list(range(len(BASE_NAMES), len(names)))
    if kind in ("quantile", "ranker"):
        x = np.asarray([[r["x"][i] for i in columns] for r in rows])
        y = np.asarray([r["y"] for r in rows])
        if kind == "quantile":
            from sklearn.linear_model import QuantileRegressor
            from sklearn.preprocessing import StandardScaler
            scaler = StandardScaler().fit(x)
            estimator = QuantileRegressor(quantile=.5, alpha=spec["alpha"], solver="highs").fit(scaler.transform(x), y)
            asset = {"family": "ridge", "columns": columns, "coef": estimator.coef_.tolist(),
                     "intercept": float(estimator.intercept_), "mean": scaler.mean_.tolist(), "scale": scaler.scale_.tolist()}
            expected = estimator.predict(scaler.transform(x[:30]))
            parity_rows = rows[:30]
        else:
            import lightgbm as lgb
            # Labels are within-day relevance ranks, never numerical returns.
            ordered = sorted(rows, key=lambda r: (r["label_date"], r["id"]))
            grouped = {}
            for r in ordered:
                grouped.setdefault(r["label_date"], []).append(r)
            x, relevances, groups = [], [], []
            for day_rows in grouped.values():
                x.extend([[r["x"][i] for i in columns] for r in day_rows])
                relevances.extend(relevance_labels([r["y"] for r in day_rows]))
                groups.append(len(day_rows))
            estimator = lgb.LGBMRanker(objective="lambdarank", n_estimators=spec["rounds"], num_leaves=spec["leaves"],
                                      min_child_samples=spec["min_leaf"], learning_rate=.04, reg_lambda=spec.get("reg_lambda", 15.),
                                      label_gain=list(range(max(groups) + 1)), lambdarank_truncation_level=10,
                                      n_jobs=threads, verbosity=-1, random_state=1701, deterministic=True, force_col_wise=True)
            estimator.fit(np.asarray(x), np.asarray(relevances), group=groups)
            asset = {"family": "lgb", "columns": columns, "output_scale": 1.,
                     "trees": [_prune_tree(t["tree_structure"]) for t in estimator.booster_.dump_model()["tree_info"]]}
            expected = estimator.predict(np.asarray(x[:30]))
            parity_rows = ordered[:30]
        if not np.allclose(expected, predict_asset(asset, parity_rows), rtol=1e-10, atol=1e-10):
            raise ValueError("V4 portable export parity failed")
    else:
        asset = fit_asset(spec, rows, names, threads)
    scale = spec.get("prediction_scale", 1.)
    shift = (1 - scale) * float(np.median([r["y"] for r in rows])) if spec.get("blend_median") else 0.
    return adjust_asset(asset, scale, shift)


def second_round(seed):
    """Actual new fits: output shrinkage, training-median blend, robust labels."""
    seed = {k: deepcopy(v) for k, v in seed.items() if k != "name"}
    return [
        {**seed, "name": "round2_shrink25", "prediction_scale": .25},
        {**seed, "name": "round2_shrink50", "prediction_scale": .5},
        {**seed, "name": "round2_shrink75", "prediction_scale": .75},
        {**seed, "name": "round2_median_blend25", "prediction_scale": .25, "blend_median": True},
        {**seed, "name": "round2_median_blend50", "prediction_scale": .5, "blend_median": True},
        {**seed, "name": "round2_winsor2", "winsor": [.02, .98]},
        {**seed, "name": "round2_winsor5", "winsor": [.05, .95]},
    ]


def run_optimization(dashboard_path, v3_output, output, model_output, threads=16, feature_pilot=None):
    output, v3_output, model_output = Path(output), Path(v3_output), Path(model_output)
    if (output / "study-plan.json").exists():
        raise ValueError("Do not overwrite a prior V4 experiment")
    output.mkdir(parents=True, exist_ok=True)
    source = Path(dashboard_path).read_bytes()
    dashboard = json.loads(source)
    incumbent_bundle = load_bundle(v3_output / "bundle.json")
    v3_development = json.loads((v3_output / "development-results.json").read_text(encoding="utf-8"))
    table = feature_table(dashboard, entity_ids=incumbent_bundle["entity_ids"])
    specs_by_name = {s["name"]: s for s in CANDIDATES}
    incumbents = {key: deepcopy(specs_by_name[incumbent_bundle["selected_candidates"][key]]) for key in ("market", "sector", "sector_ranking")}
    for key in incumbents:
        incumbents[key]["name"] = "incumbent_v3_" + incumbents[key]["name"]
    pilot = json.loads(Path(feature_pilot).read_text(encoding="utf-8")) if feature_pilot else {}
    masks = pilot.get("recommended_v4_masks", pilot.get("recommended_masks", []))
    if feature_pilot:
        if pilot.get("status") != "completed" or not masks:
            raise ValueError("Feature pilot must be completed and supply explicit recommended masks")
        if pilot.get("source_sha256") != hashlib.sha256(source).hexdigest():
            raise ValueError("Feature pilot source does not match this experiment")
        pilot_plan_path = Path(feature_pilot).with_name(Path(feature_pilot).stem + "-plan.json")
        pilot_plan = json.loads(pilot_plan_path.read_text(encoding="utf-8"))
        if digest(pilot_plan) != pilot.get("plan_sha256"):
            raise ValueError("Feature pilot plan SHA256 mismatch")
    mask_grid = {"market": [], "sector": []}
    for mask in masks:
        columns = mask["feature_columns"]
        for group in ("market", "sector"):
            if mask.get("group") and mask["group"] != group:
                continue
            for kind, alpha in (("ridge", 100.), ("quantile", .03)):
                mask_grid[group].append({"name": group + "_pilot_" + mask["name"] + "_" + kind,
                                         "kind": kind, "features": "full", "alpha": alpha,
                                         "feature_columns": columns})
    plan = {"schema_version": "daily-close-v4-two-extra-rounds", "started_at": datetime.now(timezone.utc).isoformat(),
            "target": "100*(next_session.close/current_session.close-1)", "unit": "percentage_points", "mae_target": .1,
            "user_rule": "Two additional optimization rounds must both be completed regardless of first-round improvement",
            "source_sha256": hashlib.sha256(source).hexdigest(), "incumbent_bundle_sha256": incumbent_bundle["sha256"],
            "implementation_sha256": {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                                       for name in ("daily_forecast.py", "train_daily_forecast.py", "optimize_daily_forecast.py", "calendar.py")},
            "feature_version": FEATURE_VERSION, "feature_names": table["feature_names"], "incumbent_configurations": incumbents,
            "round1_numeric": ROUND1, "round1_ranking": RANK_ROUND1, "round2_ranking": RANK_ROUND2,
            "feature_pilot": {"file": str(feature_pilot), "sha256": hashlib.sha256(Path(feature_pilot).read_bytes()).hexdigest(), "recommended_masks": masks} if feature_pilot else None,
            "pilot_feature_candidates": mask_grid,
            "round2_numeric_generator": "Select best nonconstant Round1/Incumbent by development MAE; fit with output scales .25/.5/.75, train-median blends .25/.5, train-only winsor 2/98 or 5/95 percentiles. Retain incumbent and best Round1 overall.",
            "development_protocol": "Same 45 outer development label dates as V3; each fit uses labels strictly before first outer evaluation label. Both rounds may use these development results for tuning.",
            "old_20_day_reference": "ALREADY_SEEN_REPEATED_HISTORICAL_REFERENCE_NOT_NEW_HOLDOUT; never used for model selection",
            "ranking_protocol": "Select greatest mean daily RankIC among >=95% covered development dates, numeric and ranking separately; LambdaRank scores are not return forecasts",
            "deployment": "Refit chosen configurations using all matured history, separate weights/hash from repeated-reference assets",
            "threads": threads}
    save_json(output / "study-plan.json", plan)
    progress = {"stage": "training", "status": "training", "completed_fits": 0,
                "total_fits": 122 + 3 * sum(len(v) for v in mask_grid.values()), "started_at": plan["started_at"]}
    def note(**values):
        progress.update(values, updated_at=datetime.now(timezone.utc).isoformat())
        save_json(output / "progress.json", progress)
        print(json.dumps(values, ensure_ascii=False), flush=True)
    def fit(spec, rows):
        asset = fit_candidate(spec, rows, table["feature_names"], threads)
        progress["completed_fits"] += 1
        note(candidate=spec["name"], training_rows=len(rows))
        return asset
    reports, choices, all_specs = {}, {}, {}
    for data_key, key in (("market", "market"), ("sectors", "sector")):
        rows = [r for r in table[data_key] if "label_date" in r]
        folds = v3_development[key]["folds"]
        fold_data = [( [r for r in rows if r["label_date"] < f["outer_first"]],
                       [r for r in rows if f["outer_first"] <= r["label_date"] <= f["outer_last"]]) for f in folds]
        eval_rows = [r for _, test in fold_data for r in test]
        predictions, metrics, numeric_specs, rank_specs = {}, {}, {}, {}
        def evaluate_spec(spec, numeric=True):
            if spec["name"] in metrics:
                return
            values = []
            for train, test in fold_data:
                asset = fit(spec, train)
                values.extend(predict_asset(asset, test))
            predictions[spec["name"]] = values
            metrics[spec["name"]] = evaluate(eval_rows, values)
            if numeric:
                numeric_specs[spec["name"]] = spec
            rank_specs[spec["name"]] = spec
        note(group=key, phase="incumbent_refit", message="重新拟合V3配置，禁止用全历史权重回测过去")
        evaluate_spec(incumbents[key])
        if key == "sector":
            evaluate_spec(incumbents["sector_ranking"], numeric=False)
            for signal in SIGNALS:
                metrics[signal["name"]] = evaluate(eval_rows, predict_asset(signal, eval_rows))
                rank_specs[signal["name"]] = signal
        incumbent_mae = metrics[incumbents[key]["name"]]["mae_pct"]
        note(phase="optimization_round_1", message="第1轮：近期训练窗与L1中位数线性模型")
        for spec in ROUND1 + mask_grid[key]:
            evaluate_spec(spec)
        if key == "sector":
            for spec in RANK_ROUND1:
                evaluate_spec(spec, numeric=False)
        first_best = min(numeric_specs, key=lambda n: metrics[n]["mae_pct"])
        learned = [n for n in numeric_specs if numeric_specs[n]["kind"] not in ("zero", "median", "median_entity")]
        seed_name = min(learned, key=lambda n: metrics[n]["mae_pct"])
        seed = numeric_specs[seed_name]
        first_best_mae = metrics[first_best]["mae_pct"]
        first_rank = best_ranking(metrics, rank_specs) if key == "sector" else None
        round2_specs = second_round(seed)
        for spec in round2_specs:
            spec["name"] = key + "_" + spec["name"]
        save_json(output / f"{key}-round2-plan.json", {"declared_at": datetime.now(timezone.utc).isoformat(), "seed": seed_name,
                   "first_round_best": first_best, "candidates": round2_specs, "ranking_candidates": RANK_ROUND2 if key == "sector" else []})
        note(phase="optimization_round_2", message="第2轮：预测收缩、训练中位数组合与训练段稳健截尾", first_round_best=first_best)
        for spec in round2_specs:
            evaluate_spec(spec)
        if key == "sector":
            for spec in RANK_ROUND2:
                evaluate_spec(spec, numeric=False)
        winner = min(numeric_specs, key=lambda n: metrics[n]["mae_pct"])
        choices[key] = winner
        all_specs.update(numeric_specs)
        if key == "sector":
            choices["sector_ranking"] = best_ranking(metrics, rank_specs)
            all_specs.update(rank_specs)
        rounds = [{"round": 1, "previous_best_mae_pct": incumbent_mae, "retained_best_mae_pct": first_best_mae,
                   "improvement_pct_points": incumbent_mae - first_best_mae, "best": first_best, "ranking_best": first_rank},
                  {"round": 2, "previous_best_mae_pct": first_best_mae, "retained_best_mae_pct": metrics[winner]["mae_pct"],
                   "improvement_pct_points": first_best_mae - metrics[winner]["mae_pct"], "best": winner,
                   "ranking_best": choices.get("sector_ranking") if key == "sector" else None}]
        reports[key] = {"incumbent_mae_pct": incumbent_mae, "rounds": rounds, "metrics": metrics,
                        "selected": winner, "folds": [{k: f[k] for k in ("outer_first", "outer_last", "max_outer_train_label")} for f in folds],
                        "second_round_seed": seed_name}
        save_json(output / "development-results.json", reports)
        note(phase="development_complete", group=key, rounds=rounds)
    lock = {"locked_at": datetime.now(timezone.utc).isoformat(), "choices": choices,
            "specifications": {k: all_specs[n] for k, n in choices.items()}, "plan_sha256": digest(plan),
            "development_sha256": digest(reports), "repeated_reference_not_used_for_selection": True}
    save_json(output / "selection-lock.json", lock)
    evaluation, reference_assets, deployed = {}, {}, {}
    for data_key, key in (("market", "market"), ("sectors", "sector")):
        rows = [r for r in table[data_key] if "label_date" in r]
        first_ref = incumbent_bundle["evaluation"][key]["first_label_date"]
        last_ref = incumbent_bundle["evaluation"][key]["last_label_date"]
        train = [r for r in rows if r["label_date"] < first_ref]
        reference = [r for r in rows if first_ref <= r["label_date"] <= last_ref]
        spec = all_specs[choices[key]]
        reference_assets[key] = fit(spec, train)
        current = evaluate(reference, predict_asset(reference_assets[key], reference))
        incumbent_asset = fit(incumbents[key], train)
        incumbent_score = evaluate(reference, predict_asset(incumbent_asset, reference))
        current.update(target_met=current["mae_target_met"], baseline_name=incumbents[key]["name"],
                       baseline_mae_pct=incumbent_score["mae_pct"], selected_candidate=choices[key])
        evaluation[key] = current
        deployed[key] = fit(spec, rows)
        if key == "sector":
            rank_spec = all_specs[choices["sector_ranking"]]
            rank_asset = deepcopy(rank_spec) if rank_spec.get("family") == "signal" else fit(rank_spec, train)
            reference_assets["sector_ranking"] = rank_asset
            rank_score = evaluate(reference, predict_asset(rank_asset, reference))
            evaluation["sector_ranking"] = {"selected_candidate": choices["sector_ranking"], "daily_rank_ic": rank_score["daily_rank_ic"],
                                            "ranking_valid_dates": rank_score["ranking_valid_dates"], "ranking_coverage": rank_score["ranking_coverage"],
                                            "dates": rank_score["dates"], "not_a_return_or_probability": True}
            deployed["sector_ranking"] = deepcopy(rank_spec) if rank_spec.get("family") == "signal" else fit(rank_spec, rows)
    evaluation.update(protocol="V4 exactly two extra optimization rounds; same 45 development dates; old final20 reused only as repeated historical reference",
                      historical_research=True, repeated_historical_reference=True, fresh_holdout=False,
                      note="两轮开发调优后固定选择；末20日已在V3看过，仅作重复历史参考，不是新的独立确认；未据此选模。",
                      unit="percentage_points", mae_target=.1, locked_at=lock["locked_at"],
                      target_met_all_objects=all(v["mae_target_met"] for k in ("market", "sector") for v in evaluation[k]["by_id"].values()))
    save_json(output / "repeated-reference-assets.json", reference_assets)
    save_json(output / "repeated-reference-evaluation.json", evaluation)
    bundle = {**{k: deepcopy(incumbent_bundle[k]) for k in ("schema_version", "feature_version", "horizon_trading_sessions", "target", "unit", "mae_target", "entity_ids", "feature_names", "historical_price_availability_assumption")},
              "model_id": "daily-v4-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
              "trained_at": datetime.now(timezone.utc).isoformat(), "training_cutoff": max(r["label_date"] for k in ("market", "sectors") for r in table[k] if "label_date" in r),
              "selected_candidates": choices, "models": deployed, "evaluation": evaluation,
              "plan_sha256": digest(plan), "selection_sha256": digest(lock),
              "evaluation_asset_sha256": digest(reference_assets), "deployment_asset_sha256": digest(deployed),
              "promotion_policy": "user-authorized best retained development model after two additional rounds; unmet 0.1 target must remain visible",
              "previous_model_id": incumbent_bundle["model_id"], "previous_bundle_sha256": incumbent_bundle["sha256"]}
    bundle["sha256"] = digest(bundle)
    save_json(output / "bundle.json", bundle)
    save_json(model_output / "bundle.json", bundle)
    summary = {"status": "completed", "schema_version": plan["schema_version"], "started_at": plan["started_at"], "completed_at": bundle["trained_at"],
               "selected_candidates": choices, "optimization_rounds": {k: v["rounds"] for k, v in reports.items()},
               "evaluation": evaluation, "bundle_sha256": bundle["sha256"], "bundle_bytes": (model_output / "bundle.json").stat().st_size,
               "plan_sha256": digest(plan), "fit_completed": progress["completed_fits"], "uploaded": False,
               "v3_preserved": True, "deployment_refitted": True}
    save_json(output / "summary.json", summary)
    note(stage="completed", status="completed", total_fits=progress["completed_fits"], phase="completed", summary=summary)
    return summary


def best_ranking(metrics, specs):
    names = [n for n in specs if metrics[n]["ranking_coverage"] >= .95 and metrics[n]["daily_rank_ic"] is not None]
    if not names:
        raise ValueError("No ranking model has the predeclared minimum coverage")
    return max(names, key=lambda n: metrics[n]["daily_rank_ic"])


def main(argv=None):
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dashboard", default=str(root / "web/data/dashboard.json"))
    parser.add_argument("--v3-output", default=str(root.parent / "research/daily-v3"))
    parser.add_argument("--output", default=str(root.parent / "research/daily-v4"))
    parser.add_argument("--model-output", default=str(root / "data/daily_forecast/models"))
    parser.add_argument("--threads", type=int, default=16)
    parser.add_argument("--feature-pilot")
    args = parser.parse_args(argv)
    if os.environ.get("RADAR_ALLOW_TRAINING", "true").lower() in ("false", "0", "no", "off"):
        raise SystemExit("Local training explicitly disabled")
    run_optimization(args.dashboard, args.v3_output, args.output, args.model_output, max(1, min(16, args.threads)), args.feature_pilot)


if __name__ == "__main__":
    main()
