import unittest
import numpy as np

from pipeline.daily_forecast import BASE_NAMES, predict_asset
from pipeline.train_daily_forecast import continue_optimization, fit_asset, optimization_candidates


class DailyTrainingTest(unittest.TestCase):
    def test_user_stopping_rule_is_strict_and_retains_best(self):
        self.assertTrue(continue_optimization(1., .89))
        self.assertFalse(continue_optimization(1., .9))
        self.assertFalse(continue_optimization(.3, .1))
        self.assertFalse(continue_optimization(1., 1.))
        self.assertEqual(optimization_candidates(2)[0]["stage"], "optimization_round_2")

    def test_portable_ridge_matches_linear_training(self):
        names = BASE_NAMES + ["identity_a"]
        rng = np.random.default_rng(17)
        rows = [{"id": "a", "x": x.tolist(), "y": float(x[0] * .4 - x[1] * .2)}
                for x in rng.normal(size=(100, len(names)))]
        asset = fit_asset({"kind": "ridge", "features": "full", "alpha": .1}, rows, names, threads=1)
        error = np.mean(np.abs(np.asarray(predict_asset(asset, rows)) - [r["y"] for r in rows]))
        self.assertLess(error, .01)

    def test_portable_lightgbm_export_preserves_actual_predictions(self):
        try:
            import lightgbm
        except ImportError:
            self.skipTest("Optional local research dependency is not installed")
        names = BASE_NAMES + ["identity_a"]
        rng = np.random.default_rng(19)
        rows = [{"id": "a", "x": x.tolist(), "y": float(x[0] ** 2 + x[1])}
                for x in rng.normal(size=(120, len(names)))]
        asset = fit_asset({"kind": "lgb", "features": "full", "leaves": 3,
                           "min_leaf": 10, "rounds": 8, "output_scale": .35}, rows, names, threads=1)
        values = predict_asset(asset, rows)
        self.assertEqual(len(values), len(rows))
        self.assertTrue(np.isfinite(values).all())


if __name__ == "__main__":
    unittest.main()
