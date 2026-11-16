from copy import deepcopy
from datetime import date, timedelta
import math
import statistics
import unittest

from pipeline.calendar import is_trading_day
from pipeline.metrics import compute_metrics


DAY = "2026-11-16"


def stock(code, cap, **kwargs):
    return {"code": code, "market_cap": cap, "valuation_date": DAY, **kwargs}


def dashboard(stocks=None, **kwargs):
    return {"generated_at": DAY + "T16:00:00+08:00", "market": [],
            "sectors": [{"id": "chip", "valuation_date": DAY, "stocks": stocks or [], **kwargs}]}


def prices(count=100, constant=False):
    day, value, rows = date(2026, 1, 5), 100.0, []
    while len(rows) < count:
        if is_trading_day(day):
            if rows:
                value *= 1.001 if constant else 1 + .001 + .004 * math.sin(len(rows) * .9)
            rows.append({"date": day.isoformat(), "close": value})
        day += timedelta(days=1)
    return rows


class QuantMetricsTests(unittest.TestCase):
    def metrics(self, data, config=None):
        return compute_metrics(data, config)["sectors"][0]["quant_metrics"]

    def test_ratio_of_sums_preserves_loss_and_is_not_average_pe(self):
        data = dashboard([stock("a", 100, pe_ttm=10, pb=2), stock("b", 100, pe_ttm=-20, pb=4)], constituent_count=2)
        result = self.metrics(data)
        self.assertEqual(result["pe_ttm"]["value"], 40)
        self.assertAlmostEqual(result["pb"]["value"], 200 / 75)
        self.assertEqual(result["pe_ttm"]["status"], "ok")
        self.assertEqual(result["pe_ttm"]["coverage"]["count_ratio"], 1)

    def test_nonpositive_aggregate_profit_and_equity_not_applicable(self):
        data = dashboard([stock("a", 100, net_profit_parent_ttm=-5, equity_parent=-10)], constituent_count=1)
        result = self.metrics(data)
        for name in ("pe_ttm", "pb"):
            self.assertIsNone(result[name]["value"])
            self.assertEqual(result[name]["status"], "not_applicable")

    def test_sample_label_and_count_market_cap_coverage(self):
        data = dashboard([stock("a", 100, pe_ttm=10), stock("b", 200, pe_dynamic=20)],
                         constituent_count=5, market_cap=1000)
        pe = self.metrics(data)["pe_ttm"]
        self.assertEqual(pe["value"], 10)
        self.assertEqual(pe["status"], "partial")
        self.assertIn("样本组合", pe["label"])
        self.assertAlmostEqual(pe["coverage"]["count_ratio"], .2)
        self.assertAlmostEqual(pe["coverage"]["market_cap_ratio"], .1)

    def test_unknown_full_count_never_claims_complete(self):
        pe = self.metrics(dashboard([stock("a", 100, pe_ttm=10)]))["pe_ttm"]
        self.assertIsNone(pe["coverage"]["total_count"])
        self.assertIn("样本组合", pe["label"])

    def test_dynamic_pe_is_not_ttm(self):
        pe = self.metrics(dashboard([stock("a", 100, pe=10, pe_dynamic=10)]))["pe_ttm"]
        self.assertIsNone(pe["value"])
        self.assertEqual(pe["sample_count"], 0)

    def test_mixed_dates_and_currencies_are_excluded(self):
        data = dashboard([stock("a", 100, pe_ttm=10), stock("b", 100, pe_ttm=10, pe_ttm_date="2026-11-13"),
                          stock("c", 100, pe_ttm=10, currency="USD")], constituent_count=3)
        pe = self.metrics(data)["pe_ttm"]
        self.assertEqual(pe["sample_count"], 1)
        self.assertAlmostEqual(pe["coverage"]["count_ratio"], 1 / 3)

    def test_future_financial_publication_excluded(self):
        data = dashboard([stock("a", 100, net_profit_parent_ttm=10, financial_available_at="2026-11-17")])
        self.assertIsNone(self.metrics(data)["pe_ttm"]["value"])

    def test_same_day_future_financials_and_quote_availability_are_excluded(self):
        data = dashboard([stock("a", 100, net_profit_parent_ttm=10,
                                financial_available_at=DAY + "T18:00:00+08:00"),
                          stock("b", 100, pe_ttm=10, as_of=DAY + "T18:00:00+08:00"),
                          stock("c", 100, pe_ttm=10, pe_ttm_available_at=DAY + "T18:00:00+08:00")])
        result = self.metrics(data)["pe_ttm"]
        self.assertEqual(result["sample_count"], 0)
        self.assertIsNone(result["value"])

    def test_future_timestamped_revision_does_not_change_historical_sharpe(self):
        rows = prices()
        expected = self.metrics(dashboard(history=rows))["sharpe"]
        revised = deepcopy(rows[-1])
        revised.update(close=99999, as_of=DAY + "T18:00:00+08:00")
        rows.append(revised)
        self.assertEqual(self.metrics(dashboard(history=rows))["sharpe"], expected)

    def test_zero_profit_is_retained_in_numerator(self):
        data = dashboard([stock("a", 100, net_profit_parent_ttm=10), stock("b", 100, net_profit_parent_ttm=0)], constituent_count=2)
        pe = self.metrics(data)["pe_ttm"]
        self.assertEqual(pe["value"], 20)
        self.assertEqual(pe["sample_count"], 2)

    def test_index_and_etf_without_financials_stay_null(self):
        data = dashboard(price=1000, pe_ttm=30, proxy={"code": "512480", "price": 1.2, "pe_ttm": 20})
        data["market"] = [{"id": "sh", "price": 3000}]
        output = compute_metrics(data)
        for entity in (output["market"][0], output["sectors"][0], output["sectors"][0]["proxy"]):
            self.assertIsNone(entity["quant_metrics"]["pe_ttm"]["value"])
            self.assertIsNone(entity["quant_metrics"]["pb"]["value"])

    def test_sharpe_exact_formula_ddof_and_risk_free_assumption(self):
        rows = prices()
        sharpe = self.metrics(dashboard(history=rows, adjustment="unadjusted"))["sharpe"]
        returns = [b["close"] / a["close"] - 1 for a, b in zip(rows, rows[1:])]
        daily_rf = 1.02 ** (1 / 252) - 1
        expected = math.sqrt(252) * statistics.mean(r - daily_rf for r in returns) / statistics.stdev(returns)
        self.assertAlmostEqual(sharpe["value"], expected)
        self.assertEqual(sharpe["sample_count"], 99)
        self.assertEqual(sharpe["ddof"], 1)
        self.assertEqual(sharpe["rf_annual"], .02)
        self.assertIn("未含分红/公司行动影响", sharpe["label"])
        zero_rf = self.metrics(dashboard(history=rows), {"rf_annual": 0})["sharpe"]
        self.assertGreater(zero_rf["value"], sharpe["value"])

    def test_less_than_sixty_returns_has_no_sharpe(self):
        result = self.metrics(dashboard(history=prices(60)))["sharpe"]
        self.assertEqual(result["sample_count"], 59)
        self.assertIsNone(result["value"])

    def test_sharpe_uses_at_most_latest_252_daily_returns(self):
        rows, day, value = [], date(2025, 5, 1), 100.0
        while day <= date.fromisoformat(DAY):
            if is_trading_day(day) is True or (is_trading_day(day) is None and day.weekday() < 5):
                value *= 1 + .002 * math.sin(len(rows) * .7)
                rows.append({"date": day.isoformat(), "close": value})
            day += timedelta(days=1)
        result = self.metrics(dashboard(history=rows))["sharpe"]
        self.assertEqual(result["sample_count"], 252)
        self.assertEqual(result["period_start"], rows[-253]["date"])
        changed = deepcopy(rows)
        changed[0]["close"] *= 10
        self.assertEqual(self.metrics(dashboard(history=changed))["sharpe"], result)

    def test_zero_volatility_has_no_sharpe(self):
        result = self.metrics(dashboard(history=prices(80, constant=True)))["sharpe"]
        self.assertEqual(result["status"], "not_applicable")
        self.assertIsNone(result["value"])

    def test_future_and_incomplete_bars_do_not_change_sharpe(self):
        data = dashboard(history=prices())
        baseline = self.metrics(data)["sharpe"]
        data["sectors"][0]["history"] += [{"date": "2026-11-17", "close": 99999},
                                          {"date": "2026-11-16", "close": 88888, "complete": False}]
        self.assertEqual(self.metrics(data)["sharpe"], baseline)

    def test_conflict_blocks_all_metrics_without_mutating_input(self):
        data = dashboard([stock("a", 100, pe_ttm=10, pb=2)], history=prices(), data_quality="conflict")
        old = deepcopy(data)
        result = self.metrics(data)
        self.assertEqual(data, old)
        for name in ("pe_ttm", "pb", "sharpe"):
            self.assertIsNone(result[name]["value"])
            self.assertEqual(result[name]["status"], "blocked")

    def test_mixed_adjustment_series_does_not_make_sharpe(self):
        rows = prices()
        rows[-1]["adjustment"] = "qfq"
        self.assertIsNone(self.metrics(dashboard(history=rows))["sharpe"]["value"])


if __name__ == "__main__":
    unittest.main()
