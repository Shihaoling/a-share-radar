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
    import economic_releases as releases

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

    def test_calendar_keeps_past_releases_and_prefers_confirmed_dates(self):
        def release(indicator, period, day, confirmed=False):
            row = {"indicator":indicator,"period":period,"at":day+"T09:30:00+08:00",
                   "source_url":"https://www.stats.gov.cn/schedule"}
            if confirmed:
                row["release_url"] = "https://www.stats.gov.cn/release"
            return row

        confirmed = release("cn_cpi","2026 年 10 月","2026-11-09",True)
        ppi = release("cn_ppi","2026 年 10 月","2026-11-09")
        updated = release("cn_cpi","2026 年 11 月","2026-12-10")
        rows = [confirmed, ppi,
                release("cn_cpi","2026 年 11 月","2026-12-09"),
                release("cn_cpi","2026 年 10 月","2026-11-12"),
                release("cn_cpi","2025 年 9 月","2025-10-10"), updated]
        self.assertEqual(calendar.calendar_entries(NOW,rows),[confirmed,ppi,updated])

    def test_nbs_calendar_handles_merged_month_cells(self):
        tail = "".join(f"<td>{day}/五</td>" for day in range(22,31))
        page = calendar.SchedulePage(f"""<table>
          <tr><td>4</td><td>采购经理指数月度报告</td><td>31/六</td><td rowspan="2">……</td>
              <td>4/三 注5<br>31/二</td>{tail}</tr>
          <tr>{"<td>9:30</td>"*11}</tr>
          <tr><td>11</td><td>社会消费品零售总额月度报告</td><td>19/一</td><td rowspan="2">……</td>
              <td>16/一</td>{tail}</tr>
          <tr>{"<td>10:00</td>"*11}</tr></table>""")
        rows = list(calendar.nbs_events(page,2026,"https://www.stats.gov.cn/sj/fbrc/"))
        march = [(r["at"],r["period"]) for r in rows if r["at"].startswith("2026-03")]
        self.assertEqual(march,[("2026-03-04T09:30:00+08:00","2026 年 2 月"),
                                ("2026-03-31T09:30:00+08:00","2026 年 3 月"),
                                ("2026-03-16T10:00:00+08:00","2026 年 1–2 月")])

    def test_bea_full_schedule_preserves_published_links(self):
        page = calendar.SchedulePage("""<table>
          <tr><th>Year 2026</th><th></th><th>Release</th><th></th></tr>
          <tr><td>September 30 8:30 AM</td><td>N ews</td><td>GDP (Third Estimate), 2nd Quarter 2026</td>
              <td><a href="/news/2026/gdp">View</a></td></tr>
          <tr><td>November 25 8:30 AM</td><td>N ews</td><td>Personal Income and Outlays, October 2026</td><td></td></tr>
          </table>""")
        gdp, pce = calendar.us_events(page,2026,calendar.BEA_FULL)
        self.assertEqual((gdp["at"],gdp["period"],gdp["release_url"]),
                         ("2026-09-30T20:30:00+08:00","2026 年二季度 · 三次估计","https://www.bea.gov/news/2026/gdp"))
        self.assertEqual(pce["period"],"2026 年 10 月")
        self.assertNotIn("release_url",pce)

    def test_release_values_keep_frequency_and_release_period(self):
        cn = "2026年10月份，全国居民消费价格同比上涨0.7%。10月份，全国居民消费价格环比持平。"
        self.assertEqual(releases.chinese_points("cn_cpi","2026 年 10 月",cn)[:2],
                         [{"label":"同比","value":0.7,"unit":"%"},{"label":"环比","value":0.0,"unit":"%"}])
        ppi = "2026年3月份，全国工业生产者出厂价格同比由上月下降0.9%转为上涨0.5%；环比上涨1.0%。"
        self.assertEqual([m["value"] for m in releases.chinese_points("cn_ppi","2026 年 3 月",ppi)],[0.5,1.0])
        retail = "1—10月份，社会消费品零售总额416464亿元，同比增长1.0%。10月份，社会消费品零售总额46463亿元，同比增长0.4%。"
        self.assertEqual([m["value"] for m in releases.chinese_points("cn_retail","2026 年 10 月",retail)],[46463,0.4])
        self.assertFalse(releases.chinese_points("cn_retail","2026 年 8 月",retail))
        pce = ("From the preceding month, the PCE price index for September increased 0.5 percent. "
               "Excluding food and energy, the PCE price index increased 0.3 percent. "
               "From the same month one year ago, the PCE price index for September increased 3.6 percent. "
               "Excluding food and energy, the PCE price index increased 3.1 percent from one year ago.")
        self.assertEqual([m["value"] for m in releases.english_points("us_pce","2026 年 9 月",pce)],[0.5,3.6,0.3,3.1])

    def test_alfred_uses_published_vintage_and_omits_missing_comparison(self):
        def download(url):
            series = url.split("id=")[1].split("&")[0]
            return f"observation_date,{series}_20261110\n2026-09-01,100\n2026-10-01,100.4\n"
        with patch.object(releases,"download",side_effect=download):
            run_at = datetime.fromisoformat("2026-11-10T21:30:00+08:00")
            data = releases.alfred_result("us_cpi","2026 年 10 月",run_at)
        self.assertEqual([(m["label"],m["value"]) for m in data["metrics"]],
                         [("CPI 环比（季调）",0.4),("核心 CPI 环比（季调）",0.4)])
        self.assertIn("vintage_date=2026-11-10",data["source_url"])

    def test_release_analysis_can_use_only_the_news_for_its_own_record(self):
        source = {"id":"N1","url":"https://example.org/release","title":"消息"}
        inputs = {"R1":{"news":[source]},"R2":{"news":[]}}
        reading = {"summary":"核对本次数据的变化。","impact":"可能影响增长判断。","refs":["N1"]}
        value = {"items":{"R1":reading,"R2":{**reading,"refs":[]}}}
        self.assertEqual(calendar.validate_release_analysis(value,inputs)["items"]["R1"]["references"],[source])
        value["items"]["R2"]["refs"] = ["N1"]
        with self.assertRaises(ValueError):
            calendar.validate_release_analysis(value,inputs)

    def test_invalid_model_batch_is_split_and_valid_results_are_kept(self):
        events = [{"indicator":"cn_cpi","at":DAY+"T09:30:00+08:00","period":f"第{i}期",
                   "release_data":{"metrics":[{"label":"同比","value":i,"unit":"%"}]}} for i in (1,2)]
        output = {"events":events,"indicators":{"cn_cpi":{"country":"中国","name":"CPI"}}}
        sizes = []

        def ask(title, batch, key, prompt, validator):
            sizes.append(len(batch))
            if len(batch)>1:
                raise calendar.ModelFailure("invalid_analysis_invalid_json")
            return validator({"items":{batch[0]["id"]:{"summary":"本期消费价格小幅上升。",
                                                       "impact":"可继续观察分项变化。","refs":[]}}})

        with patch.object(calendar,"ask",side_effect=ask),patch.object(calendar,"news_for_release",return_value=[]), \
             patch.object(calendar,"publish"),patch("builtins.print"):
            calendar.update_release_analysis(output,[],"unused",NOW,Path("unused.json"))
        self.assertEqual(sizes,[2,1,1])
        self.assertTrue(all(row.get("release_analysis",{}).get("summary") for row in events))

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
