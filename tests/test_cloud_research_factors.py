"""Deterministic factor checks using a split-adjusted reference, a missing session and true paired returns."""
from datetime import date, timedelta
import importlib.util
import math
from pathlib import Path
from statistics import stdev
import unittest

from pipeline.calendar import is_trading_day

MODULE = Path(__file__).resolve().parents[1] / "deploy/tencent-preview/research_factors.py"
SPEC = importlib.util.spec_from_file_location("research_factors", MODULE)
factors = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(factors)


def trade_days(n):
    end = date(2026, 11, 18)
    days = []
    while len(days) < n:
        if is_trading_day(end):
            days.append(end.isoformat())
        end -= timedelta(days=1)
    return list(reversed(days))


class ResearchFactorTests(unittest.TestCase):
    def setUp(self):
        self.days = trade_days(61)
        self.market, self.asset = {}, []
        own, bench = 100.0, 250.0
        self.returns = []
        for i, day in enumerate(self.days):
            gain = 0.01 if i % 2 else -0.005
            if i:
                own *= 1 + 2*gain
                bench *= 1 + gain
                self.returns.append(2*gain)
            self.asset.append({"date":day, "close":own})
            self.market[day] = {"close":bench, "provider":"stock_api"}
        day = self.days[-1]
        self.quote = dict(quote_date=day,quote_timestamp=day+"T14:30:00+08:00",price=105,open=101,high=108,
                          low=98,previous_close=100)
        self.bq = dict(quote_date=day,quote_timestamp=day+"T14:29:00+08:00",price=202,previous_close=200)

    def result(self, **kw):
        defaults = dict(kind="sector", quote=self.quote, benchmark_quote=self.bq, bars=self.asset,
                        benchmark_bars=self.market, sessions=self.days)
        return {r["key"]:r for r in factors.compute(**{**defaults, **kw})}

    def test_analytic_values_and_metadata(self):
        data = self.result()
        self.assertEqual(len(data), 8)
        for key,row in data.items():
            self.assertTrue(all(row[k] for k in ("formula","unit","label","description","window","basis")))
            self.assertEqual(row["sample_count"], row["required_samples"],key)
            self.assertIsNone(row["missing_reason"])
            self.assertEqual(row["as_of"], self.days[-1])
        for key, expected in {"amplitude_pct":10,"opening_gap_pct":1,"relative_hs300_pp":4,"beta60":2,
                "volatility20_pct":stdev(self.returns[-20:])*math.sqrt(252)*100,
                "momentum20_pct":(self.asset[-1]["close"]/self.asset[-21]["close"]-1)*100,
                "ma20_bias_pct":(self.asset[-1]["close"]*20/sum(x["close"] for x in self.asset[-20:])-1)*100,
                "drawdown60_pct":1}.items():
            self.assertAlmostEqual(data[key]["value"],expected,places=8,msg=key)

    def test_individual_stock_reference_survives_dividend_split_without_an_artificial_drawdown(self):
        bars = []
        for i,row in enumerate(self.asset):
            multiplier = 1 if i < 35 else .5
            bars.append({**row,"close":row["close"]*multiplier, "source":"equity_cache", "adjustment":"unadjusted",
                         "pre_close":self.asset[max(0,i-1)]["close"]*multiplier, "adj_factor":None})
        stock = self.result(kind="stock",bars=bars)
        for key in factors.DAILY:
            self.assertAlmostEqual(stock[key]["value"],self.result()[key]["value"],places=8)
            self.assertIn("参考昨收",stock[key]["basis"])
        del bars[-5]["pre_close"]
        self.assertIsNone(self.result(kind="stock",bars=bars)["volatility20_pct"]["value"])

    def test_missing_session_duplicate_and_wrong_source_do_not_cross_boundaries(self):
        gaps = self.asset[:-7]+self.asset[-6:]
        result = self.result(bars=gaps)
        self.assertIsNone(result["beta60"]["value"])
        self.assertEqual(result["beta60"]["sample_count"],58)
        self.assertIsNone(result["ma20_bias_pct"]["value"])
        self.assertEqual(result["ma20_bias_pct"]["sample_count"],19)
        for bars in (self.asset+[dict(self.asset[-1],close=9)], self.asset[:-1]+[dict(self.asset[-1],source="tencent")],
                     self.asset[:-1]+[dict(self.asset[-1],complete=False)]):
            latest=self.result(bars=bars)["momentum20_pct"]
            self.assertIsNone(latest["value"])
            self.assertEqual((latest["as_of"],latest["through"]),tuple(self.days[-2:]))
        self.assertIsNone(self.result(sessions=self.days[:-7]+self.days[-6:])["beta60"]["value"])

    def test_disjoint_benchmark_stale_intraday_and_flat_beta_leave_other_values_usable(self):
        old = dict(self.bq,quote_timestamp=self.days[-1]+"T14:28:29+08:00")
        r = self.result(benchmark_quote=old)
        self.assertIsNone(r["relative_hs300_pp"]["value"])
        self.assertAlmostEqual(r["opening_gap_pct"]["value"],1)
        self.assertIn("90秒",r["relative_hs300_pp"]["missing_reason"])
        at_limit=dict(self.bq,quote_timestamp=self.days[-1]+"T14:28:30+08:00")
        self.assertIsNotNone(self.result(benchmark_quote=at_limit)["relative_hs300_pp"]["value"])
        other=self.result(benchmark_bars={d:{"close":100} for d in self.days})
        self.assertIsNone(other["beta60"]["value"])
        self.assertIn("波动",other["beta60"]["missing_reason"])
        del self.market[self.days[-1]]
        sparse=self.result()
        self.assertEqual(sparse["beta60"]["as_of"],self.days[-2])
        self.assertIsNotNone(sparse["volatility20_pct"]["value"])

    def test_heat_estimate_missing_data_and_daily_cutoff(self):
        for tag in ("hot","estimate","constituent_estimate"):
            r = self.result(quote={**self.quote,"basis":tag})
            self.assertIsNone(r["amplitude_pct"]["value"])
            self.assertIsNone(r["opening_gap_pct"]["value"])
            self.assertIsNotNone(r["relative_hs300_pp"]["value"])
        missing=self.result(bars=[],benchmark_bars=[],quote={},benchmark_quote={})
        self.assertTrue(all(row["value"] is None and row["missing_reason"] and row["sample_count"]==0
                            for row in missing.values()))
        future=self.asset+[{"date":"2026-11-19","close":12}]
        self.assertEqual(self.result(bars=future)["ma20_bias_pct"],self.result()["ma20_bias_pct"])
        yesterday=dict(self.quote,quote_date=self.days[-2])
        self.assertIsNone(self.result(quote=yesterday)["relative_hs300_pp"]["value"])

    def test_breadth_discloses_roster_timing_and_true_coverage(self):
        day=self.days[-1]
        members=[{"code":f"{n:06}.SZ"} for n in range(10)]
        returns={r["code"]:gain for r,gain in zip(members,[-2,-1,0,0,1,1,2,3])}
        checked=day+"T20:00:00+08:00"
        up,spread=factors.board_breadth(members+[members[0]],returns,day,complete=True,checked_at=checked)
        self.assertEqual((up["sample_count"],up["total_count"],up["required_samples"],up["coverage_pct"]),(8,10,8,80))
        self.assertEqual(up["value"],50)
        avg=sum(returns.values())/8
        self.assertAlmostEqual(spread["value"],(sum((r-avg)**2 for r in returns.values())/8)**.5)
        self.assertEqual(up["checked_at"],checked)
        self.assertIn("当前核验名单回看",up["basis"])
        self.assertIn("后续名单回看",factors.board_breadth(members,returns,day,complete=True,membership_as_of="2026-11-19")[0]["basis"])
        for roster,values,complete,reason in [(members,returns,False,"名单"),(members[:-6],returns,True,"5只"),
                (members,{**returns,members[6]["code"]:None,members[7]["code"]:float("nan")},True,"80%")]:
            for result in factors.board_breadth(roster,values,day,complete=complete):
                self.assertIsNone(result["value"])
                self.assertIn(reason,result["missing_reason"])


if __name__ == "__main__":
    unittest.main()
