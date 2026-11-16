"""Local-only fixed-grid daily-index research; never imported by inference."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import statistics
import time

import numpy as np

from .daily_forecast import (SCHEMA, FEATURE_VERSION, BASE_NAMES, LOCAL_WIDTH,
                             canonical_bytes, digest, evaluate, feature_table, predict_asset)


CANDIDATES = [
    {"name": "zero", "stage": "baseline_round", "kind": "zero", "features": "core"},
    {"name": "median", "stage": "baseline_round", "kind": "median", "features": "core"},
    {"name": "median_entity", "stage": "baseline_round", "kind": "median_entity", "features": "core"},
    {"name": "ridge_core", "stage": "baseline_round", "kind": "ridge", "features": "core", "alpha": 100.},
    {"name": "ridge_entity", "stage": "baseline_round", "kind": "ridge_entity", "features": "core", "alpha": 100.},
    {"name": "lgb_core", "stage": "baseline_round", "kind": "lgb", "features": "core", "leaves": 7, "min_leaf": 40, "rounds": 100},
    {"name": "ridge_regime", "stage": "optimization_round_1", "kind": "ridge", "features": "full", "alpha": 100.},
    {"name": "ridge_regime_regularized", "stage": "optimization_round_1", "kind": "ridge", "features": "full", "alpha": 1000.},
    {"name": "lgb_regime_shallow", "stage": "optimization_round_1", "kind": "lgb", "features": "full", "leaves": 3, "min_leaf": 80, "rounds": 120},
    {"name": "lgb_regime", "stage": "optimization_round_1", "kind": "lgb", "features": "full", "leaves": 7, "min_leaf": 40, "rounds": 120},
    {"name": "lgb_regime_shrunk", "stage": "optimization_round_1", "kind": "lgb", "features": "full", "leaves": 7, "min_leaf": 40, "rounds": 120, "output_scale": .35},
]
SIGNALS = [
    {"name": "momentum_5", "family": "signal", "column": 5, "sign": 1.},
    {"name": "momentum_20", "family": "signal", "column": 15, "sign": 1.},
    {"name": "reversal_1", "family": "signal", "column": 0, "sign": -1.},
    {"name": "reversal_5", "family": "signal", "column": 5, "sign": -1.},
]


def continue_optimization(previous_best, new_best):
    return new_best > .1 and previous_best - new_best > .1 + 1e-12


def optimization_candidates(round_number):
    """Predeclared deterministic grid, used only if the previous gain >0.1pp."""
    stage = f"optimization_round_{round_number}"
    return [
        {"name": stage + "_ridge", "stage": stage, "kind": "ridge", "features": "full", "alpha": 10. ** max(-2, 4 - round_number)},
        {"name": stage + "_tree", "stage": stage, "kind": "lgb", "features": "full", "leaves": min(31, 2 ** (round_number + 2) - 1), "min_leaf": max(10, 50 // round_number), "rounds": min(300, 100 + 30 * round_number)},
        {"name": stage + "_shrunk", "stage": stage, "kind": "lgb", "features": "full", "leaves": 3, "min_leaf": max(10, 80 // round_number), "rounds": min(300, 100 + 30 * round_number), "output_scale": max(.1, .85 - .1 * round_number)},
    ]


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(canonical_bytes(value))
    temp.replace(path)


def _prune_tree(node):
    if "leaf_value" in node:
        return {"leaf_value": float(node["leaf_value"])}
    if node.get("decision_type") != "<=":
        raise ValueError("Only numeric LightGBM splits may be exported")
    return {"split_feature": int(node["split_feature"]), "threshold": float(node["threshold"]),
            "default_left": bool(node["default_left"]),
            "left_child": _prune_tree(node["left_child"]), "right_child": _prune_tree(node["right_child"])}


def fit_asset(spec, rows, feature_names, threads=16):
    if not rows:
        raise ValueError("Cannot fit empty training segment")
    kind = spec["kind"]
    y = np.asarray([r["y"] for r in rows], dtype=float)
    if not np.isfinite(y).all():
        raise ValueError("Non-finite training label")
    if kind in ("zero", "median", "median_entity"):
        values = {}
        if kind == "median_entity":
            for identity in sorted({r["id"] for r in rows}):
                values[identity] = float(np.median([r["y"] for r in rows if r["id"] == identity]))
        return {"family": "constant", "value": float(np.median(y)) if kind != "zero" else 0., "by_id": values}
    if kind == "ridge_entity":
        pooled = fit_asset({**spec, "kind": "ridge"}, rows, feature_names, threads)
        models = {}
        for identity in sorted({r["id"] for r in rows}):
            subset = [r for r in rows if r["id"] == identity]
            if len(subset) >= 40:
                models[identity] = fit_asset({**spec, "kind": "ridge"}, subset, feature_names, threads)
        return {"family": "by_entity", "fallback": pooled, "models": models}
    columns = list(range(len(feature_names))) if spec["features"] == "full" else list(range(LOCAL_WIDTH)) + list(range(len(BASE_NAMES), len(feature_names)))
    x = np.asarray([[r["x"][i] for i in columns] for r in rows], dtype=float)
    if kind == "ridge":
        from sklearn.linear_model import Ridge
        from sklearn.preprocessing import StandardScaler
        scaler = StandardScaler().fit(x)
        estimator = Ridge(alpha=spec["alpha"]).fit(scaler.transform(x), y)
        asset = {"family": "ridge", "columns": columns,
                 "coef": estimator.coef_.tolist(), "intercept": float(estimator.intercept_),
                 "mean": scaler.mean_.tolist(), "scale": scaler.scale_.tolist()}
        expected = estimator.predict(scaler.transform(x[:20]))
    elif kind == "lgb":
        import lightgbm as lgb
        estimator = lgb.LGBMRegressor(objective="regression_l1", n_estimators=spec["rounds"],
                   num_leaves=spec["leaves"], min_child_samples=spec["min_leaf"], learning_rate=.04,
                   reg_lambda=15., colsample_bytree=1., subsample=1., verbosity=-1,
                   deterministic=True, force_col_wise=True, random_state=1701, n_jobs=threads)
        estimator.fit(x, y)
        asset = {"family": "lgb", "columns": columns, "output_scale": spec.get("output_scale", 1.),
                 "trees": [_prune_tree(t["tree_structure"]) for t in estimator.booster_.dump_model()["tree_info"]]}
        expected = estimator.predict(x[:20]) * spec.get("output_scale", 1.)
    else:
        raise ValueError("Unknown declared candidate")
    actual = np.asarray(predict_asset(asset, rows[:20]))
    if not np.allclose(expected, actual, rtol=1e-10, atol=1e-10):
        raise ValueError("Portable export parity failed")
    return asset


def run_training(dashboard_path, output, model_output, threads=16):
    output, model_output = Path(output), Path(model_output)
    output.mkdir(parents=True, exist_ok=True)
    if (output / "study-plan.json").exists():
        raise ValueError("Study output already exists; do not overwrite a locked experiment")
    started = datetime.now(timezone.utc).isoformat()
    source_bytes = Path(dashboard_path).read_bytes()
    dashboard = json.loads(source_bytes)
    through = max(b["date"] for e in dashboard.get("market", []) for b in e.get("history", []) if b.get("complete") is not False)
    table = feature_table(dashboard, through=through)
    import hashlib
    import sklearn
    import lightgbm
    plan = {"schema_version": SCHEMA, "started_at": started, "feature_version": FEATURE_VERSION,
            "target": "100*(next_session.close/current_session.close-1)", "unit": "percentage_points",
            "information_cutoff": "completed daily bars, fixed post-close next-session forecast",
            "historical_price_availability_assumption": "Daily OHLC existed after each historical close; this cache is not an arrival log",
            "excluded_information": ["current_constituents", "backfilled_valuation", "news", "future_realized_market_returns"],
            "horizon_trading_sessions": 1, "mae_target": .1, "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "feature_names": table["feature_names"], "entity_ids": table["entity_ids"],
            "candidates": CANDIDATES, "ranking_signals": SIGNALS,
            "protocol": {"development_folds": 3, "outer_days_each": 15, "inner_selection_days": 15,
                         "locked_final_days": 20, "all_splits": "entire label dates, purged by first evaluation label",
                         "final_numeric_choice": "lowest equal-date mean outer-development MAE, separately market/sector",
                         "final_ranking_choice": "highest equal-date development RankIC, >=95% valid ranking dates",
                         "final_evaluation": "one locked retrospective evaluation, not a genuinely new market holdout",
                         "deployment": "refit fixed development-selected configurations on all matured history; separate assets and hashes"},
            "optimization_commitment": "Complete baseline_round and optimization_round_1 before the sole final evaluation; no final-set-driven tuning",
            "optimization_stop_rule": "After >=1 optimization if initial MAE>0.1: continue a group only while its retained best MAE>0.1 and reduction from previous round retained best is strictly >0.1 percentage points; otherwise stop",
            "conditional_optimization_generator": "Round r>=2: full-feature Ridge alpha=10**max(-2,4-r); L1 tree leaves=min(31,2**(r+2)-1), min_leaf=max(10,50//r), rounds=min(300,100+30*r); L1 shallow leaves=3,min_leaf=max(10,80//r),same rounds,output_scale=max(.1,.85-.1*r)",
            "implementation_sha256": {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest() for name in ("daily_forecast.py", "train_daily_forecast.py", "calendar.py")},
            "versions": {"numpy": np.__version__, "sklearn": sklearn.__version__, "lightgbm": lightgbm.__version__},
            "threads": threads}
    save_json(output / "study-plan.json", plan)
    progress = {"stage": "training", "started_at": started, "fit_completed": 0,
                "fit_total": 2 * 3 * 2 * len(CANDIDATES) + 8, "events": [], "groups": {}}
    def note(stage, **values):
        progress.update(stage=stage, updated_at=datetime.now(timezone.utc).isoformat(), **values)
        progress.update(completed_fits=progress["fit_completed"], total_fits=progress["fit_total"], status=stage)
        save_json(output / "progress.json", progress)
        print(json.dumps({"stage": stage, **values}, ensure_ascii=False), flush=True)
    def fit(spec, rows):
        asset = fit_asset(spec, rows, table["feature_names"], threads)
        progress["fit_completed"] += 1
        note("training", candidate=spec["name"], candidate_stage=spec["stage"], training_rows=len(rows))
        return asset
    group_reports, locks, final_rows, selected_specs = {}, {}, {}, {}
    by_name = {s["name"]: s for s in CANDIDATES}
    for data_key, group_key in (("market", "market"), ("sectors", "sector")):
        rows = [r for r in table[data_key] if "label_date" in r]
        dates = sorted({r["label_date"] for r in rows})
        if len(dates) < 125:
            raise ValueError("Insufficient independent dates for the predeclared protocol")
        development_dates, test_dates = dates[:-20], dates[-20:]
        test = [r for r in rows if r["label_date"] in test_dates]
        final_rows[group_key] = test
        outer_predictions = {s["name"]: [] for s in CANDIDATES}
        outer_rows = []
        folds, selected_procedure_rows, selected_procedure_predictions, fold_buffers = [], [], [], []
        for fold in range(3):
            start = len(development_dates) - (3 - fold) * 15
            outer_dates = development_dates[start:start + 15]
            inner_dates = development_dates[start - 15:start]
            inner_train = [r for r in rows if r["label_date"] < inner_dates[0]]
            inner_val = [r for r in rows if r["label_date"] in inner_dates]
            outer_train = [r for r in rows if r["label_date"] < outer_dates[0]]
            outer_val = [r for r in rows if r["label_date"] in outer_dates]
            scores, outer_scores, cached_predictions = {}, {}, {}
            note("training", group=group_key, fold=fold + 1)
            for spec in CANDIDATES:
                inner_asset = fit(spec, inner_train)
                scores[spec["name"]] = evaluate(inner_val, predict_asset(inner_asset, inner_val))["mae_pct"]
                outer_asset = fit(spec, outer_train)
                predictions = predict_asset(outer_asset, outer_val)
                cached_predictions[spec["name"]] = predictions
                outer_predictions[spec["name"]].extend(predictions)
                outer_scores[spec["name"]] = evaluate(outer_val, predictions)
            inner_winner = min(scores, key=scores.get)
            selected_procedure_rows.extend(outer_val)
            selected_procedure_predictions.extend(cached_predictions[inner_winner])
            outer_rows.extend(outer_val)
            folds.append({"fold": fold + 1, "inner_first": inner_dates[0], "inner_last": inner_dates[-1],
                          "outer_first": outer_dates[0], "outer_last": outer_dates[-1],
                          "inner_selected": inner_winner, "inner_mae": scores,
                          "outer_results": outer_scores,
                          "max_inner_train_label": max(r["label_date"] for r in inner_train),
                          "max_outer_train_label": max(r["label_date"] for r in outer_train)})
            fold_buffers.append((inner_train, inner_val, outer_train, outer_val, cached_predictions))
        development = {name: evaluate(outer_rows, predictions) for name, predictions in outer_predictions.items()}
        baseline_best = min((s["name"] for s in CANDIDATES if s["stage"] == "baseline_round"), key=lambda n: development[n]["mae_pct"])
        previous_best = development[baseline_best]["mae_pct"]
        retained_best = min(m["mae_pct"] for m in development.values())
        rounds = [{"round": 0, "stage": "baseline_round", "retained_best_mae_pct": previous_best},
                  {"round": 1, "stage": "optimization_round_1", "previous_best_mae_pct": previous_best,
                   "retained_best_mae_pct": retained_best, "improvement_pct_points": previous_best - retained_best,
                   "continue": continue_optimization(previous_best, retained_best)}]
        round_number = 2
        while rounds[-1]["continue"]:
            specs = optimization_candidates(round_number)
            save_json(output / f"{group_key}-round-{round_number}-plan.json", {"declared_at": datetime.now(timezone.utc).isoformat(), "candidates": specs, "trigger": rounds[-1]})
            progress["fit_total"] += 6 * len(specs)
            for spec in specs:
                by_name[spec["name"]] = spec
                predictions = []
                for fold_index, (inner_train, inner_val, outer_train, outer_val, cached_predictions) in enumerate(fold_buffers):
                    inner_asset = fit(spec, inner_train)
                    folds[fold_index]["inner_mae"][spec["name"]] = evaluate(inner_val, predict_asset(inner_asset, inner_val))["mae_pct"]
                    outer_asset = fit(spec, outer_train)
                    values = predict_asset(outer_asset, outer_val)
                    cached_predictions[spec["name"]] = values
                    folds[fold_index]["outer_results"][spec["name"]] = evaluate(outer_val, values)
                    predictions.extend(values)
                outer_predictions[spec["name"]] = predictions
                development[spec["name"]] = evaluate(outer_rows, predictions)
            previous_best, retained_best = retained_best, min(m["mae_pct"] for m in development.values())
            rounds.append({"round": round_number, "stage": f"optimization_round_{round_number}",
                           "previous_best_mae_pct": previous_best, "retained_best_mae_pct": retained_best,
                           "improvement_pct_points": previous_best - retained_best,
                           "continue": continue_optimization(previous_best, retained_best)})
            round_number += 1
        selected_procedure_rows, selected_procedure_predictions = [], []
        for fold, (_, _, _, outer_val, cached_predictions) in zip(folds, fold_buffers):
            fold["inner_selected"] = min(fold["inner_mae"], key=fold["inner_mae"].get)
            selected_procedure_rows.extend(outer_val)
            selected_procedure_predictions.extend(cached_predictions[fold["inner_selected"]])
        selected = min(development, key=lambda n: development[n]["mae_pct"])
        stage_results = {}
        for stage in dict.fromkeys(by_name[n]["stage"] for n in development):
            names = [n for n in development if by_name[n]["stage"] == stage]
            best = min(names, key=lambda n: development[n]["mae_pct"])
            stage_results[stage] = {"best": best, "mae_pct": development[best]["mae_pct"],
                                    "candidate_mae": {n: development[n]["mae_pct"] for n in names}}
        group_reports[group_key] = {"all_matured_rows": len(rows), "all_label_dates": len(dates),
                                    "development_results": development, "folds": folds,
                                    "nested_inner_selection_procedure": evaluate(selected_procedure_rows, selected_procedure_predictions),
                                    "stages": stage_results, "optimization_rounds": rounds, "selected": selected}
        selected_specs[group_key] = by_name[selected]
        if group_key == "sector":
            rankings = {n: {"daily_rank_ic": m["daily_rank_ic"], "ranking_coverage": m["ranking_coverage"]}
                        for n, m in development.items()}
            for signal in SIGNALS:
                metrics = evaluate(outer_rows, predict_asset(signal, outer_rows))
                rankings[signal["name"]] = {"daily_rank_ic": metrics["daily_rank_ic"], "ranking_coverage": metrics["ranking_coverage"]}
            eligible = [name for name, m in rankings.items() if m["ranking_coverage"] >= .95 and m["daily_rank_ic"] is not None]
            rank_winner = max(eligible, key=lambda n: rankings[n]["daily_rank_ic"])
            group_reports[group_key].update(ranking_development=rankings, selected_ranking=rank_winner)
            selected_specs["sector_ranking"] = by_name.get(rank_winner) or next(s for s in SIGNALS if s["name"] == rank_winner)
        note("development_complete", group=group_key, selected=selected, stages=stage_results)
    # Lock all choices before fitting/evaluating any last-20-day asset.
    choice = {"locked_at": datetime.now(timezone.utc).isoformat(), "selected_candidates": {k: v["name"] for k, v in selected_specs.items()},
              "source_sha256": plan["source_sha256"], "plan_sha256": digest(plan),
              "development_sha256": digest(group_reports), "final_evaluation_read": False}
    save_json(output / "development-results.json", group_reports)
    save_json(output / "selection-lock.json", choice)
    locked_assets, evaluation = {}, {}
    for data_key, key in (("market", "market"), ("sectors", "sector")):
        rows = [r for r in table[data_key] if "label_date" in r]
        test = final_rows[key]
        first_test = min(r["label_date"] for r in test)
        train = [r for r in rows if r["label_date"] < first_test]
        asset = fit(selected_specs[key], train)
        locked_assets[key] = asset
        metrics = evaluate(test, predict_asset(asset, test))
        baseline_names = ["zero", "median", "median_entity", "ridge_core"]
        baseline = min(baseline_names, key=lambda n: group_reports[key]["development_results"][n]["mae_pct"])
        baseline_asset = fit(by_name[baseline], train)
        baseline_metrics = evaluate(test, predict_asset(baseline_asset, test))
        metrics.update(baseline_name=baseline, baseline_mae_pct=baseline_metrics["mae_pct"],
                       target_met=metrics["mae_target_met"],
                       baseline_by_id=baseline_metrics["by_id"], selected_candidate=selected_specs[key]["name"])
        evaluation[key] = metrics
        if key == "sector":
            spec = selected_specs["sector_ranking"]
            ranking_asset = deepcopy(spec) if spec.get("family") == "signal" else fit(spec, train)
            locked_assets["sector_ranking"] = ranking_asset
            ranking_metrics = evaluate(test, predict_asset(ranking_asset, test))
            evaluation["sector_ranking"] = {"selected_candidate": spec["name"], "daily_rank_ic": ranking_metrics["daily_rank_ic"],
                                            "ranking_valid_dates": ranking_metrics["ranking_valid_dates"], "ranking_coverage": ranking_metrics["ranking_coverage"],
                                            "dates": ranking_metrics["dates"], "not_a_return_or_probability": True}
            evaluation["sector_ranking"]["simple_signal_comparisons"] = {
                signal["name"]: {k: v for k, v in evaluate(test, predict_asset(signal, test)).items()
                                  if k in ("daily_rank_ic", "ranking_coverage")} for signal in SIGNALS}
    evaluation.update(protocol="3 rolling development folds with inner selection; all choices frozen before one final 20-day retrospective evaluation",
                      historical_research=True, note="历史研究回溯；日期曾出现在其他项目研究中，不是新到达的真正样本外证明。部署版按固定配置重新拟合，权重另存哈希。",
                      locked_at=choice["locked_at"], final_evaluated_at=datetime.now(timezone.utc).isoformat(),
                      unit="percentage_points", mae_target=.1,
                      target_met_all_objects=all(v["mae_target_met"] for key in ("market", "sector") for v in evaluation[key]["by_id"].values()))
    save_json(output / "locked-evaluation-assets.json", locked_assets)
    save_json(output / "locked-evaluation.json", evaluation)
    # Deployment refitting is permitted after the one final evaluation, using
    # exactly the already locked configuration, without switching winners.
    deployed = {}
    for data_key, key in (("market", "market"), ("sectors", "sector")):
        rows = [r for r in table[data_key] if "label_date" in r]
        deployed[key] = fit(selected_specs[key], rows)
    rank_spec = selected_specs["sector_ranking"]
    deployed["sector_ranking"] = deepcopy(rank_spec) if rank_spec.get("family") == "signal" else fit(rank_spec, [r for r in table["sectors"] if "label_date" in r])
    trained_at = datetime.now(timezone.utc).isoformat()
    bundle = {"schema_version": SCHEMA, "feature_version": FEATURE_VERSION,
              "model_id": "daily-v3-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
              "trained_at": trained_at, "training_cutoff": through, "horizon_trading_sessions": 1,
              "target": "next_close_return_pct", "unit": "percentage_points", "mae_target": .1,
              "entity_ids": table["entity_ids"], "feature_names": table["feature_names"],
              "selected_candidates": choice["selected_candidates"], "models": deployed, "evaluation": evaluation,
              "plan_sha256": digest(plan), "selection_sha256": digest(choice),
              "evaluation_asset_sha256": digest(locked_assets), "deployment_asset_sha256": digest(deployed),
              "promotion_policy": "user-authorized best development-selected model even if MAE target is unmet",
              "training_runtime": {"numpy": np.__version__, "threads": threads, "platform": os.name},
              "historical_price_availability_assumption": plan["historical_price_availability_assumption"]}
    bundle["sha256"] = digest(bundle)
    save_json(model_output / "bundle.json", bundle)
    save_json(output / "bundle.json", bundle)
    summary = {"schema_version": SCHEMA, "status": "completed", "started_at": started, "completed_at": trained_at,
               "selected_candidates": choice["selected_candidates"], "stage_comparison": {k: v["stages"] for k, v in group_reports.items()},
               "optimization_rounds": {k: v["optimization_rounds"] for k, v in group_reports.items()},
               "evaluation": evaluation, "bundle_sha256": bundle["sha256"], "bundle_bytes": (model_output / "bundle.json").stat().st_size,
               "model_output": str((model_output / "bundle.json").resolve()), "plan_sha256": digest(plan),
               "deployment_refitted": True, "old_assets_changed": False, "uploaded": False}
    save_json(output / "summary.json", summary)
    note("completed", summary=summary, fit_total=progress["fit_completed"])
    return summary


def main(argv=None):
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dashboard", default=str(root / "web/data/dashboard.json"))
    parser.add_argument("--output", default=str(root.parent / "research/daily-v3"))
    parser.add_argument("--model-output", default=str(root / "data/daily_forecast/models"))
    parser.add_argument("--threads", type=int, default=16)
    args = parser.parse_args(argv)
    if os.environ.get("RADAR_ALLOW_TRAINING", "true").lower() in ("false", "0", "no", "off"):
        raise SystemExit("Local training explicitly disabled")
    run_training(args.dashboard, args.output, args.model_output, max(1, min(args.threads, 16)))


if __name__ == "__main__":
    main()
