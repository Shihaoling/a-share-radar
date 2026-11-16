from copy import deepcopy
from datetime import date, datetime, time, timedelta
import json
import math
import unittest
from unittest.mock import patch

from pipeline import daily_forecast as daily
from pipeline.calendar import SHANGHAI, is_trading_day, next_trading_day


def fixture():
    days, day = [], date(2026, 1, 5)
    while len(days) < 65:
        if is_trading_day(day):
            days.append(day)
        day += timedelta(days=1)
    entities = []
    for j, identity in enumerate(("sh000300", "sh000001", "801010.SI", "801030.SI", "801040.SI")):
        history = []
        for i, day in enumerate(days):
            close = 100 + i * .1 + math.sin(i * .7 + j)
            history.append({"date": day.isoformat(), "open": close * .999, "high": close * 1.01,
                            "low": close * .99, "close": close, "volume": 100 + i,
                            "amount": 1000 + i, "volume_unit": "share", "amount_unit": "CNY",
                            "source": "fixture", "complete": True})
        entities.append({"id": identity, "name": identity, "history": history})
    data = {"market": entities[:2], "sectors": entities[2:]}
    now = datetime.combine(days[-1], time(16), SHANGHAI)
    table = daily.feature_table(data)
    bundle = {"schema_version": daily.SCHEMA, "feature_version": daily.FEATURE_VERSION,
              "entity_ids": table["entity_ids"], "feature_names": table["feature_names"],
              "model_id": "fixture-a", "trained_at": now.isoformat(), "training_cutoff": days[-1].isoformat(),
              "horizon_trading_sessions": 1, "target": "next_close_return_pct", "unit": "percentage_points",
              "selected_candidates": {"market": "constant", "sector": "constant", "sector_ranking": "momentum_5"},
              "models": {"market": {"family": "constant", "value": .1}, "sector": {"family": "constant", "value": .1},
                         "sector_ranking": {"family": "signal", "column": 5}},
              "evaluation": {"market": {"by_id": {}}, "sector": {"by_id": {}}}}
    return data, now, table, bundle


class DailyForecastTest(unittest.TestCase):
    def test_future_changes_do_not_change_earlier_features(self):
        data, _, before, _ = fixture()
        day = before["market"][10]["feature_date"]
        changed = deepcopy(data)
        for group in ("market", "sectors"):
            for entity in changed[group]:
                for bar in entity["history"]:
                    if bar["date"] > day:
                        for key in ("open", "high", "low", "close"):
                            bar[key] *= 3
        after = daily.feature_table(changed)
        for group in ("market", "sectors"):
            a = {(r["id"], r["feature_date"]): r["x"] for r in before[group] if r["feature_date"] <= day}
            b = {(r["id"], r["feature_date"]): r["x"] for r in after[group] if r["feature_date"] <= day}
            self.assertEqual(a, b)

    def test_source_unit_switch_marks_flow_missing(self):
        data, _, _, _ = fixture()
        rows = data["market"][0]["history"][-21:]
        rows[-1]["volume_unit"] = "lot"
        values = daily._own_features(rows)
        self.assertEqual(values[21], 1.)
        self.assertEqual(values[23], 1.)

    def test_constant_ranking_has_no_information(self):
        self.assertIsNone(daily.rank_ic([1, 2, 3], [0, 0, 0]))
        self.assertAlmostEqual(daily.rank_ic([1, 2, 3], [1, 2, 3]), 1.)
        self.assertEqual(daily._ranks([2, 2, 1]), [2.5, 2.5, 1.])

    def test_bundle_hash_is_checked(self):
        _, _, _, bundle = fixture()
        bundle["sha256"] = daily.digest(bundle)
        with patch("pathlib.Path.read_text", return_value=json.dumps(bundle)):
            self.assertEqual(daily.load_bundle("test-bundle.json")["model_id"], "fixture-a")
        bundle["models"]["market"]["value"] = 9
        with patch("pathlib.Path.read_text", return_value=json.dumps(bundle)):
            with self.assertRaises(ValueError):
                daily.load_bundle("test-bundle.json")

    def test_model_upgrade_retains_first_issue(self):
        data, now, _, bundle = fixture()
        with patch.object(daily, "load_bundle", return_value=bundle):
            bundle["sha256"] = "test"
            first = daily.update_daily_forecasts(data, "unused", now)
        newer = deepcopy(bundle)
        newer["model_id"] = "fixture-b"
        newer["models"]["market"]["value"] = .9
        with patch.object(daily, "load_bundle", return_value=newer):
            second = daily.update_daily_forecasts(data, "unused", now, previous=first)
        self.assertEqual(first["daily_forecast"]["ledger"], second["daily_forecast"]["ledger"])
        self.assertEqual(second["daily_forecast"]["market"][0]["predicted_return_pct"], .1)
        self.assertEqual(len(second["daily_forecast"]["ledger"]), 5)

    def test_future_available_prices_are_not_used(self):
        data, now, _, bundle = fixture()
        for group in ("market", "sectors"):
            for entity in data[group]:
                for bar in entity["history"]:
                    bar["price_available_at"] = (now + timedelta(days=1)).isoformat()
        bundle["sha256"] = "test"
        with patch.object(daily, "load_bundle", return_value=bundle):
            result = daily.update_daily_forecasts(data, "unused", now)
        self.assertEqual(result["daily_forecast"]["status"], "pending")
        self.assertEqual(result["daily_forecast"]["ledger"], [])

    def test_corrupt_portable_model_is_contained(self):
        data, now, _, bundle = fixture()
        bundle["sha256"] = "test"
        bundle["models"]["market"] = {"family": "ridge", "coef": []}
        with patch.object(daily, "load_bundle", return_value=bundle):
            result = daily.update_daily_forecasts(data, "unused", now)
        self.assertEqual(result["daily_forecast"]["status"], "error")

    def test_stale_close_cannot_issue_an_already_matured_target(self):
        data, now, _, bundle = fixture()
        bundle["sha256"] = "test"
        target = next_trading_day(now.date())
        late = datetime.combine(target, time(16), SHANGHAI)
        with patch.object(daily, "load_bundle", return_value=bundle):
            result = daily.update_daily_forecasts(data, "unused", late)
        self.assertEqual(result["daily_forecast"]["status"], "pending")

    def test_matured_label_settles_once_and_is_not_revised(self):
        data, now, _, bundle = fixture()
        bundle["sha256"] = "test"
        with patch.object(daily, "load_bundle", return_value=bundle):
            first = daily.update_daily_forecasts(data, "unused", now)
        target = next_trading_day(now.date())
        later = datetime.combine(target, time(16), SHANGHAI)
        for group in ("market", "sectors"):
            for entity in data[group]:
                bar = deepcopy(entity["history"][-1])
                bar["date"] = target.isoformat()
                for key in ("open", "high", "low", "close"):
                    bar[key] *= 1.01
                entity["history"].append(bar)
        with patch.object(daily, "load_bundle", return_value=bundle):
            second = daily.update_daily_forecasts(data, "unused", later, previous=first)
        settled = [row for row in second["daily_forecast"]["ledger"] if row.get("settled_at")]
        self.assertEqual(len(settled), 5)
        self.assertTrue(all(abs(row["actual_return_pct"] - 1.) < 1e-10 for row in settled))
        for group in ("market", "sectors"):
            for entity in data[group]:
                for key in ("open", "high", "low", "close"):
                    entity["history"][-1][key] *= 1.1
        with patch.object(daily, "load_bundle", return_value=bundle):
            third = daily.update_daily_forecasts(data, "unused", later, previous=second)
        settled_again = [row for row in third["daily_forecast"]["ledger"] if row.get("settled_at")]
        self.assertEqual(settled, settled_again)

    def test_invalid_predicted_index_level_is_rejected(self):
        data, now, _, bundle = fixture()
        bundle["sha256"] = "test"
        bundle["models"]["market"]["value"] = -101.
        with patch.object(daily, "load_bundle", return_value=bundle):
            result = daily.update_daily_forecasts(data, "unused", now)
        self.assertEqual(result["daily_forecast"]["status"], "error")
        self.assertEqual(result["daily_forecast"]["ledger"], [])

    def test_mae_uses_percentage_points_and_daily_equal_weight(self):
        rows = [{"id": "a", "label_date": "2026-01-05", "y": 1.},
                {"id": "b", "label_date": "2026-01-05", "y": 3.},
                {"id": "a", "label_date": "2026-01-06", "y": 0.}]
        result = daily.evaluate(rows, [0., 0., 0.])
        self.assertEqual(result["mae_pct"], 1.)
        self.assertEqual(result["ranking_coverage"], 0.)


if __name__ == "__main__":
    unittest.main()
