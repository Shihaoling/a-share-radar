"""Test evidence binding, quote selection and supplemental news eligibility."""

from contextlib import closing
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

if sys.platform == "win32":
    raise unittest.SkipTest("The cloud workers use POSIX file locking")

with patch.object(sys, "path", [str(Path(__file__).resolve().parents[1] / "deploy/tencent-preview"), *sys.path]):
    import market_insights as insights
    import market_reading as reading
    import economic_calendar as calendar

DAY = "2026-11-20"
NOW = datetime.fromisoformat(DAY + "T16:00:00+08:00")


class MarketInsightsTests(unittest.TestCase):
    def test_fixed_slots_use_beijing_clock_and_skip_nontrading_days(self):
        for slot in reading.SLOTS:
            at = datetime.fromisoformat(DAY+"T"+slot+":00+08:00")
            self.assertEqual(reading.due_slot(at, True, ""), slot)
            self.assertEqual(reading.due_slot(at, True, slot), "")
            self.assertEqual(reading.due_slot(at, False, ""), "")
        pre_open = datetime.fromisoformat(DAY+"T09:30:59+08:00")
        self.assertEqual(reading.due_slot(pre_open, True, ""), "")
        self.assertEqual(reading.due_slot(NOW, True, "13:01"), "15:00")

    def test_rapid_move_strict_threshold_rolling_window_and_rearm(self):
        now = datetime.fromisoformat(DAY+"T10:15:00+08:00")
        def at(minutes, value):
            return (now-timedelta(minutes=minutes),value)
        names = {"idx":"上证指数"}
        series = {"idx":[at(31,90),at(30,100),at(0,101)]}
        self.assertEqual(reading.scan_moves(now,series,names,[])[0],[])
        series["idx"][-1] = at(0,98.8)
        alerts, active = reading.scan_moves(now,series,names,[])
        self.assertEqual([r["change_pct"] for r in alerts],[-1.2])
        self.assertEqual(reading.scan_moves(now,series,names,active)[0],[])
        self.assertEqual(reading.scan_moves(now,{"idx":[at(30,100),at(0,99.9)]},names,active)[1],[])
        self.assertEqual(reading.scan_moves(now+timedelta(minutes=3),series,names,[])[0],[])

    def test_market_news_gate_and_summary_limit(self):
        self.assertTrue(reading.major_world_news({"credibility":"trusted","title":"美联储宣布紧急降息50个基点"}))
        self.assertTrue(reading.major_world_news({"credibility":"official","title":"美国宣布新关税即日生效"}))
        self.assertFalse(reading.major_world_news({"credibility":"reliable","title":"交易员预计美联储可能会降息"}))
        self.assertFalse(reading.major_world_news({"credibility":"rumor","title":"美国宣布新关税即日生效"}))
        self.assertEqual(reading.validate({"summary":"主要指数走势分化，行业表现偏弱。","refs":["N1"]},{"N1":{}})["refs"],["N1"])
        complete = "主要指数震荡，成长板块回落，权重板块相对抗跌。板块表现分化，资金痕迹需要跟踪。"
        answer = reading.validate({"summary":complete+"不宜重复过多次要情况。" * 20,"refs":["N1","猜测"]},{"N1":{}})
        self.assertEqual(answer,{"summary":complete,"refs":["N1"]})
        self.assertEqual(len(reading.validate({"summary":"长"*120,"refs":[]},{})["summary"]),100)

    def test_us_release_times_convert_both_daylight_and_standard_time(self):
        page = calendar.SchedulePage("""<table>
          <tr><td>Friday, July 02, 2026</td><td>08:30 AM</td><td>Employment Situation for June 2026</td></tr>
          <tr><td>Thursday, December 10, 2026</td><td>08:30 AM</td><td>Consumer Price Index for November 2026</td></tr>
          </table>""")
        events = list(calendar.us_events(page,2026,"https://www.bls.gov",bls=True))
        self.assertEqual([r["at"] for r in events],["2026-07-02T20:30:00+08:00","2026-12-10T21:30:00+08:00"])
        self.assertEqual(events[1]["period"],"2026 年 11 月")

    def test_current_index_close_and_verified_industry_estimate(self):
        catalog = {"market":[{"id":"sh000001","name":"上证指数"}],
                   "sectors":[{"id":"801780.SI","name":"银行","taxonomy":"SW2021"}]}
        daily = {"bars":{"sh000001":{DAY:{"change_pct":-1}}, "801780.SI":{"2026-11-19":{"change_pct":12}}}}
        live = {"market":[{"id":"sh000001","quote_timestamp":DAY+"T14:50:00+08:00","change_pct":2}]}
        stocks = {"board_estimates":[{"id":"801780.SI","quote_timestamp":DAY+"T15:00:00+08:00",
                                     "estimate_phase":"close","change_pct":3}]}
        data = insights.headline_inputs(NOW, daily, live, stocks, catalog)
        self.assertEqual((data["indices"][0]["change_pct"], data["indices"][0]["basis"]), (-1,"正式收盘"))
        self.assertEqual((data["industries"][0]["change_pct"], data["industries"][0]["basis"]), (3,"盘后权重股估算"))
        self.assertEqual(insights.breadth([{"change_pct": .0001},{"change_pct": 0},{"change_pct": -.015}]),
                         {"up":1,"down":1,"flat":1})
        daily["bars"]["801780.SI"][DAY] = {"change_pct":1}
        data = insights.headline_inputs(NOW, daily, live, stocks, catalog)
        self.assertEqual((data["industries"][0]["change_pct"], data["industries"][0]["basis"]), (1,"正式收盘"))

    def test_theme_payload_binds_each_impact_to_its_own_article(self):
        inputs = {"A1":("first", {"E0001":"生猪供应下降。"}, {}), "A2":("second", {"E0001":"区域降雨增加。"}, {})}
        names = {"885573.TI":"猪肉"}
        row = {"id":"A1","tone":"neutral","impacts":[{"code":"885573.TI","direction":"positive",
                "reason":"供应变化影响猪价。","evidence":["E0001"]}]}
        value = {"items":[row, {"id":"A2","tone":"neutral","impacts":[]}]}
        validated = insights.validate_themes(value, inputs, names)["items"]
        self.assertEqual(validated["A1"]["impacts"][0]["evidence"], ["生猪供应下降。"])
        self.assertEqual(validated["A2"]["impacts"], [])
        row["impacts"][0]["evidence"] = ["E0002"]
        with self.assertRaises(ValueError):
            insights.validate_themes(value, inputs, names)
        with self.assertRaises(ValueError):
            insights.validate_themes({"items": [value["items"][1]]}, inputs, names)

    def test_supplemental_queue_accepts_provider_ids_and_excludes_prior_days(self):
        with TemporaryDirectory() as folder:
            state = Path(folder)
            (state/"news").mkdir()
            with closing(sqlite3.connect(state/"news/news.sqlite3")) as db, db:
                db.execute("CREATE TABLE articles(id TEXT,day TEXT,published TEXT,payload TEXT)")
                entries = [
                    ("stock-api-news-2feb07a48c601f5ac777b8a9", DAY, "official", "14:50"),
                    ("story_yesterday", "2026-11-19", "official", "14:50"),
                    ("story_rumor", DAY, "rumor", "14:50"),
                    ("story_future", DAY, "reliable", "16:50"),
                ]
                for ident, day, source, clock in entries:
                    stamp = day + "T" + clock + ":00+08:00"
                    db.execute("INSERT INTO articles VALUES (?,?,?,?)",
                               (ident,day,stamp,json.dumps({"published_at":stamp,"credibility":source})))
            with patch.object(insights,"STATE",state):
                self.assertEqual([ident for ident,_ in insights.eligible_news(NOW)], [entries[0][0]])
