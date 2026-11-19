"""Offline checks for exact-code lists, dated publications and the shared SXData gate."""
from contextlib import closing, nullcontext
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

if sys.platform == "win32":
    raise unittest.SkipTest("The directory worker uses POSIX file locks")

ROOT = Path(__file__).resolve().parents[1]
with patch.object(sys, "path", [str(ROOT / "deploy/tencent-preview"), *sys.path]):
    import sector_directory as directory
from pipeline import stock_api

DAY = "2026-11-19"
PRIOR = "2026-11-18"
NOW = datetime.fromisoformat(DAY + "T14:30:00+08:00")
INDEX = "980017.SZ"
SW = "801010.SI"
TOTAL = "700051R.TI"
PRICE = "700051.TI"
CONCEPTS = [str(885100 + i) + ".TI" for i in range(10)]


class ConstituentsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.public = self.root / "public"
        self.public.mkdir()
        directory.publish(self.public / "sector-catalog.json", {"items": [{"code": INDEX, "name": "国证芯片"}]})
        catalog = json.loads((ROOT / "web/data/ths-sector-catalog.json").read_text())
        actual = {item["code"]: item for item in catalog["items"]}
        ths = [dict(actual[code], member_count=2) for code in (TOTAL, PRICE)]
        ths.extend({"code": code, "name": "概念" + str(i), "kind": "N"} for i, code in enumerate(CONCEPTS))
        directory.publish(self.public / "ths-sector-catalog.json", {"items": ths,
                          "sw_items": [{"code": SW, "kind": "L1", "name": "农林牧渔"}]})
        self.store = directory.Directory(self.public, self.root)

    def job(self, code):
        with closing(self.store.connect()) as db:
            return dict(db.execute("SELECT * FROM jobs WHERE code=?", (code,)).fetchone())

    def apply(self, code, rows, day=DAY, now=NOW):
        job = self.job(code)
        with closing(self.store.connect()) as db, db:
            self.store.apply(db, job, rows, day, now)

    def publication(self, name):
        return json.loads((self.store.out / (name + ".json")).read_bytes())

    def constituents(self, code):
        return self.publication("sector-members/" + code)

    def heat(self, stamp=NOW, codes=CONCEPTS):
        return [dict(ts_code=c, rank=i, trade_date=stamp.strftime("%Y%m%d"), rank_time=stamp.isoformat(),
                     data_type="概念板块", hot=1000 - i, pct_change=-0.5, current_price=0 if i==1 else 1300 + i)
                for i, c in enumerate(codes, 1)]

    def test_true_total_return_identifier_in_queue_and_pending_http_response(self):
        self.assertIn(TOTAL, self.store.known)
        self.assertEqual(directory.api_for(TOTAL), "ths_member")
        self.assertTrue(self.store.demand(TOTAL, NOW))
        self.assertFalse(self.store.demand("700051R.SZ", NOW))
        self.assertEqual(self.store.params(TOTAL, 0, DAY, NOW)["ts_code"], TOTAL)
        # Capture the request handler without creating a server socket or worker thread.
        handlers = []
        def server(_path, handler):
            handlers.append(handler)
            return nullcontext(Mock())
        with patch.dict(os.environ, RUNTIME_DIRECTORY=str(self.root)), patch.object(directory, "UnixStreamServer", server), \
             patch.object(directory.os, "chmod"), patch.object(directory.threading, "Thread"), \
             patch.object(self.store, "work", side_effect=SystemExit), self.assertRaises(SystemExit):
            directory.serve(self.store, "fixture")
        request = Mock(path="/data/sector-members/" + TOTAL + ".json", wfile=io.BytesIO())
        handlers[0].do_GET(request)
        request.send_response.assert_called_once_with(202)
        self.assertEqual(json.loads(request.wfile.getvalue())["code"], TOTAL)

    def test_latest_complete_single_heat_snapshot_filters_future_other_universes_and_zero_prices(self):
        first = self.heat(NOW - timedelta(minutes=8))
        next_incomplete = self.heat(NOW - timedelta(minutes=1))[:9]
        mixed = [*first, *next_incomplete, dict(self.heat(NOW - timedelta(minutes=1))[-1], data_type="行业板块"),
                 *self.heat(NOW + timedelta(minutes=15))]
        self.assertTrue(self.store.finish_special("@hot", mixed, DAY, NOW))
        public = self.publication("sector-hot")
        self.assertEqual([row["code"] for row in public["items"]], CONCEPTS)
        self.assertEqual([row["rank"] for row in public["items"]], list(range(1, 11)))
        self.assertEqual(public["rank_time"], first[0]["rank_time"])
        self.assertNotIn("price", public["items"][0])
        self.assertEqual(public["items"][0]["change_pct"], -0.5)
        self.assertFalse(self.store.finish_special("@hot", self.heat(NOW - timedelta(hours=1)), DAY, NOW))
        self.assertEqual(self.publication("sector-hot"), public)

    def test_daily_reference_excludes_mismatched_dates_merges_late_coverage_and_moves_day_cleanly(self):
        def row(code, day, close):
            return {"ts_code": code, "trade_date": day.replace("-", ""), "close": close, "pct_change": 1}
        one, two = CONCEPTS[:2]
        self.assertTrue(self.store.finish_special("@daily",
            [row(one, PRIOR, 101), row(two, DAY, 202), row(PRICE, PRIOR, 0)], PRIOR, NOW))
        self.assertEqual(set(self.publication("sector-reference")["quotes"]), {one})
        self.assertTrue(self.store.finish_special("@daily", [row(two, PRIOR, 150)], PRIOR, NOW))
        self.assertEqual(set(self.publication("sector-reference")["quotes"]), {one, two})
        self.assertTrue(self.store.finish_special("@daily", [row(two, DAY, 200)], DAY, NOW))
        new = self.publication("sector-reference")
        self.assertEqual(new["quotes"], {two: {"quote_date": DAY, "price": 200, "change_pct": 1}})
        self.assertFalse(self.store.finish_special("@daily", [row(one, PRIOR, 102)], PRIOR, NOW))
        self.assertEqual(self.publication("sector-reference"), new)

    def test_worker_revisits_same_day_when_reference_arrivals_are_late(self):
        first, late = CONCEPTS[:2]

        def row(code, price):
            return {"ts_code": code, "trade_date": PRIOR, "close": price, "pct_change": 1}

        self.store.finish_special("@daily", [row(first, 110)], PRIOR, NOW)
        with closing(self.store.connect()) as db, db:
            db.execute("UPDATE jobs SET due=? WHERE code!='@daily'", (NOW.timestamp() + 7200,))
        with patch.object(directory, "local_request", return_value=[row(late, 210)]) as request, \
             patch.object(directory, "previous_trading_day", return_value=NOW.date() - timedelta(days=1)), \
             patch.object(directory, "shanghai_now", return_value=NOW), patch("builtins.print"):
            self.store.work("fixture", NOW)
        self.assertEqual(request.call_args.args[1:3], ("ths_daily", {"trade_date": "20261118"}))
        self.assertEqual(set(self.publication("sector-reference")["quotes"]), {first, late})

    def test_index_weight_pagination_survives_restart_selects_only_latest_exact_board(self):
        def weight(board, stock, day, pct):
            return dict(index_code=board, con_code=stock, trade_date=day.replace("-", ""), weight=pct)
        with patch.object(directory, "SIZE", 2):
            self.apply(INDEX, [weight(INDEX, "600001.SH", DAY, 45), weight(INDEX, "600008.SH", PRIOR, 100)])
            self.assertFalse((self.store.members / (INDEX + ".json")).exists())
            self.store = directory.Directory(self.public, self.root)
            cursor = self.job(INDEX)["cursor"]
            self.assertEqual(self.store.params(INDEX, cursor, DAY, NOW)["offset"], 2)
            self.apply(INDEX, [weight("000685.SH", "600005.SH", DAY, 100), weight(INDEX,"600002.SH", DAY, 55)])
            self.apply(INDEX, [])
        table = self.constituents(INDEX)
        self.assertEqual((table["code"], table["method"], table["as_of"], table["status"]),
                         (INDEX, "index_weight", DAY, "ready"))
        self.assertEqual({r["code"]: r["weight_pct"] for r in table["members"]},
                         {"600001.SH":45, "600002.SH":55})

    def test_ths_constituents_exclude_other_codes_and_preserve_name_without_inventing_weights(self):
        def member(board, stock, name):
            return {"ts_code":board, "con_code":stock, "con_name":name}
        with patch.object(directory, "SIZE", 2):
            self.apply(TOTAL, [member(TOTAL,"600001.SH","样本一"),member(PRICE,"600002.SH","价格指数公司")])
            self.apply(TOTAL, [member(TOTAL,"000001.SZ","样本二")])
        table=self.constituents(TOTAL)
        self.assertEqual((table["code"],table["method"],table["status"]), (TOTAL,"ths_member","ready"))
        self.assertEqual({row["name"] for row in table["members"]},{"样本一","样本二"})
        self.assertTrue(all(row["weight_pct"] is None for row in table["members"]))
        self.assertFalse((self.store.members / (PRICE + ".json")).exists())

    def test_sw_hierarchy_pagination_applies_board_effective_dates_and_current_membership(self):
        def member(code, stock, **attrs):
            return dict(l1_code=code,ts_code=stock,name=stock,is_new="Y",**attrs)
        with patch.object(directory, "SIZE", 3):
            self.apply(SW, [member(SW,"600001.SH",in_date="20200101"),member("801020.SI","600002.SH"),
                            member(SW,"600003.SH",out_date="20261119")])
            self.apply(SW, [member(SW,"600004.SH",in_date="20261125"),member(SW,"600005.SH",out_date=None)])
        items=self.constituents(SW)
        self.assertEqual(items["method"],"index_member_all")
        self.assertEqual({r["code"] for r in items["members"]},{"600001.SH","600005.SH"})

    def test_incomplete_equal_length_result_and_repeated_pages_preserve_ready_reference(self):
        def weight(stock, pct):
            return dict(index_code=INDEX, con_code=stock, trade_date=DAY, weight=pct)
        old=[weight("600001.SH",60),weight("000001.SZ",40)]
        self.apply(INDEX, old)
        ready=self.constituents(INDEX)
        self.assertEqual(ready["status"],"ready")
        self.apply(INDEX,[weight("600001.SH",25),weight("600003.SH",None)])
        self.assertEqual(self.constituents(INDEX), ready)
        with patch.object(directory, "SIZE", 2):
            self.apply(INDEX, old)
            self.apply(INDEX, list(reversed(old)))
        self.assertEqual(self.job(INDEX)["cursor"],0)
        self.assertEqual(self.constituents(INDEX), ready)

    def test_demand_preempts_background_without_bypassing_endpoint_cooldown(self):
        now=NOW.timestamp()
        with closing(self.store.connect()) as db, db:
            db.execute("UPDATE jobs SET due=? WHERE code LIKE '@%'", (now+7200,))
        self.store.demand(PRICE,NOW)
        with patch.object(directory,"local_request",side_effect=directory._Failure("rate_limited",97,deferred=True)) as call, \
             patch.object(directory,"gate_state",return_value=(97,0,"rate_limited")):
            self.store.work("fixture",NOW)
        self.assertEqual(call.call_args.args[1:3],("ths_member",{"ts_code":PRICE,"offset":0,"limit":directory.SIZE}))
        self.assertEqual(self.job(PRICE)["failures"],0)
        self.assertGreaterEqual(self.job(CONCEPTS[-1])["due"],now+97)
        self.store.demand(CONCEPTS[0],NOW)
        with closing(self.store.connect()) as db:
            self.assertIn(self.store.scheduling(db,now)["code"],[SW, INDEX])

    def make_due(self, codes, now=NOW):
        with closing(self.store.connect()) as db, db:
            db.execute("UPDATE jobs SET due=?", (now.timestamp()+8*86400,))
            db.executemany("UPDATE jobs SET due=0 WHERE code=?", ((code,) for code in codes))

    def fetch(self, rows, now=NOW):
        with patch.object(directory, "local_request", return_value=rows) as request, \
             patch.object(directory, "shanghai_now", return_value=now), patch("builtins.print"):
            self.store.work("fixture", now)
        return request.call_args.args if request.called else None

    def test_columnless_empty_membership_only_defers_the_single_board(self):
        self.make_due([PRICE])
        gate = self.root / "gate.json"
        body = io.BytesIO(b'{"code":0,"data":{"fields":[],"items":[]}}')
        body.headers = {}
        body.geturl = lambda: stock_api.ENDPOINT
        opener = Mock()
        opener.open.return_value = body
        with patch.dict(os.environ, RADAR_SERVICE_GATE_FILE=str(gate)), \
             patch.object(stock_api.urllib.request, "build_opener", return_value=opener), \
             patch.object(directory, "shanghai_now", return_value=NOW):
            self.store.work("fixture", NOW)
        self.assertEqual(opener.open.call_count, 1)
        membership = self.constituents(PRICE)
        self.assertEqual((membership["reason"], membership["members"]), ("no_rows", []))
        job = self.job(PRICE)
        self.assertEqual((job["failures"], job["due"]), (0, NOW.timestamp() + directory.PARTIAL_RETRY_SECONDS))
        usage = json.loads(gate.read_bytes())
        self.assertEqual(usage["usage"]["apis"]["ths_member"]["outcomes"], {"ok":1})
        self.assertNotIn("ths_member", usage.get("apis", {}))

    def test_columnless_or_malformed_nonempty_payloads_still_fail_validation(self):
        for api, data in [("rt_k", {"fields":[], "items":[]}), ("ths_member", {"fields":None, "items":[]}),
                          ("ths_member", {"fields":[], "items":[[]]}), ("ths_member", {"fields":[], "items":None}),
                          ("ths_member", {"fields":["ts_code"], "items":[]})]:
            with self.subTest(api=api, data=data), self.assertRaises(stock_api._Failure) as failure:
                stock_api._rows({"code":0, "data":data}, api)
            self.assertEqual(failure.exception.code, "invalid_payload")

    @staticmethod
    def ths_row(code, stock):
        return {"ts_code": code, "con_code": stock, "con_name": "测试样本"}

    def test_bulk_ths_exact_groups_omit_unrelated_and_retry_only_missing_or_dirty_codes_singly(self):
        missing, dirty, valid = TOTAL, CONCEPTS[0], PRICE
        self.make_due([missing, dirty, valid])
        args = self.fetch([self.ths_row(valid, "600001.SH"), self.ths_row(valid, "600002.SH"),
                           self.ths_row(dirty, "600003.SH"), self.ths_row(dirty, "invalid"),
                           self.ths_row(CONCEPTS[-1], "000001.SZ")])
        self.assertEqual(args[1], "ths_member")
        self.assertEqual(set(args[2]["ts_code"].split(",")), {missing, dirty, valid})
        self.assertEqual({x["code"] for x in self.constituents(valid)["members"]}, {"600001.SH","600002.SH"})
        self.assertFalse((self.store.members/(CONCEPTS[-1]+".json")).exists())
        for code in (missing, dirty):
            self.assertFalse((self.store.members/(code+".json")).exists())
            self.assertEqual(self.job(code)["single_only"], 1)
        self.store = directory.Directory(self.public, self.root)
        first = self.fetch([], NOW + timedelta(seconds=60))[2]["ts_code"]
        second = self.fetch([], NOW + timedelta(seconds=120))[2]["ts_code"]
        self.assertEqual({first, second}, {missing, dirty})
        for code in (first, second):
            payload = self.constituents(code)
            self.assertEqual((payload["status"],payload["reason"],payload["members"]), ("partial","no_rows",[]))
            self.assertTrue(payload["checked_at"])
            self.assertEqual(self.job(code)["single_only"], 0)
        due = NOW + timedelta(seconds=directory.PARTIAL_RETRY_SECONDS+120)
        self.assertIsNone(self.fetch([], NOW + timedelta(minutes=5)))
        self.assertEqual(set(self.fetch([], due)[2]["ts_code"].split(",")), {first,second})

    def test_bulk_weight_publishes_latest_per_code_and_recovers_short_weights_singly(self):
        second = "000685.SH"
        path = self.public / "sector-catalog.json"
        items=json.loads(path.read_bytes())["items"]
        directory.publish(path, {"items":[*items,{"code":second,"name":"指数二"}]})
        self.store = directory.Directory(self.public,self.root)
        self.make_due([INDEX, second])
        def row(board, stock, day, weight):
            return {"index_code":board,"con_code":stock,"trade_date":day,"weight":weight}
        raw = [row(INDEX,"600001.SH",PRIOR,100),row(INDEX,"600002.SH",DAY,100),
               row(second,"000001.SZ",DAY,9),row("399284.SZ","000009.SZ",DAY,100)]
        args = self.fetch(raw)
        self.assertEqual(args[1], "index_weight")
        self.assertEqual(set(args[2]["index_code"].split(",")),{INDEX,second})
        self.assertEqual([m["code"] for m in self.constituents(INDEX)["members"]],["600002.SH"])
        self.assertFalse((self.store.members/(second+".json")).exists())
        single = self.fetch([raw[2]], NOW+timedelta(seconds=60))
        self.assertEqual(single[2]["index_code"],second)
        self.assertEqual(self.constituents(second)["reason"],"partial_weights")
        self.assertEqual(self.constituents(second)["members"][0]["weight_pct"],9)

    def test_full_batch_restarts_at_single_offset_zero_then_resumes_per_board_cursor(self):
        self.make_due([PRICE,TOTAL])
        with patch.object(directory, "SIZE", 2):
            self.fetch([self.ths_row(PRICE,"600001.SH"),self.ths_row(TOTAL,"600002.SH")])
            self.assertEqual(self.job(PRICE)["cursor"],0)
            self.assertFalse((self.store.members/(PRICE+".json")).exists())
            self.store.demand(PRICE,NOW)
            first = self.fetch([self.ths_row(PRICE,"600001.SH"),self.ths_row(PRICE,"600003.SH")], NOW+timedelta(seconds=60))
            self.assertEqual((first[2]["ts_code"],first[2]["offset"]),(PRICE,0))
            self.store = directory.Directory(self.public,self.root)
            next_page = self.fetch([],NOW+timedelta(seconds=120))
            self.assertEqual((next_page[2]["ts_code"],next_page[2]["offset"]),(PRICE,2))
        self.assertEqual({x["code"] for x in self.constituents(PRICE)["members"]},{"600001.SH","600003.SH"})
        self.assertEqual(self.job(TOTAL)["single_only"],1)

    def test_unattributed_rows_or_batch_transport_failure_leave_all_jobs_for_independent_recheck(self):
        self.make_due([PRICE,TOTAL])
        self.fetch([self.ths_row(PRICE,"600001.SH"), {"con_code":"600002.SH"}])
        self.assertEqual([self.job(code)["single_only"] for code in (PRICE,TOTAL)],[1,1])
        with closing(self.store.connect()) as db, db:
            db.execute("UPDATE jobs SET single_only=0")
        with patch.object(directory, "local_request", side_effect=directory._Failure("invalid_payload")), \
             patch.object(directory, "gate_state",return_value=(0,0,"")):
            self.store.work("fixture",NOW)
        for code in (PRICE,TOTAL):
            job=self.job(code)
            self.assertEqual(job["single_only"],1)
            self.assertGreater(job["due"],NOW.timestamp())
            self.assertEqual(job["failures"],1)

    def test_background_batch_limits_demand_isolation_and_retry_with_shared_budget(self):
        from pipeline import data_service

        self.make_due(CONCEPTS)
        self.store.demand(CONCEPTS[-1],NOW)
        self.assertEqual(self.fetch([self.ths_row(CONCEPTS[-1],"600001.SH")])[2]["ts_code"], CONCEPTS[-1])
        now = NOW + timedelta(seconds=60)
        with closing(self.store.connect()) as db:
            job=self.store.scheduling(db,now.timestamp())
            batch=self.store.background_batch(db,job,now.timestamp())
        self.assertEqual(len(batch), directory.BATCH_CODES)
        gate=self.root/"rate-gate.json"
        directory.publish(gate, {"request_window":[now.timestamp()-20]*100})
        with patch.dict(os.environ, RADAR_SERVICE_GATE_FILE=str(gate)), \
             patch.object(data_service.time,"time",return_value=now.timestamp()), patch.object(directory,"_Client") as client:
            self.store.work("fixture", now)
        client.assert_not_called()
        for item in batch:
            self.assertGreaterEqual(self.job(item["code"])["due"],now.timestamp()+40)
            self.assertEqual(self.job(item["code"])["failures"],0)
            self.assertFalse((self.store.members/(item["code"]+".json")).exists())

    def test_held_partial_or_complete_members_survive_empty_individual_checks(self):
        self.apply(PRICE,[self.ths_row(PRICE,"600001.SH")])
        partial=self.constituents(PRICE)
        self.assertEqual(partial["reason"],"missing_members")
        self.apply(PRICE,[])
        retained=self.constituents(PRICE)
        self.assertEqual(retained["members"],partial["members"])
        self.assertEqual(retained["reason"],"previous_members")
        self.apply(PRICE,[self.ths_row(PRICE,"600001.SH"),self.ths_row(PRICE,"600002.SH")])
        complete=self.constituents(PRICE)
        self.assertEqual(complete["status"],"ready")
        self.apply(PRICE,[])
        self.assertEqual(self.constituents(PRICE),complete)

    def test_real_shared_budget_exhaustion_rejects_the_101st_request(self):
        from pipeline import data_service

        gate = self.root / "shared-gate.json"
        stamp = NOW.timestamp()
        directory.publish(gate, {"request_window": [stamp - 50 + i / 4 for i in range(100)]})
        with patch.dict(os.environ, RADAR_SERVICE_GATE_FILE=str(gate)), \
             patch.object(data_service.time, "time", return_value=stamp), patch.object(directory, "_Client") as client:
            with self.assertRaises(directory._Failure) as caught:
                directory.local_request("fixture", "ths_member", {"ts_code": PRICE}, True)
            self.assertEqual(caught.exception.code, "local_rate_limit")
            self.assertEqual(caught.exception.retry_seconds, 10)
            self.assertTrue(caught.exception.deferred)
            client.assert_not_called()
        self.assertEqual(len(json.loads(gate.read_text())["request_window"]), 100)

    def test_shared_gate_fails_closed_before_client_creation_on_cooldown_or_busy_slot(self):
        with patch.dict(os.environ, RADAR_SERVICE_GATE_FILE=str(self.root / "gate.json")), \
             patch.object(directory,"_Client") as client:
            for state, reason in [((91,0,"host_rate_limited"),"host_rate_limited"),((0,5,""),"relay_busy")]:
                with self.subTest(state=state), patch.object(directory,"gate_state",return_value=state):
                    with self.assertRaises(directory._Failure) as caught:
                        directory.local_request("fixture","index_weight",{},True)
                    self.assertEqual(caught.exception.code,reason)
                    self.assertTrue(caught.exception.deferred)
            client.assert_not_called()
