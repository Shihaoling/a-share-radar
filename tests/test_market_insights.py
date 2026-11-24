"""Test evidence binding, quote selection and supplemental news eligibility."""

from contextlib import closing
from datetime import datetime, timedelta
import json
import os
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
    import weekend_brief as weekend
    import company_announcements as disclosures
    import announcement_analysis as disclosure_analysis
    from relay_common import collection_due

DAY = "2026-11-20"
NOW = datetime.fromisoformat(DAY + "T16:00:00+08:00")


class MarketInsightsTests(unittest.TestCase):
    def test_collection_cadence_respects_market_breaks_and_holidays(self):
        for day, clock, news, announcements in (
            ("2026-11-23", "09:32", True, True),
            ("2026-11-23", "09:37", False, True),
            ("2026-11-23", "11:32", False, False),
            ("2026-11-23", "11:33", False, True),
            ("2026-11-23", "12:10", True, False),
            ("2026-11-23", "13:02", True, True),
            ("2026-11-23", "13:03", False, True),
            ("2026-11-23", "15:03", False, True),
            ("2026-11-22", "10:02", False, False),
            ("2026-10-05", "10:23", False, True),
            ("2026-10-05", "10:22", False, False),
        ):
            with self.subTest(day=day, clock=clock):
                at = datetime.fromisoformat(f"{day}T{clock}:00+08:00")
                self.assertEqual(collection_due(at), news)
                self.assertEqual(collection_due(at, announcements=True), announcements)

    def test_announcement_digest_remains_available_until_newer_batch_is_summarized(self):
        prior_day, current_day = "2026-11-19", DAY
        reference = {"id":"prior", "date":prior_day, "title":"上次公告"}
        digest = {"date":prior_day, "items":[{"text":"参考公告", "ids":["prior"]}],
                  "summary":"最近摘要", "generated_at":NOW.isoformat(), "input_count":1}
        sessions = {prior_day:{"digest":digest,"rows":[reference],"checked_at":NOW.timestamp()},
                    current_day:{"rows":[{"id":"current", "date":current_day}],"checked_at":NOW.timestamp()}}
        with TemporaryDirectory() as folder:
            path = Path(folder)/"company-announcements.json"
            disclosures.publish_announcements(path,sessions,current_day,NOW)
            latest = json.loads(path.read_bytes())
            archived = json.loads((path.with_suffix("")/(current_day+".json")).read_bytes())
        self.assertEqual(latest["digest"], digest)
        self.assertEqual(latest["digest_announcements"], [reference])
        self.assertEqual(latest["announcements"], sessions[current_day]["rows"])
        self.assertIsNone(archived["digest"])

    def test_announcement_digest_tracks_new_ids_even_beyond_the_original_sample(self):
        def item(key, title):
            return {"id":key,"date":DAY,"title":title,"name":"示例公司","code":"600001.SH"}

        current = [item(str(i),"关于股份回购的公告") for i in range(50)]
        original = {"date":DAY,"summary":"已发布的摘要","items":[],"input_count":45,
                    "generated_at":(NOW-timedelta(hours=1)).isoformat(),"fingerprint":"legacy"}
        stamp = NOW
        model_calls = 0
        def model(article, config, key, *, prompt, stage, validator):
            nonlocal model_calls
            model_calls += 1
            supplied = article["digest_articles"]
            self.assertEqual(len(supplied),45)
            self.assertEqual(supplied[0]["id"],"new")
            if model_calls == 1:
                raise disclosures.ModelFailure("http_429",60)
            return validator({"summary":"采纳新增披露的摘要","items":[{"text":"披露董事会安排","ids":["new"]}]})

        with TemporaryDirectory() as folder:
            state = Path(folder)
            (state/"news").mkdir()
            credentials = state/"credentials"
            credentials.mkdir()
            for name in ("sxdata-token","minimax-key"):
                (credentials/name).write_text("test-key")
            private = state/"news/announcements.json"
            private.write_text(json.dumps({DAY:{"digest":original,"rows":current,"checked_at":NOW.timestamp()}}))
            with patch.multiple(disclosures,STATE=state), patch.dict(os.environ,
                  {"CREDENTIALS_DIRECTORY":str(credentials),"RADAR_SCHEDULED_COLLECTION":"0"}), \
                 patch.object(disclosures,"shanghai_now",side_effect=lambda:stamp), \
                 patch.object(disclosures,"collection_days",side_effect=lambda now, sessions:[now.date()]), \
                 patch.object(disclosures,"fetch_day",return_value=(list(reversed(current)),True)) as fetch, \
                 patch.object(disclosures,"infer_article",side_effect=model) as infer:
                disclosures.main()
                infer.assert_not_called()
                self.assertEqual(json.loads(private.read_bytes())[DAY]["digest"],original)
                stamp += timedelta(minutes=10)
                fetch.return_value = (current+[item("new","关于董事会安排的公告")],True)
                disclosures.main()
                self.assertEqual(json.loads(private.read_bytes())[DAY]["digest"],original)
                stamp += timedelta(minutes=10)
                fetch.return_value = ([],True)
                disclosures.main()
                updated = json.loads(private.read_bytes())[DAY]["digest"]
                self.assertEqual(updated["generated_at"],stamp.isoformat())
                self.assertEqual(updated["items"][0]["ids"],["new"])
                stamp += timedelta(minutes=10)
                disclosures.main()
                self.assertEqual(infer.call_count,2)
                self.assertEqual(json.loads(private.read_bytes())[DAY]["digest"],updated)

    def test_disclosure_analysis_keeps_citations_and_directions_bound_to_each_original(self):
        original = {"id":"a","title":"公告甲","text":"公司完成了本次股份回购计划。"}
        title_only = {"id":"b","title":"关于聘请公司审计机构的事项","text":""}
        stock = {"direction":"neutral","reason":"按披露内容作判断"}
        normal = {"id":"a","summary":"回购计划已实施完成","stock":stock,"sector":None,
                  "evidence":"公司完成了本次股份回购计划"}
        second = {"id":"b","summary":"披露审计机构聘任事项","stock":{"direction":"uncertain","reason":"只有标题"},
                  "sector":None,"evidence":title_only["title"]}
        result = disclosure_analysis.validate({"items":[normal,second]},[original,title_only])["analysis"]
        self.assertEqual(result["a"]["basis"],"original")
        self.assertEqual(result["b"]["basis"],"title")
        invalid = {**second,"stock":{**stock,"direction":"positive"}}
        self.assertEqual(set(disclosure_analysis.validate({"items":[normal,invalid]},[original,title_only])["analysis"]),{"a"})
        misquoted = {**normal,"evidence":title_only["title"]}
        with self.assertRaises(ValueError):
            disclosure_analysis.validate({"items":[misquoted]},[original,title_only])

    def test_weekend_brief_uses_distinct_news_windows_and_runs_once_per_three_hour_slot(self):
        saturday = datetime.fromisoformat("2026-11-21T12:30:00+08:00")
        def load_news(at):
            stamp = at.replace(hour=10, minute=0, second=0, microsecond=0)
            return [("n", {"title":"央行报道 "+str(at.date()), "credibility":"reliable",
                           "published_at":stamp.isoformat()})]
        calls = []
        def model(title, packet, key, prompt, validator, *, stage):
            calls.append(packet[0])
            self.assertEqual(stage, "market_weekend_brief")
            return validator({key:{"text":"摘要","refs":["W1","E1"]} for key in weekend.KEYS})
        with TemporaryDirectory() as folder:
            out = Path(folder)/"quote-output"
            for at in (saturday, saturday+timedelta(minutes=25), saturday+timedelta(hours=3), NOW):
                weekend.run(at,out,"test",model=model,load_news=load_news)
            data = json.loads((out/"weekend-brief.json").read_bytes())
        self.assertEqual(len(calls),2)
        self.assertEqual(data["market_through"],"2026-11-20")
        self.assertEqual(data["next_trade_day"],"2026-11-23")
        self.assertEqual(data["sections"]["week_news"]["news"][0]["id"],"W1")
        self.assertEqual(data["sections"]["weekend_news"]["news"][0]["id"],"E1")
        self.assertTrue(all(r["published_at"][:10]<"2026-11-21" for r in calls[0]["week_news"]))
        self.assertTrue(all(r["published_at"][:10]=="2026-11-21" for r in calls[0]["weekend_news"]))

    def test_fixed_slots_use_beijing_clock_and_skip_nontrading_days(self):
        for slot in reading.SLOTS:
            at = datetime.fromisoformat(DAY+"T"+slot+":00+08:00")
            self.assertEqual(reading.due_slot(at, True, ""), slot)
            self.assertEqual(reading.due_slot(at, True, slot), "")
            self.assertEqual(reading.due_slot(at, False, ""), "")
        pre_open = datetime.fromisoformat(DAY+"T09:30:59+08:00")
        self.assertEqual(reading.due_slot(pre_open, True, ""), "")

    def test_weekend_disclosures_keep_period_and_publication_time_distinct(self):
        now = datetime.fromisoformat("2026-11-22T01:30:00+08:00")
        dates = ["2026-11-20","2026-11-21"]
        saved = {}
        for day in dates:
            row = disclosures.normalized({
                "ann_date":day.replace("-",""),"ts_code":"600001.SH","name":"测试公司",
                "title":"关于重组进展的公告","url":"https://static.cninfo.com.cn/report.pdf",
                "rec_time":"2026-11-20 21:54:08",
            }, datetime.fromisoformat(day).date())
            saved[day] = {"rows":[row],"complete":True}
        def model(title, packet, key, prompt, validator, *, stage):
            supplied = packet[0]
            weekend = supplied["weekend_announcements"][0]
            self.assertEqual(weekend["published_at"],"2026-11-20T21:54:08+08:00")
            self.assertEqual(weekend["disclosed_on"],"2026-11-21")
            draft = {"market":{"text":"行情回顾"},"outlook":{"text":"下周观察"},
                     "week_news":{"announcement_text":"工作日披露重组进展","announcement_refs":["AE1","AW1"]},
                     "weekend_news":{"announcement_text":"周末披露重组进展","announcement_refs":["AW1","AE1"]}}
            return validator(draft)
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root/"news").mkdir()
            (root/"news/announcements.json").write_text(json.dumps(saved))
            out = root/"quote-output"
            weekend.run(now,out,"test",model=model,load_news=lambda _:[])
            loaded = json.loads((out/"weekend-brief.json").read_bytes())["sections"]
        self.assertEqual([x["id"] for x in loaded["week_news"]["announcements"]["items"]],["AW1"])
        self.assertEqual([x["id"] for x in loaded["weekend_news"]["announcements"]["items"]],["AE1"])
        self.assertEqual(loaded["weekend_news"]["announcements"]["missing_dates"],["2026-11-22"])
        self.assertIsNone(disclosures.publication_time("2026-11-21",now.date()))

    def test_weekly_return_can_use_an_exactly_matched_trend_window(self):
        valid = {"as_of":"2026-11-20","windows":{"5":{"start_date":"2026-11-13","return_pct":-2.7}}}
        wrong = {**valid,"windows":{"5":{"start_date":"2026-11-12","return_pct":-10}}}
        trend = {"market":[{"id":"sh000001","name":"上证指数",**valid},
                           {"id":"sz399001","name":"深证成指",**wrong}],
                 "boards":[{"id":"801010.SI","name":"农林牧渔","group":"sw",**valid}]}
        rows = {"bars":{"sh000001":{"2026-11-20":{"close":3500}}}}
        evidence = weekend.market_evidence(rows,trend,NOW.date()-timedelta(days=4),NOW.date())
        self.assertEqual(len(evidence["indices"]),1)
        self.assertEqual(evidence["indices"][0]["weekly_return_pct"],-2.7)
        self.assertEqual(evidence["indices"][0]["close"],3500)
        self.assertEqual(evidence["industry_coverage"],1)
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

    def test_levels_use_quarter_release_vintage_and_original_units(self):
        row = {"indicator":"us_gdp","period":"2026 年三季度 · 初值","at":"2026-10-29T20:30:00+08:00"}

        def download(url):
            self.assertIn("vintage_date=2026-10-29&cosd=2026-07-01&coed=2026-07-01",url)
            if "id=GDP&" in url:
                return "observation_date,GDP_20261029\n2026-07-01,33010.036\n"
            return "observation_date,GDPC1_20261125\n2026-07-01,24611.077\n"

        with patch.object(releases,"download",side_effect=download):
            levels = releases.us_release_levels(row)
        self.assertEqual([(m["label"],m["value"],m["unit"]) for m in levels],
                         [("名义 GDP",33010.036,"十亿美元")])

    def test_price_reference_levels_chain_months_and_do_not_fill_a_gap(self):
        rows = [{"indicator":"cn_ppi","period":period,"release_data":{
            "metrics":[{"label":"环比","value":change,"unit":"%"}],"source_url":"https://www.stats.gov.cn/"}}
                for period,change in (("2025 年 11 月",0.2),("2025 年 12 月",1),("2026 年 1 月",-1),("2026 年 3 月",2))]
        releases.collect_release_levels(rows)
        self.assertEqual(rows[0]["release_levels"][0]["value"],100)
        self.assertAlmostEqual(rows[2]["release_levels"][0]["value"],99.99)
        self.assertIn("估算",rows[2]["release_levels"][0]["basis"])
        self.assertNotIn("release_levels",rows[3])

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

        def ask(title, batch, key, prompt, validator, *, stage):
            self.assertEqual(stage,"economic_release")
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
