from copy import deepcopy
from datetime import date, datetime, timedelta
import gzip
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from pipeline.calendar import is_trading_day
from pipeline.equity_model import (_universe, aggregate_predictions, build_samples, chronological_split,
    load_history, predict_portable, sequence_features, train_shared_baseline, update_equity_predictions)


def make_history(end="2026-11-13", count=3):
    result = {}
    for stock in range(count):
        bars, day, close, i = [], date(2026, 1, 5), 10. + stock, 0
        while day <= date.fromisoformat(end):
            if is_trading_day(day):
                opening = close * (1 + .002 * math.sin(i * .7 + stock))
                last = opening * (1 + .004 * math.sin(i * .3 + stock))
                bars.append({"ts_code": f"{stock + 1:06d}.SZ", "date": day.isoformat(), "trade_date": day.isoformat(),
                             "open": opening, "close": last, "pre_close": close,
                             "high": max(opening, last) * 1.002, "low": min(opening, last) * .998,
                             "vol": 1000 + i, "amount": 10000 + i * 10})
                close, i = last, i + 1
            day += timedelta(days=1)
        result[f"{stock + 1:06d}.SZ"] = bars
    return result


def write_history(root, history):
    by_date = {}
    for bars in history.values():
        for bar in bars:
            by_date.setdefault(bar["date"], []).append(bar)
    root.mkdir(parents=True, exist_ok=True)
    for day, rows in by_date.items():
        with gzip.open(root / (day.replace("-", "") + ".json.gz"), "wt", encoding="utf-8") as handle:
            json.dump({"trade_date": day, "complete": True, "rows": rows}, handle)


def dashboard(history):
    return {"sectors": [{"id": "801010.SI", "name": "测试行业", "constituents": [
        {"ts_code": code, "name": code, "stock_api_daily_basic": {"trade_date": "2026-11-13", "circ_mv": i + 1}}
        for i, code in enumerate(history)]}], "stock_api_state": {}}


class EquityModelTests(unittest.TestCase):
    def test_validated_current_universe_excludes_stale_sector_members(self):
        source = {'sectors': [{'id': 'x', 'constituents': [{'ts_code': '000001.SZ'}, {'ts_code': '000002.SZ'}]}],
            'stock_api_state': {'daily_basic': {'full_market_request': True, 'quality': {'complete': True},
                'trade_date': '2026-11-16', 'records': [{'ts_code': '000001.SZ', 'circ_mv': 3.}]}}}
        stocks, members = _universe(source, {})
        self.assertEqual(list(stocks), ['000001.SZ'])
        self.assertEqual(members['x'], ['000001.SZ'])

    def test_partial_batches_are_excluded_and_parent_directory_accepted(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "daily"
            write_history(directory, make_history(count=1))
            with gzip.open(directory / "20261116.json.gz", "wt") as handle:
                json.dump({"complete": False, "rows": [{"ts_code": "000001.SZ", "trade_date": "2026-11-16", "close": 999}]}, handle)
            loaded = load_history(root)
            self.assertEqual(loaded["000001.SZ"][-1]["date"], "2026-11-13")

    def test_ex_rights_uses_label_preclose_not_prior_raw_close(self):
        history = make_history(count=1)
        code = next(iter(history))
        last = history[code][-1]
        last.update(pre_close=5., open=5.1, close=5.2, high=5.3, low=5.)
        samples = build_samples(history)
        self.assertAlmostEqual(samples["y_open"][-1], 2.)
        self.assertAlmostEqual(samples["y_close"][-1], 4.)

    def test_suspended_stock_cannot_label_next_observed_as_next_session(self):
        history = make_history(count=2)
        code = sorted(history)[0]
        removed = history[code].pop(-2)["date"]
        before = history[code][-2]["date"]
        samples = build_samples(history)
        self.assertNotIn((code, before), list(zip(samples["codes"], samples["dates"])))
        self.assertIn(removed, [bar["date"] for bar in history[sorted(history)[1]]])

    def test_date_split_has_gaps_and_stocks_never_split_same_date(self):
        dates = [f"2026-01-{day:02d}" for day in range(1, 21)] * 3
        split = chronological_split(dates, min_train_dates=10, min_validation_dates=4, min_test_dates=4)
        self.assertEqual(len(split["gap_dates"]), 2)
        for day in set(dates):
            selected = np.asarray(dates) == day
            self.assertLessEqual(sum(bool(mask[selected].any()) for name, mask in split.items() if name.endswith("_mask")), 1)
        self.assertIsNone(chronological_split(dates, min_train_dates=90))

    def test_sequence_is_past_only_and_uses_ex_rights_basis(self):
        bars = make_history(count=1)["000001.SZ"]
        first = sequence_features(bars, 70)
        modified = deepcopy(bars)
        modified[80]["close"] = 1e9
        self.assertEqual(first.shape, (60, 8))
        np.testing.assert_array_equal(first, sequence_features(modified, 70))

    def test_zero_candidate_never_promoted_for_constant_zero_targets(self):
        history = make_history(count=1)
        for bar in history["000001.SZ"]:
            bar.update(open=10., close=10., pre_close=10., high=10.1, low=9.9)
        with tempfile.TemporaryDirectory() as root:
            artifact = train_shared_baseline(history, root)
            self.assertFalse(artifact["report"]["promoted"])
            self.assertEqual(artifact["targets"]["next_open"]["model"], "zero_return")
            self.assertIsNone(artifact["report"]["metrics"]["next_open"]["test"]["direction_accuracy"])
            self.assertTrue((Path(root) / "shared_baseline.json").exists())

    def test_portable_ridge_is_finite_and_test_statistics_do_not_fit_scaler(self):
        history = make_history(count=2)
        with tempfile.TemporaryDirectory() as root:
            artifact = train_shared_baseline(history, root)
            self.assertEqual(artifact["report"]["date_ranges"]["test"]["dates"], 20)
            for model in artifact["targets"].values():
                if model["parameters"]:
                    xs = build_samples(history)["X"][:3]
                    self.assertTrue(np.isfinite(predict_portable(model["parameters"], xs)).all())
            json.dumps(artifact, allow_nan=False)

    def test_aggregate_deduplicates_and_discloses_missing_weights(self):
        context = {"target_date": "2026-11-16", "target_kind": "next_open", "reference_date": "2026-11-13"}
        predictions = {code: {**context, "model": "ridge", "predicted_return_pct": value} for code, value in (("a", 1.), ("b", 3.))}
        full = aggregate_predictions(predictions, ["a", "a", "b"], {"a": 1., "b": 3.}, context)
        self.assertEqual(full["universe_count"], 2)
        self.assertAlmostEqual(full["predicted_return_pct"], 2.5)
        missing = aggregate_predictions(predictions, ["a", "b"], {"a": 1.}, context)
        self.assertEqual(missing["status"], "partial")
        self.assertFalse(missing["weights_complete"])
        self.assertEqual(missing["known_weight_count"], 1)
        stale = deepcopy(predictions)
        stale["b"]["target_date"] = "2026-11-17"
        self.assertEqual(aggregate_predictions(stale, ["a", "b"], {"a": 1., "b": 3.}, context)["predicted_count"], 1)

    def test_intraday_abstains_and_immutable_ledger_settles_later(self):
        history = make_history(count=2)
        data = dashboard(history)
        with tempfile.TemporaryDirectory() as root:
            directory, models = Path(root) / "daily", Path(root) / "models"
            write_history(directory, history)
            initial = update_equity_predictions(data, directory, models, datetime.fromisoformat("2026-11-16T08:00:00+08:00"))
            self.assertEqual(initial["equity_forecast"]["coverage"]["predicted_count"], 2)
            for stock in initial['equity_forecast']['stocks']:
                reference = next(row for row in history[stock['code']] if row['date'] == stock['reference_date'])
                self.assertEqual(stock['reference_price'], reference['close'])
                self.assertIsNone(stock.get('predicted_price'))
            path = Path(root) / "forecasts" / "20261116-next_open.json.gz"
            with gzip.open(path, "rt") as handle:
                original = json.load(handle)
            intraday = update_equity_predictions(initial, directory, models, datetime.fromisoformat("2026-11-16T10:00:00+08:00"))
            self.assertEqual(intraday["equity_forecast"]["coverage"]["predicted_count"], 0)
            expanded = make_history(end="2026-11-16", count=2)
            write_history(directory, expanded)
            settled = update_equity_predictions(intraday, directory, models, datetime.fromisoformat("2026-11-16T16:00:00+08:00"))
            with gzip.open(path, "rt") as handle:
                after = json.load(handle)
            self.assertEqual(original["predictions"], after["predictions"])
            self.assertEqual(original["prediction_hash"], after["prediction_hash"])
            self.assertEqual(len(after["settlements"]), 2)
            self.assertEqual(settled["equity_forecast"]["evaluation"]["settled_count"], 2)

    def test_quote_only_never_reads_history_and_expires_open_forecast(self):
        initial = {"sectors": [{"id": "x"}], "equity_forecast": {"target_date": "2026-11-16", "target_kind": "next_open",
                    "reference_date": "2026-11-13", "coverage": {"universe_count": 10}, "stocks": [{"predicted_return_pct": 3}]}}
        with patch("pipeline.equity_model.load_history", side_effect=AssertionError("must not load archive")):
            same = update_equity_predictions(initial, "/nope", "/nope", datetime.fromisoformat("2026-11-16T08:30:00+08:00"), {"equity": {"reuse_only": True}})
            self.assertTrue(same["equity_forecast"]["reuse_only"])
            expired = update_equity_predictions(initial, "/nope", "/nope", datetime.fromisoformat("2026-11-16T10:00:00+08:00"), {"reuse_only": True})
            self.assertEqual(expired["equity_forecast"]["status"], "expired")
            self.assertEqual(expired["equity_forecast"]["stocks"], [])
            self.assertIsNone(expired["sectors"][0]["equity_prediction"]["predicted_return_pct"])

    def test_future_latest_bar_is_filtered_and_missing_previous_date_abstains(self):
        history = make_history(end="2026-11-17", count=1)
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "daily"
            write_history(directory, history)
            result = update_equity_predictions(dashboard(history), directory, Path(root) / "models", datetime.fromisoformat("2026-11-16T08:00:00+08:00"))
            self.assertEqual(result["equity_forecast"]["stocks"][0]["reference_date"], "2026-11-13")
            self.assertEqual(result["equity_forecast"]["coverage"]["predicted_count"], 1)
            (directory / "20261113.json.gz").unlink()
            stale = update_equity_predictions(dashboard(history), directory, Path(root) / "models", datetime.fromisoformat("2026-11-16T08:00:00+08:00"))
            self.assertEqual(stale["equity_forecast"]["coverage"]["predicted_count"], 0)


if __name__ == "__main__":
    unittest.main()
