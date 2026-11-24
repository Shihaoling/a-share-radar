from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/"deploy/tencent-preview")]
from asset_research import ResearchStore
from market_gap_recovery import confirmed_suspensions, run, session

NOW = datetime.fromisoformat("2026-11-22T20:00:00+08:00")
DAY = "2026-11-20"


class MarketGapRecoveryTests(unittest.TestCase):
    def test_only_manual_run_targets_previous_session_on_a_weekend(self):
        self.assertIsNone(session(NOW, "session"))
        self.assertEqual(session(NOW, "latest").isoformat(), DAY)
        self.assertIsNone(session(datetime.fromisoformat("2026-11-20T18:59:59+08:00"), "session"))
        self.assertEqual(session(datetime.fromisoformat("2026-11-20T19:00:00+08:00"), "session").isoformat(), DAY)

    def test_only_full_day_suspensions_for_the_target_date_explain_absence(self):
        base = {"trade_date":"20261120","suspend_timing":None,"suspend_type":"S"}
        rows = [{**base,"ts_code":"600000.SH"}, {**base,"ts_code":"600001.SH","suspend_timing":"09:30-10:00"},
                {**base,"ts_code":"600002.SH","trade_date":"20261119"}, {**base,"ts_code":"600003.SH","suspend_type":"R"}]
        self.assertEqual(confirmed_suspensions(rows,DAY),{"600000.SH"})

    def test_fills_only_requested_dated_gaps_and_skips_complete_feeds(self):
        source = {"market":[]}
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ResearchStore(root, root/"public", root/"runtime")
            names = root/"sector-directory/stock-names.json"
            names.parent.mkdir()
            names.write_text(json.dumps({"600000.SH":"已覆盖","600001.SH":"缺日线"}))
            store.out.mkdir()
            (store.out/"valuation-stock.json").write_text(json.dumps(
                {"trade_date":DAY,"items":{"600000.SH":{},"600001.SH":{}}}))
            with closing(store.db()) as db, db:
                db.execute("INSERT INTO assets VALUES('sector','885959.TI','PCB')")
                db.execute("INSERT INTO bars VALUES('stock','600000.SH',?,20,19,5,'daily')",(DAY,))
            calls = []
            def request(api, params):
                calls.append((api,params))
                if api == "suspend_d":
                    return []
                if api == "daily":
                    return [{"ts_code":"600000.SH","trade_date":"20261120","close":999},
                            {"ts_code":"600001.SH","trade_date":"20261120","close":13,"pct_chg":1.2}]
                if api == "ths_daily":
                    return [{"ts_code":"885959.TI","trade_date":"20261120","close":1100,"pct_change":-2.0}]
                raise AssertionError(api)
            result = run(root,source,DAY,NOW,request)
            self.assertEqual([api for api,_ in calls],["suspend_d","daily","ths_daily"])
            self.assertEqual(result["daily"]["stock"],{"expected":2,"missing":[],"new":1})
            self.assertEqual(result["daily"]["ths"]["new"],1)
            self.assertFalse(result["valuation_missing"]["stock"])
            with closing(store.db()) as db:
                self.assertEqual(db.execute("SELECT close FROM bars WHERE code='600000.SH'").fetchone()[0],20)
            self.assertEqual(json.loads((store.out/"sector-reference.json").read_text())["quotes"]["885959.TI"]["price"],1100)
            calls.clear()
            run(root,source,DAY,NOW,request)
            self.assertEqual(calls,[])


if __name__ == "__main__":
    unittest.main()
