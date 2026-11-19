from datetime import date
import unittest

from pipeline.trend import classify_window, daily_returns, observe, rank_leaders, session_dates


class TrendObservationTests(unittest.TestCase):
    def setUp(self):
        self.sessions = session_dates(date(2026, 11, 19))

    def test_five_states_and_single_jump(self):
        prior = [-0.01, 0.01] * 20
        cases = [(0.02, 2), (0.004, 1), (0, 0), (-0.004, -1), (-0.02, -2)]
        for move, expected in cases:
            with self.subTest(state=expected):
                result = classify_window(prior + [move] * 10, self.sessions[-11:])
                self.assertEqual(result["state"], expected)
        reversal = classify_window(prior + [0.03, -0.03] * 5, self.sessions[-11:])
        self.assertEqual(reversal["state"], 0)
        jump = classify_window(prior + [0.2] + [0] * 9, self.sessions[-11:])
        self.assertEqual(jump["state"], 1)

    def test_suspend_and_untrusted_bar_do_not_bridge_window(self):
        bars = [{"date": day, "close": 100, "source": "stock_api"} for day in self.sessions]
        bars[-7]["source"] = "eastmoney_page"
        bars[-1]["complete"] = False
        result = observe(bars, self.sessions)
        self.assertEqual(result["as_of"], self.sessions[-2])
        self.assertEqual(result["windows"]["10"], {"reason": "missing_window"})

    def test_stock_returns_use_the_adjusted_previous_close(self):
        days = self.sessions[-3:]
        bars = [{"date": days[0], "close": 20}, {"date": days[1], "close": 10, "pre_close": 10},
                {"date": days[2], "close": 10.1, "pre_close": 10}]
        values = daily_returns(bars, days, stock=True)
        self.assertEqual(values[1], 0)
        self.assertAlmostEqual(values[2], 0.00995033)
        self.assertEqual(daily_returns(bars[1:], days, stock=True)[1], None)

    def test_leader_rank_needs_membership_and_same_day_value_coverage(self):
        members = [{"code": str(i), "name": str(i)} for i in range(10)]
        caps = {str(i): i + 1 for i in range(1, 10)}
        self.assertEqual(rank_leaders(members, caps, complete=False)[1], "membership_pending")
        self.assertEqual(rank_leaders(members, {"1": 100}, complete=True)[1], "ranking_pending")
        stocks, basis, coverage = rank_leaders(members, caps, complete=True)
        self.assertEqual([row["id"] for row in stocks], ["9", "8", "7"])
        self.assertEqual((basis, coverage), ("total_market_cap", 0.9))
