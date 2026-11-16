from copy import deepcopy
from datetime import date, datetime, timedelta
import math
import unittest

from pipeline.calendar import is_trading_day
from pipeline.model import update_predictions


def history(end="2026-11-13"):
    rows, day, close, i = [], date(2026, 1, 5), 1000.0, 0
    while day <= date.fromisoformat(end):
        if is_trading_day(day):
            opening = close * (1 + .001 * math.sin(i * .8))
            close = opening * (1 + .003 * math.sin(i * .3))
            rows.append({"date": day.isoformat(), "open": opening, "close": close,
                         "high": max(opening, close) * 1.001, "low": min(opening, close) * .999})
            i += 1
        day += timedelta(days=1)
    return rows


def dashboard(rows=None):
    return {"market": [], "sectors": [{"id": "chip", "name": "半导体", "history": rows if rows is not None else history(),
                                         "price": 1010.0, "as_of": "2026-11-16T10:00:00+08:00"}], "news": []}


class ModelTests(unittest.TestCase):
    def update(self, data, previous=None, when="2026-11-16T08:00:00+08:00", config=None):
        return update_predictions(data, previous=previous, config=config, now=datetime.fromisoformat(when))

    def test_no_future_day_bar_or_future_publication_leakage(self):
        original = dashboard()
        dirty = deepcopy(original)
        dirty["sectors"][0]["history"] += [
            {"date": "2026-11-16", "open": 1, "close": 999999, "high": 999999, "low": 1},
            {"date": "2026-11-17", "open": 1, "close": 888888},
        ]
        # A revision not yet available must not replace the historical row.
        dirty["sectors"][0]["history"].append({"date": "2026-11-13", "open": 1, "close": 123456,
                                                "available_at": "2026-11-17T16:00:00+08:00"})
        first, second = self.update(original), self.update(dirty)
        self.assertEqual(first["predictions"], second["predictions"])
        self.assertEqual(first["predictions"][0]["status"], "ready")
        self.assertEqual(first["predictions"][0]["training_cutoff"], "2026-11-13")

    def test_current_day_close_not_available_before_confirmation(self):
        data = dashboard(history("2026-11-16"))
        result = self.update(data, when="2026-11-16T15:01:00+08:00")
        self.assertEqual(result["predictions"][0]["status"], "pending")
        self.assertIn("2026-11-16", result["predictions"][0]["reason"])

    def test_missing_exact_preceding_close_does_not_use_older_day(self):
        data = dashboard(history("2026-11-12"))
        result = self.update(data)
        self.assertEqual(result["predictions"][0]["status"], "pending")
        self.assertIn("2026-11-13", result["predictions"][0]["reason"])

    def test_holiday_reference_is_september_30(self):
        result = self.update(dashboard(history("2026-09-30")), when="2026-10-08T08:30:00+08:00")
        self.assertEqual(result["predictions"][0]["reference_date"], "2026-09-30")

    def test_intraday_is_honest_persistence_and_ignores_current_close(self):
        data = dashboard(history("2026-11-16"))
        original = deepcopy(data)
        result = self.update(data, when="2026-11-16T10:01:00+08:00")
        prediction = result["predictions"][0]
        self.assertEqual(prediction["model_version"], "naive-now-v1")
        self.assertEqual(prediction["predicted_price"], 1010)
        self.assertIsNone(prediction["up_probability"])
        self.assertIsNone(result["models"]["last_trained_at"])
        self.assertEqual(data, original)

    def test_retrieval_timestamp_and_stale_quote_rejected(self):
        data = dashboard()
        data["sectors"][0]["timestamp_quality"] = "retrieved_only"
        self.assertEqual(self.update(data, when="2026-11-16T10:01:00+08:00")["predictions"][0]["status"], "pending")
        data["sectors"][0].pop("timestamp_quality")
        self.assertEqual(self.update(data, when="2026-11-16T11:00:00+08:00")["predictions"][0]["status"], "pending")

    def test_live_snapshot_used_without_overwriting_daily_price(self):
        data = dashboard()
        entity = data['sectors'][0]
        entity.update(price=999, as_of='2026-11-13T15:00:00+08:00')
        entity['live_quote'] = {'price':1025, 'quote_timestamp':'2026-11-16T10:00:00+08:00', 'timestamp_quality':'source_reported'}
        prediction = self.update(data, when='2026-11-16T10:01:00+08:00')['predictions'][0]
        self.assertEqual(prediction['predicted_price'],1025)
        self.assertEqual(entity['price'],999)
        entity['live_quote']['quote_timestamp'] = '2026-11-16T10:02:00+08:00'
        self.assertEqual(self.update(data, when='2026-11-16T10:01:00+08:00')['predictions'][0]['status'],'pending')

    def test_lunch_quote_is_valid_and_today_target_is_retained(self):
        data = dashboard()
        data["sectors"][0]["as_of"] = "2026-11-16T11:30:00+08:00"
        result = self.update(data, when="2026-11-16T12:50:00+08:00")
        self.assertEqual(result["predictions"][0]["status"], "ready")
        self.assertEqual(result["predictions"][0]["target_kind"], "today_close")

    def test_open_settlement_delayed_and_original_forecast_immutable(self):
        data = dashboard()
        initial = self.update(data)
        original = deepcopy(initial)
        actual_open = data["sectors"][0]["history"][-1]["close"] * 1.01
        data["sectors"][0]["history"].append({"date": "2026-11-16", "open": actual_open, "close": actual_open * 1.1,
                                               "complete": False})
        early = self.update(data, initial, when="2026-11-16T09:24:00+08:00")
        self.assertNotEqual(early["ledger"][0]["status"], "settled")
        settled = self.update(data, initial, when="2026-11-16T09:30:00+08:00")
        row = settled["ledger"][0]
        self.assertEqual(row["status"], "settled")
        self.assertEqual(row["actual_price"], actual_open)
        self.assertAlmostEqual(row["actual_return_pct"], 1.0)
        self.assertAlmostEqual(row["error_pct"], row["predicted_return_pct"] - 1)
        self.assertAlmostEqual(row["baseline_error_pct"], -1)
        for key in ("id", "issued_at", "predicted_price", "feature_hash", "model_version"):
            self.assertEqual(row[key], initial["ledger"][0][key])
        self.assertEqual(initial, original)

    def test_close_settlement_waits_for_completed_close(self):
        data = dashboard()
        first = self.update(data, when="2026-11-16T10:01:00+08:00")
        data["sectors"][0]["history"].append({"date": "2026-11-16", "open": 1005, "close": 1020})
        early = self.update(data, first, when="2026-11-16T14:00:00+08:00")
        self.assertEqual(early["ledger"][0]["status"], "ready")
        late = self.update(data, first, when="2026-11-16T15:16:00+08:00")
        self.assertEqual(late["ledger"][0]["status"], "settled")
        self.assertEqual(late["ledger"][0]["actual_price"], 1020)

    def test_identical_snapshot_is_idempotent(self):
        data = dashboard()
        first = self.update(data)
        second = self.update(data, first)
        self.assertEqual(second["ledger"], first["ledger"])
        self.assertEqual(len(second["ledger"]), 1)

    def test_insufficient_history_and_unknown_calendar_abstain(self):
        result = self.update(dashboard(history("2026-02-10")), when="2026-02-11T08:00:00+08:00")
        self.assertEqual(result["predictions"][0]["status"], "pending")
        self.assertIsNone(result["models"]["last_trained_at"])
        result = self.update(dashboard(), when="2027-01-04T10:00:00+08:00")
        self.assertIsNone(result["predictions"][0]["target_date"])
        self.assertEqual(result["predictions"][0]["status"], "pending")

    def test_future_news_not_in_feature_snapshot_and_news_not_price_input(self):
        plain = dashboard()
        future = deepcopy(plain)
        future["news"] = [{"id": "future", "title": "future", "first_seen_at": "2026-11-17T08:00:00+08:00"}]
        self.assertEqual(self.update(plain)["predictions"], self.update(future)["predictions"])
        future["news"][0]["first_seen_at"] = "2026-11-15T08:00:00+08:00"
        observed = self.update(future)["predictions"][0]
        self.assertEqual(observed["news_features"], "collecting")
        self.assertEqual(observed["predicted_price"], self.update(plain)["predictions"][0]["predicted_price"])

    def test_optional_mlp_evaluated_on_later_confirmation_window(self):
        result = self.update(dashboard(), config={"enable_mlp": True})
        row = result["predictions"][0]
        self.assertEqual(row["status"], "ready")
        self.assertIn("mlp", row["validation"]["scores"])
        self.assertLess(row["validation"]["selection_start"], row["validation"]["confirmation_start"])
        self.assertLess(row["validation"]["confirmation_start"], row["target_date"])
        self.assertIsNotNone(result["models"]["last_trained_at"])

    def test_settled_actual_not_silently_revised(self):
        data = dashboard()
        first = self.update(data, when="2026-11-16T10:01:00+08:00")
        data["sectors"][0]["history"].append({"date": "2026-11-16", "open": 1005, "close": 1020})
        settled = self.update(data, first, when="2026-11-16T15:16:00+08:00")
        data["sectors"][0]["history"][-1]["close"] = 5000
        rerun = self.update(data, settled, when="2026-11-16T15:17:00+08:00")
        old = next(row for row in rerun["ledger"] if row["id"] == first["ledger"][0]["id"])
        self.assertEqual(old["actual_price"], 1020)


if __name__ == "__main__":
    unittest.main()
