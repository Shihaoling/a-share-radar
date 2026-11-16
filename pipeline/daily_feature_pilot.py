"""Small predeclared historical feature pilot, never a deployment trainer.

python -m pipeline.daily_feature_pilot --output ../research/daily-v4
No old trained assets are loaded. Narrow fits map columns back to original x.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import time
from .daily_forecast import BASE_NAMES, FEATURE_VERSION, digest, evaluate, feature_table, predict_asset
from .train_daily_forecast import fit_asset, save_json

IDS = ["sh000001", "sz399001", "sz399006", "sh000300", "sh000688", "sh000905",
       "801780.SI", "801080.SI", "801050.SI", "801150.SI", "801160.SI"]
SPECS = [{"name":"ridge_100", "kind":"ridge", "features":"full", "alpha":100.},
         {"name":"l1_tree_3", "kind":"lgb", "features":"full", "leaves":3, "min_leaf":40, "rounds":60}]


def feature_groups(names):
    definitions = {
        "bar_core": ["return_1", "overnight_1", "intraday_1", "range_1", "close_location"],
        "multi_period_5_10_20": [n for n in names if n.startswith(("momentum_", "return_mean_", "volatility_", "up_fraction_"))],
        "efficiency_ratio": [n for n in names if n.startswith("trend_efficiency_")],
        "volume_amount": [n for n in names if n.startswith(("volume_", "amount_"))],
        "market_relative": [n for n in names if n.startswith(("market_", "relative_"))],
        "sector_dispersion": [n for n in names if n.startswith("sector_")]}
    grouped = [n for group in definitions.values() for n in group]
    assert len(grouped) == len(set(grouped)) and set(grouped) == set(BASE_NAMES)
    return {key:{"names":group, "columns":[names.index(n) for n in group]} for key,group in definitions.items()}


def fit_mask(spec, rows, names, columns, threads):
    narrow = [{**r, "x":[r["x"][i] for i in columns]} for r in rows]
    asset = fit_asset(spec, narrow, [names[i] for i in columns], threads)
    before = predict_asset(asset, narrow[:20])
    if "columns" in asset:
        asset["columns"] = [columns[i] for i in asset["columns"]]
    after = predict_asset(asset, rows[:20])
    assert all(abs(a-b)<1e-10 for a,b in zip(before,after)), "Portable column mapping parity failed"
    return asset


def rotate_date_blocks(rows, dates, columns, offset):
    """Diagnostic corruption only: rotate intact 5-day blocks synchronously.

    Identical date mapping applies to every entity, retaining same-identity donors.
    These corrupted validation rows are never fitted or issued as predictions.
    """
    position = {day:i for i,day in enumerate(dates)}
    lookup = {(r["id"],r["label_date"]):r for r in rows}
    output = []
    for row in rows:
        donor = lookup[(row["id"],dates[(position[row["label_date"]]+offset)%len(dates)])]
        x = list(row["x"])
        for i in columns:
            x[i] = donor["x"][i]
        output.append({**row,"x":x})
    return output


def run_pilot(dashboard_path, output, threads=4):
    if os.environ.get("RADAR_ALLOW_TRAINING", "true").strip().lower() in ("false", "0", "no", "off"):
        raise RuntimeError("Local training explicitly disabled")
    started = time.perf_counter()
    output = Path(output)
    plan_path, result_path = output/"feature-pilot-plan.json", output/"feature-pilot.json"
    if plan_path.exists() or result_path.exists():
        raise ValueError("Pilot already declared; never overwrite the locked plan or results")
    source = Path(dashboard_path).read_bytes()
    dashboard = json.loads(source)
    table = feature_table(dashboard, entity_ids=IDS)
    full_ids = sorted({e["id"] for group in ("market", "sectors") for e in dashboard[group]})
    v4_names = BASE_NAMES + ["identity_" + identity for identity in full_ids]
    names, groups = table["feature_names"], feature_groups(table["feature_names"])
    batches = {key:[r for r in table[key] if r.get("label_date","").startswith("2026-")] for key in ("market","sectors")}
    id_dates = {identity:{r["label_date"] for batch in batches.values() for r in batch if r["id"]==identity} for identity in IDS}
    dates = sorted(set.intersection(*(id_dates[identity] for identity in IDS)))
    if len(dates)<76:
        raise ValueError("Need 60 common training dates, one purge date and 15 validation dates")
    training_dates, purge_dates, validation_dates = dates[:60], dates[60:61], dates[61:76]
    previous = json.loads((output.parent/"daily-v3/development-results.json").read_text("utf-8"))
    old_first = min(v["first_label_date"] for report in previous.values() for v in report["development_results"].values())
    assert validation_dates[-1]<old_first, "Pilot must predate previous development/final evaluation windows"
    subsets = {}
    for key,rows in batches.items():
        tr = [r for r in rows if r["label_date"] in training_dates]
        va = [r for r in rows if r["label_date"] in validation_dates]
        count = 6 if key=="market" else 5
        assert len(tr)==60*count and len(va)==15*count
        assert max(r["label_date"] for r in tr)<min(r["feature_date"] for r in va)
        subsets[key] = tr,va
    masks = {"full":list(range(len(names)))}
    masks.update({"without_"+key:[i for i in range(len(names)) if i not in g["columns"]] for key,g in groups.items()})
    limitations = ["Historical small pilot: 60 training dates and 15 validation dates do not prove independent improvement",
                  "Early dates may have been training data in prior research; this pilot remains exploratory",
                  "Correlated groups, only five representative sectors, and one short market regime limit importance interpretation",
                  "Date-block permutation is deliberately corrupted validation input, never a causal forecast",
                  "Historical daily prices assumed available after close; current cache is not an arrival log"]
    plan = {"schema_version":"daily-feature-pilot-v1", "declared_at":datetime.now(timezone.utc).isoformat(),
            "status":"declared_before_any_pilot_fit", "source_sha256":hashlib.sha256(source).hexdigest(),
            "feature_version":FEATURE_VERSION, "feature_names":names, "v4_feature_names":v4_names, "groups":groups, "masks":masks,
            "entity_ids":sorted(IDS), "sector_names":["银行","电子","有色金属","医药生物","公用事业"],
            "training_label_dates":training_dates, "purged_label_dates":purge_dates, "validation_label_dates":validation_dates,
            "previous_development_first_label":old_first, "model_grid":SPECS, "baselines":["zero","training_median"],
            "total_fits":32, "unit":"percentage_points", "metric":"equal-date mean MAE; separate task and per-object reports",
            "date_block_permutation":{"block_sessions":5,"synchronous_offsets":[5,10],"same_identity_donor":True,
                                      "all_entities_share_date_mapping":True,"fit_permuted_data":False,"causal_forecast":False},
            "context":"Train on five sectors; same-day sector context may use the existing 31 official sector indices, with no future labels",
            "decision_rule":"Always retain full. Recommend at most two group removals if mean MAE delta across four task/model comparisons <= -0.005pp and >=3/4 comparisons non-worsening; order by mean delta. Otherwise recommend full only.",
            "limitations":limitations,"threads":threads,
            "implementation_sha256":{name:hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest()
                                     for name in ("daily_feature_pilot.py","daily_forecast.py","train_daily_forecast.py","calendar.py")}}
    save_json(plan_path,plan)
    print(json.dumps({"stage":"pilot_plan_frozen","plan_sha256":digest(plan),"train":[training_dates[0],training_dates[-1]],
                      "validation":[validation_dates[0],validation_dates[-1]],"fits":32}),flush=True)
    reports,completed = {},0
    for key,(training,validation) in subsets.items():
        record = {"training_rows":len(training),"validation_rows":len(validation),"baselines":{},"models":{}}
        for kind in ("zero","median"):
            asset = fit_asset({"name":kind,"kind":kind,"features":"full"},training,names,threads)
            record["baselines"][kind] = evaluate(validation,predict_asset(asset,validation)); completed+=1
        for spec in SPECS:
            report = {"spec":spec,"ablations":{},"date_block_permutation":{}}
            assets = {}
            for mask,columns in masks.items():
                asset = fit_mask(spec,training,names,columns,threads); assets[mask]=asset
                score = evaluate(validation,predict_asset(asset,validation))
                report["ablations"][mask] = {"columns":columns,"asset_sha256":digest(asset),"metrics":score};completed+=1
            full_mae = report["ablations"]["full"]["metrics"]["mae_pct"]
            for row in report["ablations"].values():
                row["mae_delta_from_full_pct_points"] = row["metrics"]["mae_pct"]-full_mae
            for family,definition in groups.items():
                rotations=[]
                for offset in (5,10):
                    changed=rotate_date_blocks(validation,validation_dates,definition["columns"],offset)
                    mae=evaluate(changed,predict_asset(assets["full"],changed))["mae_pct"]
                    rotations.append({"offset_sessions":offset,"mae_pct":mae,"mae_delta_pct_points":mae-full_mae})
                report["date_block_permutation"][family]={"rotations":rotations,"mean_mae_increase_pct_points":statistics.fmean(r["mae_delta_pct_points"] for r in rotations)}
            record["models"][spec["name"]]=report
            print(json.dumps({"stage":"pilot_fitting","task":key,"model":spec["name"],"fits_completed":completed,"full_mae_pct":full_mae}),flush=True)
        reports[key]=record
    comparisons=[]
    for group in groups:
        mask="without_"+group
        deltas=[r["models"][spec["name"]]["ablations"][mask]["mae_delta_from_full_pct_points"] for r in reports.values() for spec in SPECS]
        mean,nonworse=statistics.fmean(deltas),sum(d<=1e-12 for d in deltas)
        comparisons.append({"mask":mask,"mean_mae_delta_pct_points":mean,"non_worsening_comparisons":nonworse,"comparisons":deltas,"eligible":mean<=-.005 and nonworse>=3})
    recommended=["full"]+[r["mask"] for r in sorted((x for x in comparisons if x["eligible"]),key=lambda x:x["mean_mae_delta_pct_points"])[:2]]
    result={"schema_version":plan["schema_version"],"status":"completed","completed_at":datetime.now(timezone.utc).isoformat(),
            "plan_sha256":digest(plan),"source_sha256":plan["source_sha256"],"fits_completed":completed,"elapsed_seconds":time.perf_counter()-started,
            "data_ranges":{"training":[training_dates[0],training_dates[-1]],"validation":[validation_dates[0],validation_dates[-1]],"old_development_first":old_first},
            "reports":reports,"mask_comparisons":sorted(comparisons,key=lambda x:x["mean_mae_delta_pct_points"]),
            "recommended_v4_masks":[{"name":name,"pilot_columns":masks[name],"pilot_kept_feature_names":[names[i] for i in masks[name]],
                "feature_columns":[i for i,feature in enumerate(v4_names) if name=="full" or feature not in groups[name[len("without_"):]]["names"]],
                "kept_feature_names":[feature for feature in v4_names if name=="full" or feature not in groups[name[len("without_"):]]["names"]],
                "rationale":"Mandatory original-feature control" if name=="full" else "Early pilot only: average MAE improved by >=0.005pp and at least 3/4 task/model comparisons non-worsening; confirm in v4",
                "pilot_metric":next((row for row in comparisons if row["mask"]==name),None)} for name in recommended],
            "recommendation":"Narrow exploratory candidates only; full remains mandatory, no group permanently removed",
            "production_changed":False,"previous_models_loaded":False,"limitations":limitations}
    assert completed==32
    save_json(result_path,result)
    print(json.dumps({"stage":"pilot_completed","fits":completed,"recommended_masks":recommended,"elapsed_seconds":result["elapsed_seconds"]}),flush=True)
    return result


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument("--dashboard",default="web/data/dashboard.json")
    parser.add_argument("--output",default="../research/daily-v4")
    parser.add_argument("--threads",type=int,default=4)
    args=parser.parse_args(argv)
    if os.environ.get("RADAR_ALLOW_TRAINING", "true").strip().lower() in ("false", "0", "no", "off"):
        raise SystemExit("Local training explicitly disabled")
    run_pilot(args.dashboard,args.output,max(1,min(16,args.threads)))


if __name__=="__main__":
    main()
