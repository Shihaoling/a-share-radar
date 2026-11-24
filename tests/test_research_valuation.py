from datetime import datetime
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy" / "tencent-preview"))

from research_valuation import asset_metrics, collect, normalized_rows, read_packet

DAY = "2026-11-20"


class ValuationTests(unittest.TestCase):
    def test_units_conflicts_missing_income_and_sample_weights(self):
        day = "20261120"
        rows = [
            dict(ts_code="600001.SH", trade_date=day, total_mv=100000, pe_ttm=10, pb=2),
            dict(ts_code="600002.SH", trade_date=day, total_mv=300000, pe_ttm=15, pb=3),
            dict(ts_code="600003.SH", trade_date=day, total_mv=200000, pe_ttm=20, pb=2),
            dict(ts_code="600004.SH", trade_date=day, total_mv=200000, pe_ttm=None, pb=-2),
            dict(ts_code="600005.SH", trade_date=day, total_mv=200000, pe_ttm=0, pb=float("inf")),
            dict(ts_code="600006.SH", trade_date=day, total_mv=200000, pe_ttm=30, pb=4),
            dict(ts_code="600006.SH", trade_date=day, total_mv=200000, pe_ttm=31, pb=4),
        ]
        items = normalized_rows("stock", rows, DAY)
        self.assertNotIn("600006.SH", items)
        self.assertEqual(items["600001.SH"]["market_cap"], 10)
        self.assertNotIn("pe_ttm", items["600004.SH"])
        data = {"trade_date": DAY, "items": items}
        roster = {"status": "ready", "members": [{"code": code} for code in items]}
        pe, pb, total = asset_metrics("sector", "885001.TI", DAY, lambda _: data, roster)
        self.assertEqual(pe["value"], 15)
        self.assertEqual(pe["coverage_pct"], 60)
        self.assertAlmostEqual(pb["value"], 80 / 15)
        self.assertEqual(total["value"], 100)
        self.assertIsNone(asset_metrics("sector", "885001.TI", DAY, lambda _: data, {**roster, "status":"partial"})[0]["value"])
        self.assertIsNone(asset_metrics("sector", "885001.TI", "2026-11-19", lambda _: data, roster)[0]["value"])

    def test_dated_collection_skips_same_session_after_success(self):
        from pipeline.stock_api import FIELDS
        original_fields = FIELDS.copy()
        self.addCleanup(FIELDS.update, original_fields)
        self.addCleanup(FIELDS.clear)
        now = datetime(2026, 11, 21, 18, tzinfo=ZoneInfo("Asia/Shanghai"))
        attempted = []
        def request(api, params):
            attempted.append(api)
            return [{"ts_code":f"{n:06d}.SH", "trade_date":params["trade_date"], "total_mv":100000000}
                    for n in range(6)] if api == "index_dailybasic" else []
        with TemporaryDirectory() as tmp:
            output = Path(tmp)
            state = {}
            collect(output, "test", now, attempts=state, request=request)
            first = read_packet(output / "valuation-index.json")
            collect(output, "test", now, attempts=state, request=request)
        self.assertEqual(attempted, ["index_dailybasic", "sw_daily"])
        self.assertEqual(first["trade_date"], DAY)
        self.assertEqual(first["items"]["000000.SH"]["market_cap"], 1)
