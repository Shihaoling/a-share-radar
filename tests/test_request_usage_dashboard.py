"""Local-only observation of the shared relay and actual MiniMax request boundary."""
from contextlib import closing, nullcontext
from datetime import datetime
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from pipeline import minimax_news as model
from pipeline.usage_meter import counts,connect,model_start,model_finish
DEPLOY = Path(__file__).resolve().parents[1]/"deploy/tencent-preview"
with patch.object(sys,"path",[str(DEPLOY),*sys.path]):
    import request_usage as usage


def when(clock):
    return datetime.fromisoformat("2026-11-19T"+clock+"+08:00").timestamp()


def event(seq,api,clock,outcome="ok"):
    return dict(id=seq,api=api,started_at=when(clock),outcome=outcome)


class UsageTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.db = self.state/"request-usage"/"metrics.sqlite3"
        env = patch.dict(os.environ,{"RADAR_REQUEST_USAGE_DB":str(self.db)},clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.gate = dict(since=when("09:29:00"),recent=[],peak_60s=2,starts_last_60s=0,apis={})
        live = patch.object(usage,"live_gate",side_effect=lambda:self.gate)
        live.start()
        self.addCleanup(live.stop)

    def take(self,clock):
        usage.sample(self.db,self.gate,when(clock))

    def result(self,clock):
        return usage.dashboard(when(clock),self.state)

    def test_finished_late_once_and_zero_minutes_use_wall_clock(self):
        self.gate["recent"]=[event(1,"news","09:29:55"),event(2,"rt_idx_k","09:30:01","in_flight")]
        self.gate["apis"]={"news":{"starts":1,"outcomes":{"ok":1}},"rt_idx_k":{"starts":1,"outcomes":{}}}
        self.take("09:30:15")
        self.gate["recent"][1]["outcome"]="http_429"
        self.gate["apis"]["rt_idx_k"]["outcomes"]["http_429"]=1
        self.take("09:32:30")
        self.take("09:32:40")
        result = self.result("09:32:50")
        self.assertEqual(result["relay"]["periods"]["trading"]["avg_requests_per_minute"],.5)
        self.assertEqual(result["relay"]["periods"]["other"]["avg_requests_per_minute"],1)
        self.assertEqual(result["relay"]["apis"]["news"]["periods"]["trading"]["requests"],0)
        per = result["relay"]["apis"]["rt_idx_k"]["periods"]["trading"]
        self.assertEqual((per["minutes"],per["requests"],per["failed"]),(2,1,1))
        self.assertEqual(result["relay"]["observed"]["requests"],2)
        self.assertEqual(result["relay"]["cumulative"]["failed"],1)
        usage.publish_sources(self.state,result)
        public = json.loads((self.db.parent/"connected-sources.json").read_text())
        self.assertEqual(set(public["sources"][0]),{"id","name","last_success_at","cooling_down"})
        self.assertEqual(public["sources"][0]["id"],"sxdata")

    def test_overwritten_ring_leaves_a_gap_and_partial_first_minutes_excluded(self):
        self.gate["recent"]=[event(21,"news","11:28:19")]
        self.take("11:30:00")
        self.gate["recent"]=[event(98,"news","12:58:20"),event(99,"idx_mins","13:00:12")]
        self.take("13:02:10")
        periods = self.result("13:02:10")["relay"]["periods"]
        self.assertEqual((periods["other"]["minutes"],periods["other"]["requests"]),(1,0))
        self.assertEqual((periods["trading"]["minutes"],periods["trading"]["requests"]),(3,1))
        self.assertEqual(self.result("13:02:10")["relay"]["observed"]["requests"],3)
        with closing(connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM receipts").fetchone()[0],2)

    def test_primary_totals_use_route_tags_and_late_receipts_only_count_once(self):
        self.gate["recent"] = [
            event(7,"news","09:29:12"),
            {**event(8,"news","09:29:55"),"route":"primary"},
            {**event(9,"news","09:31:02","http_429"),"route":"backup"},
            {**event(10,"anns_d","09:31:06","in_flight"),"route":"primary"},
        ]
        self.gate["apis"] = {"news":{"starts":8,"routes":{"primary":3,"backup":2},"outcomes":{"ok":4}},
                              "anns_d":{"starts":1,"routes":{"primary":1},"outcomes":{}}}
        self.gate["starts_last_60s"] = 2
        self.take("09:31:10")
        self.gate["recent"][-1]["outcome"] = "transport_error"
        self.take("09:31:15")
        self.take("09:32:15")
        current = self.result("09:31:15")
        self.assertEqual(current["primary"]["rolling_60s"],1)
        self.assertEqual(current["primary"]["cumulative"]["requests"],4)
        self.assertEqual(current["relay"]["cumulative"]["requests"],9)
        final = self.result("09:32:15")
        primary = final["primary"]
        self.assertEqual(primary["apis"]["news"]["outcomes"],{"ok":1})
        self.assertEqual(primary["apis"]["anns_d"]["outcomes"],{"transport_error":1})
        self.assertEqual(primary["periods"]["trading"]["requests"],1)
        self.assertEqual(primary["periods"]["trading"]["failed"],1)
        source = final["sources"][0]
        self.assertEqual(source["route"],"primary")
        self.assertIsNone(source["expires_at"])

    def model_request(self,body,*,training=False,error=False):
        response = Mock()
        response.read.return_value = json.dumps(body).encode()
        opener = Mock()
        if error:
            opener.open.side_effect=HTTPError(model.ENDPOINT,429,"secret-upstream",{},
                                              io.BytesIO(json.dumps(body).encode()))
        else:
            opener.open.return_value=nullcontext(response)
        argv = ["training_news.py" if training else "cloud_news.py"]
        with patch.object(sys,"argv",argv),patch.object(model.urllib.request,"build_opener",return_value=opener):
            return model.infer_article({"full_text":"secret-article","title":"secret-title"},{},
                                       "secret-key",prompt="secret-prompt",validator=lambda row:row)

    def test_rolling_minimax_has_exact_second_boundaries_and_pending_receipts(self):
        self.take("08:00:00")
        with patch("pipeline.usage_meter.clock.time",return_value=when("08:59:29")):
            older = model_start("economic_release")
            model_finish(older,True,{"prompt_tokens":7,"completion_tokens":2,"total_tokens":9})
        with patch("pipeline.usage_meter.clock.time",return_value=when("08:59:30")):
            boundary = model_start("market_reading")
            model_finish(boundary,False,{"total_tokens":6})
            model_finish(boundary,False,{"total_tokens":6})
        with patch("pipeline.usage_meter.clock.time",return_value=when("09:00:00")):
            model_start("trend_brief")
        recent = self.result("09:00:30")["minimax"]["rolling"]
        self.assertEqual((recent["60"]["requests"],recent["60"]["total_tokens"],recent["60"]["token_receipts"]),(2,6,1))
        self.assertEqual(recent["3600"]["total_tokens"],15)
        self.assertEqual(recent["86400"]["observed_seconds"],3630)
        self.assertIsNone(recent["60"]["scopes"]["trend_brief"]["total_tokens"])
        self.assertEqual(recent["60"]["scopes"]["news_analysis"]["total_tokens"],0)

    def test_stage_note_reaches_actual_outbound_system_prompt(self):
        from pipeline.minimax_prompts import save_note
        path = self.state/"minimax-prompts.json"
        save_note(path,"economic_release","补充：优先解释指标构成。",0)
        opener = Mock()
        response = Mock()
        response.read.return_value = b'{"choices":[{"finish_reason":"stop","message":{"content":"{}"}}]}'
        opener.open.return_value = nullcontext(response)
        with patch.dict(os.environ,{"RADAR_MINIMAX_PROMPTS_FILE":str(path)}), \
             patch.object(model.urllib.request,"build_opener",return_value=opener):
            for stage in ("economic_release","market_reading"):
                model.infer_article({"full_text":"article"},{}, "key",prompt="固定格式",
                                    validator=lambda row:row,stage=stage)
        outgoing = [json.loads(call.args[0].data)["messages"][0]["content"] for call in opener.open.call_args_list]
        self.assertTrue(outgoing[0].startswith("固定格式"))
        self.assertIn("优先解释指标构成",outgoing[0])
        self.assertEqual(outgoing[1],"固定格式")

    def test_per_stage_effort_reaches_model_and_preserves_legacy_shared_note(self):
        from pipeline.minimax_prompts import ANSWER_STAGE, PLAN_STAGE, REVIEW_STAGE, save_note
        path = self.state/"minimax-prompts.json"
        path.write_text(json.dumps({"research_assistant":{"note":"原来的说明","revision":3}}))
        opener = Mock()
        response = Mock()
        response.read.return_value = b'{"choices":[{"finish_reason":"stop","message":{"content":"{}"}}]}'
        opener.open.return_value = nullcontext(response)
        stages = (PLAN_STAGE, ANSWER_STAGE, "news_digest", "market_reading", "market_weekend_brief")
        with patch.dict(os.environ, {"RADAR_MINIMAX_PROMPTS_FILE":str(path)}), \
             patch.object(model.urllib.request,"build_opener",return_value=opener):
            for stage in stages:
                model.infer_article({"full_text":"article"},{}, "key",prompt="固定格式",
                                    validator=lambda row:row,stage=stage)
            save_note(path,ANSWER_STAGE,"新的回答说明",0,reasoning_effort="high")
            save_note(path,PLAN_STAGE,"新的规划说明",0,reasoning_effort="max")
            save_note(path,ANSWER_STAGE,"仅修改说明",1)
            model.infer_article({"full_text":"article"},{}, "key",stage=ANSWER_STAGE,
                                prompt="固定格式",validator=lambda row:row)
            save_note(path,REVIEW_STAGE,"复核说明",0,reasoning_effort="low")
            for stage in (ANSWER_STAGE, PLAN_STAGE, REVIEW_STAGE):
                model.infer_article({"full_text":"article"},{"answer_effort":"low"},"key",stage=stage,
                                    prompt="固定格式",validator=lambda row:row)
        sent = [json.loads(call.args[0].data) for call in opener.open.call_args_list]
        self.assertEqual([row["reasoning_effort"] for row in sent],
                         ["medium","xhigh","medium","medium","medium","high","low","max","max"])
        self.assertTrue(all("原来的说明" in row["messages"][0]["content"] for row in sent[:2]))
        self.assertIn("仅修改说明",sent[-3]["messages"][0]["content"])
        self.assertIn("复核说明",sent[-1]["messages"][0]["content"])
        self.assertEqual(json.loads(path.read_text())[PLAN_STAGE]["reasoning_effort"],"max")

    def test_raw_usage_from_rejected_outputs_and_http_errors_model_scopes_and_privacy(self):
        self.take("09:29:00")
        tokens = {"prompt_tokens":35,"completion_tokens":2,"total_tokens":37,"secret-field":"secret-body"}
        with patch("pipeline.usage_meter.clock.time",return_value=when("09:45:01")):
            with self.assertRaises(model.ModelFailure):
                self.model_request({"usage":tokens,"choices":[{"finish_reason":"length"}]},training=True)
            with self.assertRaises(model.ModelFailure):
                self.model_request({"usage":{"total_tokens":1}},error=True)
            self.model_request({"usage":{},"choices":[{"finish_reason":"stop","message":{"content":"{}"}}]})
            with self.assertRaises(model.ModelFailure):
                model.infer_article({"full_text":"abc"},{"max_input_characters":1},"private")
        self.take("09:46:05")
        data = self.result("09:46:05")["minimax"]
        self.assertEqual((data["observed"]["requests"],data["observed"]["success"],data["observed"]["failed"]),(3,1,2))
        self.assertEqual((data["observed"]["usage_responses"],data["observed"]["total_tokens"]),(2,38))
        self.assertEqual(data["scopes"]["training_labels"]["input_tokens"],35)
        self.assertEqual(data["scopes"]["news_analysis"]["failed"],1)
        period = data["periods"]["trading"]
        self.assertEqual((period["requests"],period["minutes"]),(3,7))
        self.assertNotIn("secret",json.dumps(data))
        with closing(connect(self.db)) as db:
            self.assertNotIn("secret"," ".join(db.iterdump()))

    def test_model_startup_transition_unknown_and_persisted_archived_and_lifetime(self):
        self.take("09:30:30")
        with closing(connect(self.db)) as db,db:
            counts(db,when("09:31:00"),"minimax","news_analysis",start=True,success=True,usage={"total_tokens":0})
        self.take("09:32:05")
        start=self.result("09:32:05")["minimax"]
        self.assertIsNone(start["periods"]["trading"]["avg_requests_per_minute"])
        self.assertEqual(start["observed"]["total_tokens"],0)
        future=when("09:32:05")+usage.HOUR_RETENTION+86400
        with closing(connect(self.db)) as db,db:
            usage.set_value(db,"model_through",future)
            usage.set_value(db,"relay_through",future)
        usage.sample(self.db,self.gate,future)
        with closing(connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM meter WHERE grain!='all'").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT requests FROM meter WHERE grain='all'").fetchone()[0],1)

    def test_historical_receipts_report_unknown_news_failures_and_training_batches(self):
        for parent in ("news","training-news"):
            (self.state/parent).mkdir()
        with closing(sqlite3.connect(self.state/"news/news.sqlite3")) as db,db:
            db.execute("CREATE TABLE model_usage(at,input_tokens,output_tokens,total_tokens)")
            db.execute("INSERT INTO model_usage VALUES('2026-11-18T10:00:00+08:00',12,1,13)")
        with closing(sqlite3.connect(self.state/"training-news/dataset.sqlite3")) as db,db:
            db.execute("CREATE TABLE usage(at,input_tokens,output_tokens,total_tokens,outcome)")
            db.executemany("INSERT INTO usage VALUES('2026-11-18T10:00:00+08:00',?,?,?,?)",
                           [(8,2,10,"partial"),(None,None,None,"transport_error"),(None,None,None,"input_over_budget")])
        data=self.result("09:40:50")
        past=data["minimax"]["historical"]
        self.assertEqual(past["news_analysis"]["total_tokens"],13)
        self.assertIsNone(past["news_analysis"]["failed"])
        self.assertEqual((past["training_labels"]["requests"],past["training_labels"]["failed"]),(2,1))
        self.assertIsNone(data["minimax"]["observed"])
        self.assertEqual([row["id"] for row in data["sources"]],["minimax"])

    def test_public_training_progress_allowlists_nested_fields_and_preserves_last_valid_snapshot(self):
        input_path = self.state/"training-status"/"progress.json"
        input_path.parent.mkdir()
        raw = dict(schema_version=1,start_day="2026-11-18",end_day_exclusive="2026-11-19",
                   total_days=1,audited_days=1,collected=10,labelled=7,generated_at="2026-11-19T15:00:00+08:00",
                   usage={"calls":123,"total_tokens":456},collection_attempts=789,label_error="secret",
                   latest_collection={"checked_at":"2026-11-18T16:00:00+08:00","requests":100},
                   label_rate={"minutes":30,"articles":7,"calls":5},
                   days=[{"day":"2026-11-18","windows":1,"verified_windows":1,"collected":10,"labelled":7,
                          "label_retry_pending":3,"usage":{"input_tokens":999}}])
        input_path.write_text(json.dumps(raw))
        version = usage.publish_training_progress(self.state)
        target = self.state/"request-usage"/"public-training-progress.json"
        public = json.loads(target.read_bytes())
        self.assertEqual(public["label_rate"],{"minutes":30,"articles":7})
        self.assertEqual(public["days"][0]["label_retry_pending"],3)
        self.assertEqual(public["label_error"],"retry")
        for forbidden in ("secret","usage","tokens","calls","requests","attempts"):
            self.assertNotIn(forbidden,target.read_text())
        self.assertEqual(input_path.read_text(),json.dumps(raw))
        self.assertEqual(usage.publish_training_progress(self.state,version),version)
        input_path.write_text(json.dumps({**raw,"days":[]}))
        with self.assertRaises(ValueError):
            usage.publish_training_progress(self.state,version)
        self.assertEqual(json.loads(target.read_bytes()),public)

    def test_public_news_research_and_home_are_sanitized_even_after_a_legacy_worker_writes(self):
        site=self.state/"site"
        site.mkdir()
        for folder in ("news-output","research-output"):
            (self.state/folder).mkdir()
        original={"articles":[{"llm_analysis":{"usage":{"total_tokens":99},"summary":"行情分析"}}],
                  "news_digest":{"summary":"今天","usage":{}},"previous_digest":{"usage":{},"date":"2026-11-18"}}
        news=self.state/"news-output/news.json"
        news.write_text(json.dumps(original))
        (self.state/"research-output/research.json").write_text(json.dumps(
            {"entities":{"A":{"price":123}},"fund_state":{"requests_this_run":19,"status":"ready"}}))
        (site/"view.json").write_text(json.dumps({"market":[{"price":12}],"sectors":[{"name":"光刻胶"}],
                  "sources":[{"id":"fed"}],"news":[{"source":"财联社","title":"测试快讯"}],
                  "collection":{"market_http_requests":110,"status":"ready"},"fallback":{"baostock_calls":15}}))
        (site/"health.json").write_text(json.dumps({"status":"ready","requests_this_run":7,
                  "sources":[{"id":"ecb","requests_this_run":2}],"article_count":342,"training_samples":600}))
        folder=self.state/"request-usage"
        folder.mkdir()
        directory=folder/"connected-sources.json"
        directory.write_text(json.dumps({"schema_version":1,"sources":[{"id":"sxdata","last_success_at":"12:00"}]}))
        versions={}
        usage.publish_feeds(self.state,versions,site)
        home=folder/"public-view.json"
        health=folder/"public-health.json"
        version=usage.file_version(home)
        checked=[{"id":"sxdata","name":"行情与资讯接口"}]
        source_fields=dict(sources=checked,sources_url="/data/connected-sources.json")
        self.assertEqual(json.loads(home.read_bytes()),{"market":[{"price":12}],"sectors":[{"name":"光刻胶"}],
                         "news":[{"source":"财联社","title":"测试快讯"}],
                         "collection":{"status":"ready"},"fallback":{},**source_fields})
        self.assertEqual(json.loads((folder/"public-research.json").read_bytes())["fund_state"],{"status":"ready"})
        self.assertEqual(json.loads((folder/"public-health.json").read_bytes()),
                         {"status":"ready","article_count":342,"training_samples":600,**source_fields})
        self.assertNotIn("usage",(folder/"public-news.json").read_text())
        original["news_digest"].update(summary="最新摘要",usage={"total_tokens":33})
        news.write_text(json.dumps(original))
        directory.write_text(json.dumps({"schema_version":1,"sources":[{"id":"sxdata","last_success_at":"12:01"}]}))
        usage.publish_feeds(self.state,versions,site)
        self.assertEqual(usage.file_version(home),version)
        self.assertEqual(json.loads((folder/"public-news.json").read_bytes())["news_digest"],{"summary":"最新摘要"})
        self.assertEqual(json.loads(news.read_bytes()),original)
        directory.write_text(json.dumps({"schema_version":1,"sources":[{"id":"minimax"},{"id":"sxdata"}]}))
        usage.publish_feeds(self.state,versions,site)
        checked.append({"id":"minimax","name":"专属模型"})
        self.assertEqual(json.loads(home.read_bytes())["sources"],checked)
        self.assertEqual(json.loads(health.read_bytes())["sources"],checked)
        self.assertEqual(json.loads((site/"health.json").read_bytes())["sources"][0]["id"],"ecb")

    def test_read_only_wal_fallback_requires_stable_main_and_absent_sidecars(self):
        path=self.state/"model.sqlite3"
        with closing(sqlite3.connect(path)) as db,db:
            db.execute("CREATE TABLE sample(value INTEGER)")
            db.execute("INSERT INTO sample VALUES(17)")
        real=usage.readonly
        failure=sqlite3.OperationalError("cannot open")
        failure.sqlite_errorcode=sqlite3.SQLITE_CANTOPEN
        def restricted(path,immutable=False):
            if not immutable:
                raise failure
            return real(path,immutable=True)
        with patch.object(usage,"readonly",side_effect=restricted) as access:
            self.assertEqual(usage.aggregate_row(path,"SELECT value FROM sample"),(17,))
            self.assertTrue(any(call.kwargs.get("immutable") for call in access.call_args_list))
            Path(str(path)+"-wal").write_bytes(b"still has committed work")
            access.reset_mock()
            with self.assertRaises(sqlite3.OperationalError):
                usage.aggregate_row(path,"SELECT value FROM sample")
            self.assertFalse(any(call.kwargs.get("immutable") for call in access.call_args_list))
