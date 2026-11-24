from datetime import date, datetime
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy" / "tencent-preview"))

from global_indices import completed_bars, merge
from global_intraday import due, merge_series, session, sina_series, tencent_series, with_previous_close


class GlobalIndexTests(unittest.TestCase):
    def test_us_daily_bar_becomes_usable_after_the_local_close(self):
        rows = [
            {"ts_code": "SPX", "trade_date": "20261120", "close": 4020, "pre_close": 4000},
            {"ts_code": "SPX", "trade_date": "20261121", "close": 5010},
            {"ts_code": "DJI", "trade_date": "20261120", "close": 9000},
        ]
        before = datetime.fromisoformat("2026-11-21T04:59:00+08:00")
        after = datetime.fromisoformat("2026-11-21T05:01:00+08:00")
        self.assertFalse(completed_bars(rows, "SPX", before))
        bars = completed_bars(rows, "SPX", after)
        self.assertEqual(list(bars), ["2026-11-20"])
        self.assertAlmostEqual(bars["2026-11-20"]["change_pct"], .5)

    def test_partial_fetch_preserves_other_markets_and_existing_sessions(self):
        saved = {
            "SPX": {"history": [{"date":"2026-11-19","close":4000}]},
            "HSI": {"history": [{"date":"2026-11-20","close":22100}]},
        }
        result = merge(saved, {"SPX":{"2026-11-20":{"date":"2026-11-20","close":4010}}})
        self.assertEqual(result["HSI"], saved["HSI"])
        self.assertEqual([row["close"] for row in result["SPX"]["history"]], [4000, 4010])
        self.assertEqual(result["SPX"]["market"], "US")

    def test_global_sessions_follow_local_hours_and_known_early_closures(self):
        check = lambda market, stamp: due(market, datetime.fromisoformat(stamp))
        self.assertFalse(check("US", "2026-11-20T22:29:00+08:00"))
        self.assertTrue(check("US", "2026-11-20T22:31:00+08:00"))
        self.assertTrue(check("US", "2026-07-08T21:31:00+08:00"))
        self.assertFalse(check("US", "2026-11-26T22:45:00+08:00"))
        self.assertFalse(check("HK", "2026-11-20T12:30:00+08:00"))
        self.assertEqual(session("US", date(2026, 11, 27))[-1][-1].hour, 13)
        self.assertEqual(session("HK", date(2026, 12, 24))[-1][-1].hour, 12)

    def test_missing_previous_close_uses_only_the_previous_local_session(self):
        entry = {"date": "2026-10-20", "provider": "tencent"}
        friday = {"date": "2026-10-16", "close": 22000, "complete": True}
        older = {"date": "2026-10-15", "close": 22100, "complete": True}
        self.assertEqual(with_previous_close(entry, "HK", [older, friday])["previous_close"], 22000)
        self.assertEqual(with_previous_close(entry, "HK", [older]), entry)
        self.assertEqual(with_previous_close({**entry, "previous_close": 21900}, "HK", [friday])["previous_close"], 21900)

    def test_timestamped_series_keeps_trading_date_while_ignoring_out_of_session_ticks(self):
        now = datetime.fromisoformat("2026-11-22T12:00:00+08:00")
        packet = {"code":0,"data":{"hkHSI":{"data":{"date":"20261120","data":[
            "0920 100","0930 110","1200 115","1230 117","1300 123","1608 125","1831 129",
        ]}}}}
        hk = tencent_series(packet, "hkHSI", now)
        self.assertEqual(hk["date"], "2026-11-20")
        self.assertEqual([row[1] for row in hk["points"]], [110,115,123,125])
        self.assertEqual(hk["points"][-1][0], "2026-11-20T16:08:00+08:00")

        response = {"result":{"status":{"code":0},"data":[[
            {"date":"2026-11-20","m":"09:30:00","price":"4010","prevclose":"3990"},
            {"m":"15:59:00","price":"4050"},{"m":"16:00:00","price":"4060"},{"m":"16:02:00","price":"4070"},
        ]]}}
        us = sina_series(response, "US", now)
        self.assertEqual(us["date"], "2026-11-20")
        self.assertEqual(us["points"][-1], ["2026-11-20T16:00:00-05:00", 4060])
        self.assertEqual(merge_series(us, {**us, "date":"2026-11-19"}), us)
        during = datetime.fromisoformat("2026-11-20T23:00:00+08:00")
        opening = sina_series(response, "US", during)
        self.assertEqual(opening["points"], [["2026-11-20T09:30:00-05:00",4010]])
        self.assertEqual(opening["previous_close"], 3990)
