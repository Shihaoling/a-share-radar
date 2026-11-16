from copy import deepcopy
import unittest
import numpy as np

from pipeline.daily_forecast import BASE_NAMES, predict_asset
from pipeline.optimize_daily_forecast import adjust_asset, best_ranking, fit_candidate, relevance_labels, second_round, training_subset


class OptimizationTest(unittest.TestCase):
    def test_recent_window_uses_label_dates_only(self):
        rows = [{"label_date": f"2026-01-{d:02d}", "id": identity} for d in range(5, 10) for identity in ("a", "b")]
        selected = training_subset(rows, 2)
        self.assertEqual({r["label_date"] for r in selected}, {"2026-01-08", "2026-01-09"})
        self.assertEqual(len(selected), 4)

    def test_affine_adjustment_for_portable_families(self):
        row = {"id": "a", "x": [2.]}
        assets = [
            {"family": "constant", "value": 2., "by_id": {"a": 3.}},
            {"family": "ridge", "columns": [0], "coef": [2.], "intercept": 1., "mean": [0.], "scale": [1.]},
            {"family": "lgb", "columns": [0], "trees": [{"leaf_value": 4.}], "output_scale": .35},
            {"family": "signal", "column": 0, "sign": -1.},
        ]
        for asset in assets:
            original = deepcopy(asset)
            expected = predict_asset(asset, [row])[0] * .25 + .4
            adjusted = adjust_asset(asset, .25, .4)
            self.assertAlmostEqual(predict_asset(adjusted, [row])[0], expected)
            self.assertEqual(asset, original)

    def test_two_round_generation_retains_seed_configuration(self):
        seed = {"name": "seed", "kind": "ridge", "features": "full", "alpha": 100., "recent_days": 60}
        candidates = second_round(seed)
        self.assertEqual(len(candidates), 7)
        self.assertTrue(all(c["recent_days"] == 60 and c["alpha"] == 100. for c in candidates))
        self.assertEqual(seed["name"], "seed")

    def test_ranking_selection_ignores_missing_coverage(self):
        metrics = {"a": {"ranking_coverage": 1., "daily_rank_ic": .1}, "b": {"ranking_coverage": .2, "daily_rank_ic": .9}}
        self.assertEqual(best_ranking(metrics, {"a": {}, "b": {}}), "a")

    def test_l1_quantile_exports_without_executable_weights(self):
        names = BASE_NAMES + ["identity_a"]
        rng = np.random.default_rng(5)
        rows = [{"id": "a", "label_date": f"2026-{1+i//28:02d}-{1+i%28:02d}", "x": x.tolist(), "y": float(.4*x[0]+.1*x[1])}
                for i,x in enumerate(rng.normal(size=(90,len(names))))]
        asset = fit_candidate({"kind": "quantile", "features": "full", "alpha": .001}, rows, names, threads=1)
        self.assertEqual(asset["family"], "ridge")
        self.assertLess(np.mean(np.abs(np.asarray(predict_asset(asset, rows))-[r["y"] for r in rows])), .01)

    def test_winsorization_does_not_change_true_labels(self):
        names = BASE_NAMES + ["identity_a"]
        rows = [{"id":"a","label_date":"2026-01-05","x":[0.]*len(names),"y":v} for v in [0.,1.,2.,100.]]
        before = deepcopy(rows)
        fit_candidate({"kind":"median","features":"core","winsor":[.1,.9]}, rows, names, threads=1)
        self.assertEqual(rows,before)

    def test_feature_mask_exports_original_feature_indices(self):
        names = BASE_NAMES + ["identity_a"]
        rng = np.random.default_rng(43)
        rows = [{"id": "a", "label_date": "2026-01-05", "x": x.tolist(), "y": float(.4*x[2]-.2*x[0])}
                for x in rng.normal(size=(100,len(names)))]
        asset = fit_candidate({"kind": "ridge", "features": "full", "alpha": .01, "feature_columns": [2,0]}, rows, names, threads=1)
        self.assertEqual(asset["columns"], [2,0])
        self.assertLess(np.mean(np.abs(np.asarray(predict_asset(asset, rows))-[r["y"] for r in rows])), .01)

    def test_ranker_relevance_keeps_return_ties(self):
        self.assertEqual(relevance_labels([3.,1.,3.,2.]), [2,0,2,1])

    def test_ranker_portable_export(self):
        try:
            import lightgbm
        except ImportError:
            self.skipTest("Optional local LightGBM training dependency")
        names = BASE_NAMES + ["identity_a"]
        rng = np.random.default_rng(7)
        rows = [{"id": str(i%10), "label_date": f"2026-01-{5+i//10:02d}", "x": x.tolist(), "y": float(i%4)}
                for i,x in enumerate(rng.normal(size=(60,len(names))))]
        asset = fit_candidate({"kind":"ranker","features":"full","leaves":3,"min_leaf":5,"rounds":3},rows,names,threads=1)
        self.assertEqual(asset["family"],"lgb")
        self.assertTrue(np.isfinite(predict_asset(asset, rows)).all())


if __name__ == "__main__":
    unittest.main()
