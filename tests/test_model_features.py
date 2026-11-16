"""Point-in-time feature, training-cache and observed-opening contracts (no network)."""
from copy import deepcopy
from datetime import datetime
import json
import math
import unittest
from unittest.mock import patch

from pipeline import model
from test_model import dashboard, history


NOW = datetime.fromisoformat("2026-11-16T08:00:00+08:00")


def feature(vector, name, names=model.FEATURE_NAMES):
    index = names.index(name)
    return vector[index], vector[len(names) + index]


class PointInTimeFeatureTests(unittest.TestCase):
    def test_current_valuation_never_backfills_training_history(self):
        rows = history()
        entity = {"vendor_pe": 25, "vendor_pe_ttm": 30, "vendor_pb": 5, "market_cap": 1e12,
                  "feature_history": [{"date": rows[-1]["date"], "vendor_pe": 25, "vendor_pb": 5,
                                       "as_of": rows[-1]["date"] + "T15:00:00+08:00"}]}
        values = model._features(rows, len(rows) - 1, entity, NOW)
        self.assertEqual(feature(values, "log_vendor_pe"), (0, 1))
        entity["feature_history"][0]["available_at"] = "2026-11-16T07:00:00+08:00"
        # It is known to this live forecast, but was not known on the feature day.
        current = model._features(rows, len(rows) - 1, entity, NOW)
        historical = model._features(rows, len(rows) - 1, entity)
        self.assertAlmostEqual(feature(current, "log_vendor_pe")[0], math.log(25))
        self.assertEqual(feature(historical, "log_vendor_pe"), (0, 1))

    def test_neutral_and_unknown_sentiment_are_different_and_future_is_excluded(self):
        rows = history()
        base = {"date": "2026-11-13", "available_at": "2026-11-13T18:00:00+08:00",
                "score": 0, "article_count": 4, "confidence": .7}
        neutral = model._features(rows, len(rows) - 1, {"sentiment_history": [base]})
        unknown = model._features(rows, len(rows) - 1, {"sentiment_history": [{**base, "score": None}]})
        future = model._features(rows, len(rows) - 1, {"sentiment_history": [{**base, "available_at": "2026-11-16T10:00:00+08:00"}]})
        self.assertEqual(feature(neutral, "sentiment_score"), (0, 0))
        self.assertEqual(feature(unknown, "sentiment_score"), (0, 1))
        self.assertEqual(feature(future, "sentiment_score"), (0, 1))

    def test_newly_collected_sentiment_is_visible_but_not_activated_without_history(self):
        data = dashboard()
        data["sectors"][0]["sentiment_history"] = [{"date": "2026-11-16", "available_at": "2026-11-16T07:00:00+08:00",
                                                    "score": .9, "article_count": 5, "confidence": .8}]
        result = model.update_predictions(data, now=NOW)
        row = result["predictions"][0]
        coverage = next(r for r in row["feature_metadata"]["coverage"] if r["name"] == "sentiment_score")
        self.assertTrue(coverage["latest_available"])
        self.assertEqual(coverage["present_count"], 0)
        self.assertFalse(coverage["active"])
        self.assertNotEqual(row["news_features"], "active")

    def test_volume_ratios_respect_units_and_unknown_provider_changes(self):
        rows = history()
        for row in rows:
            row.update(volume=10000, volume_unit="share", amount=100000, amount_unit="CNY")
        rows[-1].update(volume=200, volume_unit="lot", amount=20, amount_unit="CNY_10000")
        values = model._features(rows, len(rows) - 1)
        self.assertEqual(feature(values, "volume_ratio_20d"), (2, 0))
        self.assertEqual(feature(values, "amount_ratio_20d"), (2, 0))
        rows[-1].update(volume_unit="unknown", provider="different")
        self.assertEqual(feature(model._features(rows, len(rows) - 1), "volume_ratio_20d"), (0, 1))

    def test_alternate_volume_requires_matching_price_and_known_unit(self):
        rows = history()
        other = [{**row, "volume": 100, "volume_unit": "lot"} for row in rows]
        entity = {"source_history": {"stock_api": other}}
        filled = model._with_source_flows(rows, entity, NOW)
        self.assertEqual(filled[-1]["volume"], 10000)
        self.assertNotIn("volume", rows[-1])
        other[-1]["close"] *= 1.02
        self.assertNotIn("volume", model._with_source_flows(rows, entity, NOW)[-1])

    def test_late_alternate_volume_revision_keeps_its_availability(self):
        rows = history()
        other = [{**row, "volume": 100, "volume_unit": "lot", "available_at": "2026-11-16T07:00:00+08:00"} for row in rows]
        filled = model._with_source_flows(rows, {"source_history": {"stock_api": other}}, NOW)
        historical = model._features(filled, len(filled) - 1)
        current = model._features(filled, len(filled) - 1, cutoff=NOW)
        self.assertEqual(feature(historical, "volume_ratio_20d"), (0, 1))
        self.assertEqual(feature(current, "volume_ratio_20d"), (1, 0))

    def test_missing_and_nonfinite_auxiliary_values_remain_strict_json(self):
        data = dashboard()
        data["sectors"][0]["feature_history"] = [{"date": "2026-11-13", "available_at": "2026-11-13T18:00:00+08:00",
                                                  "vendor_pe": float("nan"), "vendor_pb": float("inf")}]
        result = model.update_predictions(data, now=NOW)
        json.dumps(result, allow_nan=False)
        values = result["models"]["by_target"]["chip"]["cached_outcome"]["feature_values"]
        self.assertEqual(feature(values, "log_vendor_pe"), (0, 1))

    def test_late_price_revision_and_missing_trading_day_do_not_form_old_features(self):
        rows = history()
        for field in ("available_at", "price_available_at"):
            rows[-1][field] = "2026-11-16T07:00:00+08:00"
            self.assertIsNone(model._features(rows, len(rows) - 1))
            self.assertIsNotNone(model._features(rows, len(rows) - 1, cutoff=NOW))
            rows[-1].pop(field)
        del rows[-4]
        self.assertIsNone(model._features(rows, len(rows) - 1))


class TrainingCacheTests(unittest.TestCase):
    def test_unchanged_input_avoids_fit_and_new_bar_value_invalidates_cache(self):
        data = dashboard()
        first = model.update_predictions(data, now=NOW)
        with patch("pipeline.model._fit", side_effect=AssertionError("unchanged inputs must not refit")):
            second = model.update_predictions(data, first, now=NOW)
        self.assertTrue(second["models"]["by_target"]["chip"]["cache_hit"])
        self.assertEqual(second["models"]["last_trained_at"], first["models"]["last_trained_at"])
        data["sectors"][0]["history"][-1]["close"] *= 1.0001
        with patch("pipeline.model._fit", wraps=model._fit) as fit:
            third = model.update_predictions(data, second, now=NOW)
        self.assertGreater(fit.call_count, 0)
        self.assertFalse(third["models"]["by_target"]["chip"]["cache_hit"])

    def test_tree_candidate_requires_sufficient_initial_training_window(self):
        data = dashboard()
        small = model.update_predictions(data, config={"hgb_min_train": 500}, now=NOW)
        self.assertNotIn("hgb", small["predictions"][0]["validation"]["scores"])
        enough = model.update_predictions(data, config={"hgb_min_train": 60}, now=NOW)
        row = enough["predictions"][0]
        self.assertIn("hgb", row["validation"]["scores"])
        self.assertLess(row["validation"]["selection_start"], row["validation"]["confirmation_start"])
        self.assertLess(row["validation"]["confirmation_start"], row["target_date"])
        self.assertNotIn("mlp", row["validation"]["scores"])

    def test_final_refit_failure_is_confined_to_that_prediction(self):
        samples = [{"x": [float(i), 0], "y": float(i), "feature_date": "2026-01-05", "label_date": "2026-01-06"}
                   for i in range(70)]
        calls, original = [], model._fit
        def fail_final(name, x, y):
            calls.append(name)
            if len(calls) == 3:
                raise RuntimeError("simulated final fit failure")
            return original(name, x, y)
        with patch("pipeline.model._fit", side_effect=fail_final):
            outcome, error = model._train_samples(samples, [71., 0], {**model.DEFAULTS, "min_train": 20, "holdout_size": 10}, {}, ("signal",))
        self.assertIsNone(outcome)
        self.assertIn("最终训练失败", error["reason"])

    def test_zero_return_champion_does_not_claim_to_use_candidate_features(self):
        samples = [{"x": [float(i), 0], "y": 0., "feature_date": "2026-01-05", "label_date": "2026-01-06"}
                   for i in range(70)]
        outcome, error = model._train_samples(samples, [71., 0], {**model.DEFAULTS, "min_train": 20, "holdout_size": 10}, {}, ("signal",))
        self.assertIsNone(error)
        self.assertEqual(outcome["champion"], "zero_return")
        self.assertEqual(outcome["feature_metadata"]["active_names"], ["signal"])
        self.assertEqual(outcome["feature_metadata"]["used_names"], [])


class ActualOpenTests(unittest.TestCase):
    def setUp(self):
        self.data = dashboard()
        self.first = model.update_predictions(self.data, now=NOW)
        self.quote = {"open": 1020, "price": 1030, "quote_timestamp": "2026-11-16T09:30:00+08:00",
                      "timestamp_quality": "source_reported"}

    def test_observed_live_open_settles_without_creating_daily_close(self):
        self.data["sectors"][0]["live_quote"] = self.quote
        old = deepcopy(self.data)
        out = model.update_predictions(self.data, self.first, now=datetime.fromisoformat("2026-11-16T09:31:00+08:00"))
        settled = next(r for r in out["ledger"] if r["id"] == self.first["ledger"][0]["id"])
        self.assertEqual(settled["actual_price"], 1020)
        self.assertEqual(settled["actual_source"], "source_reported_live_open")
        self.assertEqual(self.data, old)
        self.assertEqual(self.data["sectors"][0]["history"][-1]["date"], "2026-11-13")

    def test_early_future_or_retrieval_only_open_is_not_an_actual(self):
        for overrides in ({"quote_timestamp": "2026-11-16T09:29:00+08:00"},
                          {"quote_timestamp": "2026-11-16T09:32:00+08:00"},
                          {"timestamp_quality": "retrieved_only"},
                          {"fetched_at": "2026-11-16T09:32:00+08:00"},
                          {"quote_timestamp": "2026-11-13T10:00:00+08:00"}):
            with self.subTest(overrides=overrides):
                self.data["sectors"][0]["live_quote"] = {**self.quote, **overrides}
                out = model.update_predictions(self.data, self.first, now=datetime.fromisoformat("2026-11-16T09:31:00+08:00"))
                original = next(r for r in out["ledger"] if r["id"] == self.first["ledger"][0]["id"])
                self.assertNotEqual(original["status"], "settled")


class IntradayTrainingTests(unittest.TestCase):
    def test_only_real_same_time_snapshots_enter_daily_independent_samples(self):
        data = dashboard()
        rows = data["sectors"][0]["history"]
        points = []
        for row in rows:
            stamp = row["date"] + "T10:00:00+08:00"
            point = {"price": row["close"] / 1.01, "open": row["open"], "quote_timestamp": stamp,
                     "fetched_at": row["date"] + "T10:00:05+08:00", "timestamp_quality": "source_reported"}
            points.extend([point, deepcopy(point)])
            points.append({**point, "price": 1, "quote_timestamp": row["date"] + "T14:00:00+08:00"})
        data["intraday"] = {"chip": {"points": points}}
        now = datetime.fromisoformat("2026-11-16T10:01:00+08:00")
        result = model.update_predictions(data, now=now)
        row = result["predictions"][0]
        self.assertNotEqual(row["model_version"], "naive-now-v1")
        # Twenty initial rows cannot construct the daily feature; no duplicates count twice.
        self.assertEqual(row["sample_count"], len(rows) - 21)
        self.assertAlmostEqual(row["expected_remaining_return_pct"], 1, delta=.03)
        self.assertIn("live_return_pct", row["feature_metadata"]["names"])
        # Retrospectively downloaded snapshots are ineligible even with old source times.
        for point in points:
            point["fetched_at"] = point["quote_timestamp"][:10] + "T16:00:00+08:00"
        fallback = model.update_predictions(data, now=now)["predictions"][0]
        self.assertEqual(fallback["model_version"], "naive-now-v1")
        self.assertEqual(fallback["sample_count"], 0)


if __name__ == "__main__":
    unittest.main()
