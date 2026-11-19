"""Exercise the after-close factor-history scheduler against the real asset store in a temp directory."""
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

if sys.platform == "win32":
    raise unittest.SkipTest("The cloud collector uses POSIX locks")

DEPLOY = Path(__file__).resolve().parents[1] / "deploy/tencent-preview"
with patch.object(sys, "path", [str(DEPLOY), *sys.path]):
    import asset_research
    import factor_history_jobs as jobs

NOW = datetime.fromisoformat("2026-11-19T16:00:00+08:00")
DAY = "2026-11-19"
STOCK = "600001.SH"
AI = "399284.SZ"
CHIP = "980017.SZ"
THS = "700051R.TI"
SW = "801010.SI"


class HistoryJobsTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        public = root / "public"
        public.mkdir()
        self.store = asset_research.ResearchStore(public=public,state=root,runtime=root)

    def register(self, *pairs):
        with closing(self.store.db()) as db, db:
            db.executemany("INSERT OR IGNORE INTO assets VALUES(?,?,NULL)", pairs)

    def db_rows(self, query):
        with closing(self.store.db()) as db:
            return [tuple(row) for row in db.execute(query)]

    def bars(self):
        return self.db_rows("SELECT kind,code,day,close,pre_close,pct_chg,source FROM bars ORDER BY kind,code,day")

    def cycle(self, rows, now=NOW, host=0):
        effect = {"side_effect":rows} if isinstance(rows,Exception) or callable(rows) else {"return_value":rows}
        with patch.object(jobs,"local_request",**effect) as fetch, \
             patch.object(jobs,"gate_state",return_value=(host,0,"rate_limited")):
            delay = jobs.run_one(self.store,"fixture",now)
        return delay, fetch.call_args.args if fetch.called else None

    def test_trading_hours_and_preopen_never_create_requests_calendar_off_days_stay_available(self):
        self.register(("index",jobs.BENCHMARK))
        for hour, minute in ((7,0),(9,30),(11,38),(13,0),(14,59)):
            self.assertIsNone(self.cycle([],NOW.replace(hour=hour,minute=minute))[1])
        _, earlier = self.cycle([],NOW.replace(hour=15,minute=0))
        self.assertEqual(earlier[1],"index_daily")
        self.assertEqual(earlier[2]["end_date"],"20261118")
        self.assertIsNone(self.cycle([],NOW.replace(hour=15,minute=44))[1])
        self.assertEqual(self.cycle([],NOW.replace(hour=15,minute=45))[1][2]["end_date"], "20261119")
        self.assertFalse(jobs.eligible(NOW.replace(hour=6,minute=59,second=55)))
        self.assertTrue(jobs.eligible(NOW.replace(hour=6,minute=58)))
        self.assertTrue(jobs.eligible(datetime.fromisoformat("2026-11-21T12:00:00+08:00")))
        self.assertFalse(jobs.eligible(datetime.fromisoformat("2027-01-04T20:00:00+08:00")))
        self.assertEqual(jobs.window(NOW.replace(hour=6))[0],"2026-11-18")
        self.assertEqual(jobs.window(datetime.fromisoformat("2026-11-21T12:00:00+08:00"))[0], "2026-11-20")

    def test_benchmark_windows_exclude_unrelated_dates_codes_and_duplicate_conflicts(self):
        self.register(("index",jobs.BENCHMARK),("sector",CHIP),("sector",AI))
        days=jobs.window(NOW)
        with closing(self.store.db()) as db, db:
            self.store.put_bars(db,"index",jobs.BENCHMARK,[{"date":days[3],"close":123}],"sxdata_rt_idx_k.pre_close")
            db.execute("INSERT INTO watched VALUES(?,?,?)",("sector",AI,NOW.timestamp()))
        def bar(code,day,close,**other):
            return dict(ts_code=code,trade_date=day,close=close,**other)
        rows=[bar(jobs.BENCHMARK,DAY,3450,pre_close=3400,pct_chg=1.47),bar(CHIP,DAY,10),
              bar(jobs.BENCHMARK,days[1],3330),bar(jobs.BENCHMARK,days[1],3329),
              bar(jobs.BENCHMARK,"2026-11-20",3330),bar(jobs.BENCHMARK,days[3],0)]
        delay,call=self.cycle(rows)
        self.assertEqual(call[1:3],("index_daily",{"ts_code":jobs.BENCHMARK,
            "start_date":days[-1].replace("-",""),"end_date":"20261119"}))
        self.assertEqual(delay,jobs.PACE)
        self.assertIn(("index",jobs.BENCHMARK,DAY,3450.0,3400.0,1.47,"index_daily"),self.bars())
        self.assertEqual(len(self.bars()),2)
        self.assertIsNone(self.cycle([],NOW+timedelta(seconds=2))[1])
        self.assertEqual(self.cycle([],NOW+timedelta(seconds=25))[1][2]["ts_code"],AI)
        self.assertEqual(self.cycle([],NOW+timedelta(seconds=50))[1][2]["ts_code"],CHIP)
        _,evening = self.cycle([],NOW+timedelta(hours=3))
        self.assertEqual(evening[2]["ts_code"],AI)

    def test_stored_complete_stock_file_skips_today_one_stock_never_covers_a_market(self):
        self.register(("stock",STOCK),("stock","000001.SZ"),("stock","00002.HK"))
        with closing(self.store.db()) as db, db:
            db.execute("INSERT INTO coverage VALUES(?,?,?,1,?)",("stock",DAY,"equity_cache",5100))
            db.execute("INSERT INTO coverage VALUES(?,?,?,1,?)",("stock","2026-11-18","equity_cache",1))
            self.store.put_bars(db,"stock",STOCK,[{"date":"2026-11-18","close":10}],"sample")
        rows=[dict(ts_code=STOCK,trade_date="20261118",close=12.4,pre_close=12,pct_chg=3.33),
              dict(ts_code="920998.BJ",trade_date="20261118",close=11),
              dict(ts_code="00002.HK",trade_date="20261118",close=10)]
        self.assertEqual(self.cycle(rows)[1][1:3],("daily",{"trade_date":"20261118"}))
        self.assertEqual(self.db_rows("SELECT complete,rows FROM coverage WHERE source='daily'"),[(0,2)])
        self.assertEqual(self.db_rows("SELECT code FROM assets WHERE code='920998.BJ'"),[("920998.BJ",)])
        self.assertTrue(all(row[1]!="00002.HK" for row in self.bars()))
        self.assertEqual(self.cycle([],NOW+timedelta(minutes=2))[1][2], {"trade_date":"20261117"})

    def test_ths_and_shenwan_cross_sections_have_independent_coverage_and_strict_identity(self):
        self.register(("sector",THS),("sector","881121.TI"),("sector",SW))
        target=jobs.window(NOW)[1]
        with closing(self.store.db()) as db, db:
            self.store.put_bars(db,"sector",SW,[{"date":DAY,"close":100}],"sw_daily")
        _, call=self.cycle([{"ts_code":THS,"trade_date":"20261119","close":120,"pct_change":2},
                            {"ts_code":SW,"trade_date":"20261119","close":9999},
                            {"ts_code":"881121.TI","trade_date":"20261119","close":0}])
        self.assertEqual(call[1:3],("ths_daily",{"trade_date":"20261119"}))
        self.assertEqual(self.db_rows("SELECT complete,rows FROM coverage WHERE source='ths_daily'"),[(0,1)])
        self.assertEqual([b[3] for b in self.bars() if b[1]==SW],[100.0])
        self.assertEqual(self.cycle([],NOW+timedelta(minutes=1))[1][2]["trade_date"],target.replace("-",""))
        _,third=self.cycle([{"ts_code":SW,"trade_date":target,"close":130,"pct_change":-0.1}],
                           NOW+timedelta(minutes=2))
        self.assertEqual(third[1],"sw_daily")
        self.assertEqual(self.db_rows("SELECT complete,rows FROM coverage WHERE source='sw_daily' AND complete=1"),[(1,1)])

    def test_recent_boards_use_exact_code_ranges_for_both_providers(self):
        self.register(("sector",THS),("sector",SW))
        recent = NOW.replace(hour=15,minute=15)
        days = jobs.window(recent)
        self.assertEqual(len(days),61)
        with closing(self.store.db()) as db, db:
            db.executemany("INSERT INTO watched VALUES('sector',?,?)",
                           [(THS,recent.timestamp()),(SW,recent.timestamp()-10)])
        for i, (code,other,api) in enumerate(((THS,SW,"ths_daily"),(SW,THS,"sw_daily"))):
            rows = [{"ts_code":code,"trade_date":day,"close":100} for day in days]
            rows.extend([{"ts_code":other,"trade_date":days[0],"close":9999},
                         {"ts_code":code,"trade_date":DAY,"close":9999}])
            # The fair rotation may offer intervening date-wide jobs.
            for step in range(4):
                _,call = self.cycle(lambda token,api,params,active: rows if "ts_code" in params else [],
                                    recent+timedelta(minutes=i*4+step))
                if "ts_code" in call[2]:
                    break
            self.assertEqual(call[1:3],(api,{"ts_code":code,"start_date":days[-1].replace("-",""),
                                             "end_date":"20261118"}))
            self.assertEqual(len([b for b in self.bars() if b[1]==code and b[3]==100]),61)
        self.assertFalse(any(b[3]==9999 for b in self.bars()))
        self.assertFalse(self.db_rows("SELECT day FROM coverage WHERE day LIKE 'sector/%'"))

    def test_newly_viewed_boards_leave_slots_for_index_and_all_board_dates(self):
        self.register(("sector",CHIP))
        calls = []
        for step in range(5):
            now = NOW+timedelta(minutes=step)
            code = str(881100+step)+".TI"
            self.register(("sector",code))
            with closing(self.store.db()) as db, db:
                db.execute("INSERT INTO watched VALUES('sector',?,?)",(code,now.timestamp()))
            calls.append(self.cycle([],now)[1])
        self.assertEqual([c[2].get("ts_code") for c in calls],["881100.TI",None,CHIP,None,"881104.TI"])
        self.assertTrue(all("trade_date" in calls[i][2] for i in (1,3)))

    def test_partial_current_cross_section_retries_at_publication_and_after_midnight(self):
        self.register(("sector",THS),("sector","881121.TI"))
        with closing(self.store.db()) as db, db:
            for code in (THS,"881121.TI"):
                self.store.put_bars(db,"sector",code,[{"date":d,"close":95} for d in jobs.window(NOW)[1:]],"ths_daily")
        first = [{"ts_code":THS,"trade_date":DAY,"close":100}]
        self.assertEqual(self.cycle(first)[1][2],{"trade_date":"20261119"})
        self.assertIsNone(self.cycle([],NOW.replace(hour=19,minute=4))[1])
        for hour in (19,21):
            self.assertEqual(self.cycle([],NOW.replace(hour=hour,minute=5))[1][2],{"trade_date":"20261119"})
        self.assertEqual(self.cycle([],NOW.replace(hour=23))[1][2],{"trade_date":"20261119"})
        after_midnight = NOW+timedelta(hours=9)
        final = [{"ts_code":"881121.TI","trade_date":DAY,"close":102}]
        self.assertEqual(self.cycle(final,after_midnight)[1][2],{"trade_date":"20261119"})
        self.assertEqual([(b[1],b[3]) for b in self.bars() if b[2]==DAY],[(THS,100),("881121.TI",102)])
        self.assertEqual(self.db_rows("SELECT complete,rows FROM coverage WHERE day='2026-11-19'"),[(1,2)])
        self.assertIsNone(self.cycle([],after_midnight+timedelta(minutes=1))[1])

    def test_recheck_partial_indices_next_date_one_task_per_day_and_existing_gaps_only(self):
        self.register(("sector",CHIP))
        days=jobs.window(NOW)
        with closing(self.store.db()) as db, db:
            self.store.put_bars(db,"sector",CHIP,[{"date":d,"close":29} for d in days if d!=days[5]],"index_daily")
            self.store.put_bars(db,"sector",CHIP,[{"date":days[5],"close":10}],"saved_daily")
        _,call=self.cycle([])
        missing=days[5].replace("-","")
        self.assertEqual(call[2],{"ts_code":CHIP,"start_date":missing,"end_date":missing})
        self.assertIsNone(self.cycle([],NOW+timedelta(hours=5))[1])
        next_day=NOW+timedelta(hours=10)
        self.assertIsNotNone(self.cycle([{"ts_code":CHIP,"trade_date":missing,"close":30}],next_day)[1])
        self.assertIsNone(self.cycle([],next_day+timedelta(minutes=10))[1])

    def test_api_cooldown_yields_to_other_sources_and_deferred_claim_can_resume(self):
        self.register(("stock",STOCK),("sector",THS))
        delay,call=self.cycle(jobs._Failure("rate_limited",100,deferred=True))
        self.assertEqual(call[1],"daily")
        self.assertEqual(delay,jobs.PACE)
        self.assertEqual(self.cycle([],NOW+timedelta(seconds=25))[1][1], "ths_daily")
        self.assertEqual(self.cycle([],NOW+timedelta(seconds=101))[1][1:3],("daily",{"trade_date":"20261119"}))
        self.assertEqual(self.db_rows("SELECT count(*) FROM factor_history_jobs WHERE api='daily' AND task='2026-11-19'"),[(1,)])

    def test_host_grace_prevents_cross_api_requests_without_escalating_an_api_only_delay(self):
        self.register(("stock",STOCK),("sector",THS))
        delay,_=self.cycle(jobs._Failure("rate_limited",600,deferred=True),host=45)
        self.assertEqual(delay,45)
        self.assertIsNone(self.cycle([],NOW+timedelta(seconds=44))[1])
        self.assertEqual(self.cycle([],NOW+timedelta(seconds=46))[1][1],"ths_daily")
        delay,_=self.cycle(jobs._Failure("local_rate_limit",90,deferred=True),NOW+timedelta(seconds=100))
        self.assertEqual(delay,90)
        self.assertIsNone(self.cycle([],NOW+timedelta(seconds=150))[1])
