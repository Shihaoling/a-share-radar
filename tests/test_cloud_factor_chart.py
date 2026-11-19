from datetime import date, timedelta
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from pipeline.calendar import is_trading_day

DEPLOY = Path(__file__).resolve().parents[1] / "deploy/tencent-preview"
with patch.object(sys, "path", [str(DEPLOY), *sys.path]):
    from factor_chart import build_daily_chart
    from research_factors import compute


class FactorChartTests(unittest.TestCase):
    def setUp(self):
        day = date(2026,11,18)
        days = []
        while len(days) < 125:
            if is_trading_day(day):
                days.append(day.isoformat())
            day -= timedelta(days=1)
        self.days = days[::-1]
        self.index = {day:{"close":100+i+(i%3)*10} for i,day in enumerate(self.days)}
        self.stock = []
        for i,day in enumerate(self.days):
            scale = .5 if i>=90 else 1
            self.stock.append({"date":day,"close":self.index[day]["close"]*scale,
                "pre_close":self.index[self.days[max(0,i-1)]]["close"]*scale,"source":"equity_cache"})

    def test_rolling_values_match_cards_for_every_visible_session_with_warmup(self):
        chart = build_daily_chart("stock",self.stock,self.index,self.days)
        self.assertEqual((chart["basis"],len(chart["rows"])),("reference_linked",60))
        self.assertEqual(chart["anchors"],{self.days[-1]:self.stock[-1]["close"]})
        for i,row in enumerate(chart["rows"],len(self.days)-60):
            # A stock split halves an unadjusted close, while the linked series remains continuous.
            self.assertAlmostEqual(row["close"],self.index[row["day"]]["close"]*.5)
            self.assertEqual(row["raw_close"],self.stock[i]["close"])
            self.assertFalse(row["gap"])
            values = {r["key"]:r["value"] for r in compute("stock",{},{},self.stock,self.index,sessions=self.days[:i+1])}
            for chart_key,key in (("momentum20","momentum20_pct"),("volatility20","volatility20_pct"),
                                  ("beta60","beta60"),("drawdown60","drawdown60_pct")):
                self.assertAlmostEqual(row[chart_key],values[key],places=9)
            self.assertAlmostEqual((row["close"]/row["ma20"]-1)*100,values["ma20_bias_pct"])
        dd=chart["drawdown"]
        self.assertLess(dd["peak_day"],dd["trough_day"])
        self.assertAlmostEqual(dd["pct"],chart["rows"][-1]["drawdown60"])
        self.assertAlmostEqual((1-dd["trough_price"]/dd["peak_price"])*100,dd["pct"])

    def test_missing_sessions_and_reference_break_the_plot_without_joining_independent_anchors(self):
        del self.stock[95]["pre_close"]
        removed=self.stock.pop(83)
        chart=build_daily_chart("stock",self.stock,self.index,self.days)
        rows={r["day"]:r for r in chart["rows"]}
        missing=rows[removed["date"]]
        self.assertTrue(missing["gap"])
        self.assertTrue(all(missing[k] is None for k in ("close","raw_close","ma20","drawdown60","beta60")))
        self.assertTrue(rows[self.days[84]]["gap"])
        self.assertTrue(rows[self.days[95]]["gap"])
        self.assertFalse(rows[self.days[96]]["gap"])
        self.assertEqual(set(chart["anchors"]),{self.days[82],self.days[94],self.days[-1]})
        self.assertIsNone(chart["drawdown"])
        self.assertIsNone(rows[self.days[-1]]["beta60"])

    def test_missing_benchmark_only_affects_beta_and_empty_and_flat_series_have_no_drawdown_annotation(self):
        chart=build_daily_chart("sector",self.index,{},self.days)
        self.assertEqual(chart["basis"],"index_close")
        for row in chart["rows"]:
            self.assertEqual(row["close"],self.index[row["day"]]["close"])
            self.assertIsNone(row["beta60"])
            self.assertIsNotNone(row["volatility20"])
        flat=build_daily_chart("index",{d:{"close":10} for d in self.days},self.index,self.days)
        self.assertEqual(flat["rows"][-1]["drawdown60"],0)
        self.assertIsNone(flat["drawdown"])
        empty=build_daily_chart("index",None,{},[])
        self.assertEqual(empty["rows"],[])
        self.assertIsNone(empty["as_of"])


if __name__ == "__main__":
    unittest.main()
