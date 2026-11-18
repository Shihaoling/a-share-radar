"""Offline coverage for the Tencent constituent and weight refresh rotations."""
from contextlib import nullcontext
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

if sys.platform == "win32":
    raise unittest.SkipTest("Tencent Cloud workers use POSIX locks")

with patch.object(sys, "path", [str(Path(__file__).resolve().parents[1] / "deploy/tencent-preview"), *sys.path]):
    import cloud_research as research
    import market_reference as weights

NOW = datetime.fromisoformat("2026-11-19T17:00:00+08:00")
OLD = (NOW - timedelta(days=30)).isoformat()


class ReferenceRotation(unittest.TestCase):
    def test_late_flows_merge_without_losing_prior_fields_or_counting_preblocked_calls(self):
        now = NOW.replace(hour=19)
        stocks = {"000001.SZ","000002.SZ"}
        def row(symbol, **fields):
            return {"ts_code":symbol, "trade_date":now.strftime("%Y%m%d"), **fields}
        prior = {"trade_date":"2026-11-18", "stocks":{"000001.SZ":{"net_mf_amount":6}}}
        client = Mock()
        client.request.side_effect = [
            research._Failure("rate_limited", 120, deferred=True),
            [],
            [row("000001.SZ", net_mf_amount=0, buy_lg_amount=3)],
            [row("000001.SZ", sell_lg_amount=2), row("000002.SZ", net_mf_amount=1)],
        ]
        with patch.object(research, "relay_client", return_value=nullcontext(client)):
            for offset, status, covered, attempts in [
                (0,"rate_limited",0,0), (1,"rate_limited",0,0), (3,"empty",0,1),
                (15,"empty",0,1), (25,"partial",1,2), (50,"ready",2,3), (80,"ready",2,3),
            ]:
                prior = research.fetch_flows("fixture",now+timedelta(minutes=offset),prior,stocks)
                self.assertEqual((prior["status"],prior["covered_stocks"],prior["attempts"]),(status,covered,attempts))
                if not covered:
                    self.assertEqual(prior["trade_date"],"2026-11-18")
        self.assertEqual(client.request.call_count,4)
        self.assertEqual(prior["stocks"]["000001.SZ"],{"net_mf_amount":0,"buy_lg_amount":3,"sell_lg_amount":2})

    def test_persistently_incomplete_flow_uses_only_three_daily_calls(self):
        now = NOW.replace(hour=19)
        client = Mock()
        client.request.return_value = [{"ts_code":"000001.SZ","trade_date":"20261119","net_mf_amount":3}]
        state = {}
        with patch.object(research, "relay_client", return_value=nullcontext(client)):
            for i in range(6):
                state = research.fetch_flows("fixture",now+timedelta(minutes=30*i),state,{"000001.SZ","000002.SZ"})
        self.assertEqual(client.request.call_count,3)
        self.assertEqual((state["status"],state["covered_stocks"],state["eligible_stocks"]),("partial",1,2))

    def test_weights_do_not_replace_old_payload_with_empty_or_stale_results(self):
        with TemporaryDirectory() as directory:
            state = Path(directory)
            output = state / "relay/industry-weights.json"
            output.parent.mkdir()
            (state / "sxdata-token").write_text("fixture")
            old = {"date": "2026-11-18", "stocks": [{"ts_code": "600000.SH", "weight_pct": 3}],
                   "checked_at": OLD}
            output.write_text(json.dumps({"A": old, "B": old, "C": old}))
            client = Mock()
            client.request.side_effect = [
                weights._Failure("transport_error", 7200),
                [{"index_code": "B", "trade_date": "20261117", "con_code": "600002.SH", "weight": 2}],
                [{"index_code": "C", "trade_date": "20261119", "con_code": "600001.SH", "weight": 5}],
                [],
            ]
            with patch.multiple(weights, STATE=state, INDUSTRIES={"A","B","C"}), \
                 patch.dict(weights.os.environ, {"CREDENTIALS_DIRECTORY": directory}), \
                 patch.object(weights, "relay_client", return_value=nullcontext(client)), \
                 patch.object(weights, "shanghai_now", side_effect=[NOW,NOW+timedelta(minutes=30)]):
                weights.update(2)
                weights.update(2)
            cache=json.loads(output.read_text())
            self.assertEqual([call.args[1]["index_code"] for call in client.request.call_args_list],["A","B","C","B"])
            for code in ("A","B"):
                self.assertEqual(cache[code]["date"],old["date"])
                self.assertEqual(cache[code]["stocks"],old["stocks"])
                self.assertEqual(cache[code]["checked_at"],OLD)
                self.assertGreater(cache[code]["retry_at"],NOW.isoformat())
            self.assertEqual(cache["C"]["date"],"2026-11-19")
            self.assertEqual(cache["C"]["stocks"][0]["weight_pct"],5)

    def test_members_filter_before_budget_and_preserve_prior_complete_snapshot(self):
        sectors=[{"id":"legacy","taxonomy":"old"}]+[{"id":code,"taxonomy":"SW2021"} for code in "ABC"]
        dashboard={"sectors":sectors}
        record={"ts_code":"600000.SH","name":"原成分"}
        old={"checked_at":OLD,"fetched_at":OLD,"records":[record],"complete":True}
        cache={"A":{**old,"checked_at":NOW.isoformat()},"B":old,"C":old}
        replacement={"ts_code":"600001.SH","name":"新成分","is_new":"Y","l1_code":"C",
                     "l2_code":"801001.SI","l3_code":"851001.SI","l1_name":"一级","l2_name":"二级","l3_name":"三级"}
        client=Mock()
        client.request.side_effect=[
            [dict(replacement,l1_code="B"),dict(replacement,ts_code="bad",l1_code="B")],
            [replacement],[],research._Failure("transport_error",600),
        ]
        with patch.object(research,"relay_client",return_value=nullcontext(client)):
            research.refresh_members(dashboard,"test",NOW,cache)
            research.refresh_members(dashboard,"test",NOW+timedelta(minutes=30),cache)
        codes=[call.args[1]["l1_code"] for call in client.request.call_args_list]
        self.assertEqual(codes,["B","C","A","B"])
        self.assertTrue(cache["C"]["complete"])
        self.assertEqual(cache["C"]["records"][0]["ts_code"],"600001.SH")
        self.assertEqual(cache["C"]["fetched_at"],NOW.isoformat())
        for code in "AB":
            self.assertEqual(cache[code]["records"],[record])
            self.assertEqual(cache[code]["fetched_at"],OLD)

    def test_deferred_shared_gate_preserves_record_and_does_not_try_next_member(self):
        dashboard={"sectors":[{"id":code,"taxonomy":"SW2021"} for code in "AB"]}
        cache={}
        client=Mock()
        client.request.side_effect=research._Failure("authorization_denied",86400,deferred=True)
        with patch.object(research,"relay_client",return_value=nullcontext(client)):
            research.refresh_members(dashboard,"test",NOW,cache)
        self.assertEqual(client.request.call_count,1)
        self.assertEqual(cache,{})

    def test_weights_on_actual_host_cooldown_preserve_attempt_and_stop_batch(self):
        with TemporaryDirectory() as directory:
            state=Path(directory)
            (state/"sxdata-token").write_text("fixture")
            client=Mock()
            client.request.side_effect=weights._Failure("http_error",3600,host=True)
            with patch.multiple(weights,STATE=state,INDUSTRIES={"A","B"}), \
                 patch.dict(weights.os.environ,{"CREDENTIALS_DIRECTORY":directory}), \
                 patch.object(weights,"relay_client",return_value=nullcontext(client)), \
                 patch.object(weights,"shanghai_now",return_value=NOW):
                weights.update(2)
            cache=json.loads((state/"relay/industry-weights.json").read_text())
            self.assertEqual(client.request.call_count,1)
            self.assertEqual(cache["A"]["retry_at"],(NOW+timedelta(hours=1)).isoformat())
            self.assertNotIn("B",cache)
