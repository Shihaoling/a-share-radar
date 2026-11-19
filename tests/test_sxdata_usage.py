"""Offline transport and process-level accounting for the production shared gate."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime
from http.client import IncompleteRead
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from pipeline import data_service as gate
from pipeline.equity_data import DailyClient
from pipeline.funds import _Client as FundClient
from pipeline.stock_api import ENDPOINT, _Client, _Failure

NOW = datetime.fromisoformat("2026-11-19T10:00:00+08:00")


class SameGateTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "gate.json"
        env = patch.dict(os.environ, {"RADAR_SERVICE_GATE_FILE": str(self.path)}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_last_legacy_call_counts_once_and_old_window_entries_expire_exactly(self):
        self.path.write_text(json.dumps({"last": 992, "last_api": "rt_idx_k", "request_window": [941]*99}))
        with patch("pipeline.data_service.time.time", return_value=1000):
            self.assertTrue(gate.request_usage()["pending_legacy"])
            self.assertEqual(gate.reserve_request("news", 6, 15)[:3], (1, 0, "local_rate_limit"))
            self.assertEqual(gate.reserve_request("news", 6, 15)[:3], (1, 0, "local_rate_limit"))
            usage = gate.request_usage()
            self.assertEqual(usage["starts_last_60s"],100)
            self.assertEqual(usage["apis"]["rt_idx_k"]["legacy"],1)
            self.assertEqual(usage["apis"]["news"]["deferred"],{"local_rate_limit":2})
        with patch("pipeline.data_service.time.time", return_value=1001):
            self.assertIsNotNone(gate.reserve_request("news",6,15)[3])
            self.assertEqual(gate.request_usage()["starts_last_60s"],2)

    def test_four_processes_share_last_permit_and_a_new_reader_sees_the_same_state(self):
        import time
        self.path.write_text(json.dumps({"request_window": [time.time()-2]*99}))
        command = [sys.executable, "-c", "from pipeline.data_service import reserve_request; "
                   "import json; print(json.dumps(reserve_request('news',6,20)))"]
        cwd = Path(__file__).resolve().parents[1]
        with ThreadPoolExecutor(4) as pool:
            results=list(pool.map(lambda _: json.loads(subprocess.check_output(command,cwd=cwd)),range(4)))
        self.assertEqual(sum(result[3] is not None for result in results),1)
        self.assertEqual(sum(result[2]=="local_rate_limit" for result in results),3)
        usage=gate.request_usage()
        self.assertEqual(usage["starts_last_60s"],100)
        self.assertEqual(usage["apis"]["news"]["starts"],1)

    def test_invalid_existing_gate_blocks_before_the_network(self):
        for broken in ('incomplete gate file', '{"apis":[]}', '{"host_until":NaN}', '{"request_window":{}}'):
            with self.subTest(broken=broken):
                self.path.write_text(broken)
                self.assertEqual(gate.realtime_min_gap(), 15)
                client = _Client("private-test", {}, NOW)
                client.opener = Mock()
                with self.assertRaises(_Failure) as result:
                    client.request("news", {})
                self.assertTrue(result.exception.deferred)
                self.assertEqual(result.exception.code, "gate_unavailable")
                client.opener.open.assert_not_called()
                self.assertEqual(self.path.read_text(), broken)

    def test_early_quote_releases_the_network_lock_while_small_jitter_can_resolve(self):
        self.path.write_text(json.dumps({"last": 1000, "last_accounted": 1000, "last_api": "news"}))
        with patch("pipeline.data_service.time.time", return_value=1002):
            self.assertEqual(gate.reserve_request("rt_k", 5, 10)[:3], (4, 0, "shared_spacing"))
            self.assertEqual(gate.reserve_request("daily", 6, 10)[:3], (0, 4, None))
            self.assertEqual(gate.realtime_min_gap(), 5)
        with patch("pipeline.data_service.time.time", return_value=1005.75):
            self.assertEqual(gate.reserve_request("rt_k", 5, 10)[:3], (0, .25, None))

    def test_true_http_and_business_429s_deferred_and_adaptive_tiers(self):
        def limit(t, api, business=False):
            with patch("pipeline.data_service.time.time",return_value=t):
                client=_Client("private-test",{},NOW)
                client.opener=Mock()
                if business:
                    response=Mock(status=200,headers={})
                    response.geturl.return_value=ENDPOINT
                    response.read.return_value=b'{"code":429,"msg":"private-origin"}'
                    client.opener.open.return_value=nullcontext(response)
                else:
                    client.opener.open.side_effect=HTTPError(ENDPOINT,429,"private-origin",{"Retry-After":"60"},None)
                with self.assertRaises(_Failure) as result:
                    client.request(api,{})
                return result.exception,client.count
        error,count=limit(1000,"rt_idx_k")
        self.assertEqual((error.code,count),("rate_limited",1))
        error,count=limit(1005,"rt_idx_k")
        self.assertEqual((error.deferred,count),(True,0))
        limit(1065,"news")
        self.assertEqual(len(json.loads(self.path.read_text())["recent_rt_429"]),1)
        limit(1130,"rt_idx_k",business=True)
        with patch("pipeline.data_service.time.time",return_value=1190):
            _,_,_,event=gate.reserve_request("rt_idx_k",5,10)
            gate.finish_request("rt_idx_k",event,"ok",12,200)
        with patch("pipeline.data_service.time.time",return_value=1195):
            self.assertEqual(gate.reserve_request("rt_k",5,4)[:3],(5,0,"adaptive_spacing"))
            self.assertEqual(gate.gate_state("news",5)[1],1)
            info=gate.request_usage()
            self.assertEqual(info["realtime_min_gap"],10)
            self.assertEqual(gate.realtime_min_gap(),10)
            self.assertEqual(info["apis"]["rt_idx_k"]["outcomes"],{"http_429":1,"provider_429":1,"ok":1})
        limit(1200,"rt_idx_k")
        with patch("pipeline.data_service.time.time",return_value=1205):
            self.assertEqual(gate.reserve_request("rt_k",5,4)[:3],(55,0,"rate_limited"))
            self.assertEqual(gate.request_usage()["realtime_min_gap"],15)
            self.assertEqual(gate.realtime_min_gap(),15)
        with patch("pipeline.data_service.time.time",return_value=1600):
            self.assertEqual(gate.request_usage()["realtime_min_gap"],10)
        with patch("pipeline.data_service.time.time",return_value=1730):
            self.assertEqual(gate.request_usage()["realtime_min_gap"],5)
        self.assertNotIn("private-", self.path.read_text())

    def test_typed_transport_errors_are_recorded_without_any_exception_text(self):
        cases=[("dns",URLError(socket.gaierror(-2,"private-origin")),False),
               ("connect_refused",URLError(ConnectionRefusedError()),False),
               ("open_timeout",URLError(TimeoutError()),False),
               ("read_timeout",TimeoutError("private-origin"),True),
               ("reset_on_read",ConnectionResetError("private-origin"),True),
               ("incomplete_read",IncompleteRead(b"private-body",1),True)]
        for i,(kind,error,headers_received) in enumerate(cases):
            with self.subTest(kind=kind),patch("pipeline.data_service.time.time",return_value=2000+i*1000):
                client=_Client("private-test",{},NOW)
                client.opener=Mock()
                if headers_received:
                    response=Mock(status=200,headers={})
                    response.geturl.return_value=ENDPOINT
                    response.read.side_effect=error
                    client.opener.open.return_value=nullcontext(response)
                else:
                    client.opener.open.side_effect=error
                with self.assertRaises(_Failure) as caught:
                    client.request("news",{"query":"private-param"})
                self.assertEqual(caught.exception.code,"transport_error")
                event=gate.request_usage()["recent"][-1]
                self.assertEqual((event["transport"],event["outcome"]),(kind,"transport_error"))
                self.assertGreaterEqual(event["duration_ms"],0)
                self.assertTrue(event["finished_at"]>=event["started_at"])
        self.assertNotIn("private-",self.path.read_text())

    def test_daily_and_fund_use_the_same_accounting_and_semantic_limits_stay_distinct(self):
        cases = [
            (2000, DailyClient("test", now=NOW), ("2026-11-18",)),
            (2061, FundClient("test", {}, NOW), ("fund_daily", {})),
            (2122, _Client("test", {}, NOW), ("rt_idx_k", {})),
        ]
        for timestamp, client, args in cases:
            with patch("pipeline.data_service.time.time", return_value=timestamp):
                client.opener = Mock()
                response = Mock(status=200, headers={})
                response.geturl.return_value = ENDPOINT
                response.read.return_value = b'{"code":-2002,"msg":"too many requests"}'
                client.opener.open.return_value = nullcontext(response)
                with self.assertRaises(_Failure):
                    client.request(*args)
        usage = gate.request_usage()
        self.assertEqual([r["api"] for r in usage["recent"]], ["daily", "fund_daily", "rt_idx_k"])
        self.assertEqual([r["outcome"] for r in usage["recent"]], ["provider_rate_limited"] * 3)
        self.assertTrue(all(r["http_status"] == 200 for r in usage["recent"]))
        self.assertFalse(json.loads(self.path.read_text()).get("recent_rt_429"))

    def test_event_payload_is_bounded_while_counts_survive_eviction(self):
        for i in range(90):
            with patch("pipeline.data_service.time.time",return_value=3000+6*i):
                event=gate.reserve_request("news",6,15)[3]
                gate.finish_request("news",event,"ok",25,200)
        usage=gate.request_usage()
        self.assertEqual(len(usage["recent"]),gate.EVENT_LIMIT)
        self.assertEqual(usage["apis"]["news"]["outcomes"]["ok"],90)
        self.assertLess(self.path.stat().st_size,16000)

    def test_recovery_worker_and_minute_api_have_specific_labels(self):
        with patch.object(sys, "argv", ["/opt/site/minute_recovery.py"]):
            event = gate.reserve_request("idx_mins", 6, 15)[3]
            gate.finish_request("idx_mins", event, "ok", 15, 200)
        usage = gate.request_usage()
        self.assertEqual(usage["recent"][0]["worker"], "minute_recovery")
        self.assertEqual(usage["apis"]["idx_mins"]["starts"], 1)
