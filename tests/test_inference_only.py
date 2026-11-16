"""Production inference must never silently fit models when training is off."""
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np

from pipeline import model
from pipeline.equity_model import FEATURE_NAMES, VERSION, update_equity_predictions
from tests.test_equity_model import dashboard, make_history, write_history
from tests.test_model import dashboard as legacy_dashboard


NOW = datetime.fromisoformat("2026-11-16T08:00:00+08:00")


class InferenceOnlyTests(unittest.TestCase):
    def setUp(self):
        # Tests explicitly control the process override and leave the parent env unchanged.
        self.env = patch.dict(os.environ, {"RADAR_ALLOW_TRAINING": "true"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_legacy_disabled_config_and_environment_never_fit(self):
        for config, environment in (({"training_enabled": False}, "true"), ({}, "false"), ({}, "0"), ({}, "no")):
            with self.subTest(config=config, environment=environment), \
                    patch.dict(os.environ, {"RADAR_ALLOW_TRAINING": environment}), \
                    patch("pipeline.model._fit", side_effect=AssertionError("must not fit")) as fit:
                result = model.update_predictions(legacy_dashboard(), config=config, now=NOW)
                fit.assert_not_called()
                self.assertTrue(result["models"]["inference_only"])
                self.assertIsNone(result["predictions"][0]["predicted_return_pct"])
                self.assertIn("云端只推断，等待本机训练模型", result["predictions"][0]["reason"])

    def test_legacy_only_identical_input_cache_is_reused(self):
        data = legacy_dashboard()
        initial = model.update_predictions(data, config={"enable_hgb": False}, now=NOW)
        with patch("pipeline.model._fit", side_effect=AssertionError("must not fit")) as fit:
            same = model.update_predictions(data, initial,
                config={"training_enabled": False, "enable_hgb": False}, now=NOW)
            self.assertTrue(same["predictions"][0]["cache_hit"])
            self.assertEqual(same["predictions"][0]["predicted_return_pct"], initial["predictions"][0]["predicted_return_pct"])
            changed = deepcopy(data)
            changed["sectors"][0]["history"][-1]["close"] *= 1.0001
            different = model.update_predictions(changed, initial,
                config={"training_enabled": False, "enable_hgb": False}, now=NOW)
            self.assertIsNone(different["predictions"][0]["predicted_return_pct"])
            self.assertIn("没有完全匹配", different["predictions"][0]["reason"])
            fit.assert_not_called()

    def test_shared_disabled_without_artifact_does_not_train_or_invent_prediction(self):
        history = make_history(count=1)
        with tempfile.TemporaryDirectory() as root:
            archive, models = Path(root) / "daily", Path(root) / "models"
            write_history(archive, history)
            for config, environment in (({"training_enabled": False}, "true"),
                    ({"equity": {"enable_training": False}}, "true"), ({}, "false")):
                with self.subTest(config=config, environment=environment), \
                        patch.dict(os.environ, {"RADAR_ALLOW_TRAINING": environment}), \
                        patch("pipeline.equity_model.train_shared_baseline", side_effect=AssertionError("must not train")) as fit:
                    result = update_equity_predictions(dashboard(history), archive, models, NOW, config)
                    fit.assert_not_called()
                    forecast = result["equity_forecast"]
                    self.assertTrue(forecast["training"]["inference_only"])
                    self.assertEqual(forecast["coverage"]["predicted_count"], 0)
                    self.assertIn("云端只推断，等待本机训练模型", forecast["stocks"][0]["note"])
            self.assertFalse((models / "shared_baseline.json").exists())

    def test_shared_existing_portable_model_predicts_without_retraining_even_if_old(self):
        history = make_history(count=1)
        with tempfile.TemporaryDirectory() as root:
            archive, models = Path(root) / "daily", Path(root) / "models"
            write_history(archive, history)
            models.mkdir()
            width = len(FEATURE_NAMES)
            artifact = {"version": VERSION, "trained_at": "2026-10-01T08:00:00+08:00",
                "label_cutoff": "2026-09-30", "report": {"status": "evaluated", "promoted": True},
                "targets": {"next_open": {"model": "ridge", "parameters": {
                    "mean": [0.] * width, "scale": [1.] * width, "coef": [0.] * width, "intercept": .25}}}}
            path = models / "shared_baseline.json"
            path.write_text(json.dumps(artifact))
            before = path.read_bytes()
            with patch("pipeline.equity_model.train_shared_baseline", side_effect=AssertionError("must not train")) as fit, \
                    patch("pipeline.equity_model._portable_fit", side_effect=AssertionError("must not fit")):
                result = update_equity_predictions(dashboard(history), archive, models, NOW,
                    {"training_enabled": False, "equity": {"retrain_days": 1, "enable_training": True}})
                fit.assert_not_called()
            forecast = result["equity_forecast"]
            self.assertEqual(forecast["coverage"]["predicted_count"], 1)
            self.assertEqual(forecast["stocks"][0]["predicted_return_pct"], .25)
            self.assertTrue(forecast["training"]["inference_only"])
            self.assertEqual(forecast["training"]["status"], "evaluated")
            self.assertEqual(before, path.read_bytes())

    def test_uploaded_model_predicts_each_stock_before_weighted_aggregation(self):
        history = make_history(count=2)
        with tempfile.TemporaryDirectory() as root:
            archive, models = Path(root) / 'daily', Path(root) / 'models'
            write_history(archive, history)
            models.mkdir()
            (models / 'deployed-model.json').write_text('{}')
            bundle = {'kind': 'ensemble', 'model_id': 'ensemble-test', 'window': 60,
                      'trained_at': '2026-11-10T08:00:00+08:00', 'label_cutoff': '2026-11-09'}
            with patch('pipeline.deployed_equity.load_deployed_model', return_value=bundle), \
                    patch('pipeline.deployed_equity.predict_deployed', return_value=np.array([[.1, .3], [.2, .4]])) as infer, \
                    patch('pipeline.equity_model.train_shared_baseline', side_effect=AssertionError('must not fit')):
                result = update_equity_predictions(dashboard(history), archive, models, NOW, {'training_enabled': False})
            forecast = result['equity_forecast']
            self.assertEqual([r['predicted_return_pct'] for r in forecast['stocks']], [.1, .2])
            self.assertAlmostEqual(forecast['market']['predicted_return_pct'], 1 / 6)
            self.assertEqual(forecast['training']['deployed_model']['status'], 'active')
            self.assertEqual(infer.call_args.args[1].shape, (2, 60, 8))


if __name__ == "__main__":
    unittest.main()
