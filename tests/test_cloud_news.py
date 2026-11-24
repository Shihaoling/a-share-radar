from contextlib import ExitStack, nullcontext
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

if sys.platform == "win32":
    raise unittest.SkipTest("The Tencent Cloud worker uses POSIX file locking")

with patch.object(sys, "path", [str(Path(__file__).resolve().parents[1] / "deploy/tencent-preview"), *sys.path]):
    import cloud_news as news

from pipeline.minimax_news import infer_article

NOW = datetime.fromisoformat("2026-11-18T12:00:00+08:00")


def report(key, stamp=NOW, body="来源提供的快讯正文", **extra):
    return {"id": key, "title": "市场经济资讯消息"+key, "published_at": stamp.isoformat(),
            "first_seen_at": stamp.isoformat(), "full_text": body, **extra}


class CloudNewsTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        state = Path(stack.enter_context(TemporaryDirectory()))
        stack.enter_context(patch.multiple(news, STATE=state, OUTPUT=state/"output"))
        stack.enter_context(patch.object(news, "secret", return_value="fake-key"))
        stack.enter_context(patch.object(news, "shanghai_now", return_value=NOW))
        self.db = news.db_open()
        self.addCleanup(self.db.close)

    def feed(self, stamp=NOW):
        news.project(self.db, stamp)
        return json.loads((news.OUTPUT/"news.json").read_text())

    def test_digest_searches_from_the_previous_trading_close_and_keeps_source_links(self):
        day = "2026-11-23"
        for key, stamp in (("before","2026-11-20T14:59:59+08:00"),("overnight","2026-11-22T20:30:00+08:00")):
            story = report(key, datetime.fromisoformat(stamp), source="财联社")
            story["title"] = "英伟达CEO相关发布"
            news.save_article(self.db, story)
        now = datetime.fromisoformat(day+"T09:30:00+08:00")
        self.assertEqual(news.pending_digest_day(self.db,day,now.isoformat()),day)
        def model(packet,config,key,*,validator,**kwargs):
            boundary,source = packet["digest_articles"]
            self.assertEqual(boundary["window_start"],"2026-11-20T15:00:00+08:00")
            self.assertEqual(source["published_at"],"2026-11-22T20:30:00+08:00")
            return validator({"summary":"上一交易日收盘后有全球科技动态","positives":[],"risks":[],"watch":[],
                              "global_events":[{"text":"全球科技事件","article_ids":[source["id"]]}]})
        with patch.object(news,"infer_article",side_effect=model),patch.object(news,"shanghai_now",return_value=now):
            news.update_digest(self.db,"fake-key",day)
        feed = self.feed(now)
        self.assertEqual(feed["news_digest"]["global_events"][0]["article_ids"],["overnight"])
        self.assertFalse(feed["articles"])
        self.assertIn("overnight",{row["id"] for row in feed["recent_articles"]})
        self.assertIsNone(news.pending_digest_day(self.db,day,now.isoformat()))

    def test_midnight_snapshot_preserves_prior_day_bodies_and_digest_without_changing_today(self):
        article = report("A",llm_analysis={"status":"ready","summary":"经济数据","usage":{"total_tokens":12}})
        news.save_article(self.db, article)
        digest = {"date": NOW.date().isoformat(), "summary": "昨日经济数据公布", "generated_at": NOW.isoformat(),
                  "positives": [{"text": "昨日的消息", "article_ids": ["A"]}]}
        self.db.execute("INSERT INTO digests VALUES (?,?)", (digest["date"], json.dumps({**digest,"usage":{"total_tokens":8}})))
        self.assertNotIn("usage",json.dumps(self.feed(NOW)))
        result = self.feed(NOW+timedelta(days=1))
        self.assertEqual(result["articles"], [])
        self.assertEqual(result["news_digest"]["current_article_count"], 0)
        self.assertEqual(result["previous_digest"], digest)
        story = result["recent_articles"][0]
        self.assertEqual(story["published_at"], article["published_at"])
        self.assertNotIn("usage",story["llm_analysis"])
        self.assertIn("usage",self.db.execute("SELECT payload FROM digests").fetchone()[0])
        body = news.OUTPUT/"articles"/Path(story["article_text_url"]).name
        self.assertEqual(json.loads(body.read_text()),{"full_text":article["full_text"],"status":"complete","id":"A"})

    def test_rate_limit_keeps_cursor_and_articles_and_skips_the_next_request(self):
        news.save_article(self.db, report("A"))
        news.set_checkpoint(self.db, "coverage_end", NOW.isoformat())
        client = Mock()
        client.request.side_effect = news._Failure("rate_limited", 14400)
        with patch.object(news, "relay_client", return_value=nullcontext(client)):
            news.collect(self.db, NOW)
            news.collect(self.db, NOW+timedelta(minutes=2))
        client.request.assert_called_once()
        feed = self.feed()
        self.assertEqual(feed["collector"]["status"], "cooldown")
        self.assertEqual(feed["collector"]["coverage_end"], NOW.isoformat())
        self.assertEqual(feed["collector"]["retry_after"], (NOW+timedelta(hours=4)).isoformat())
        self.assertEqual(feed["articles"][0]["id"], "A")

    def test_capped_window_splits_and_advances_with_one_request_per_run(self):
        client = Mock()
        row = {"datetime": "2026-11-18 10:03:15", "content": "来源正文", "title": "市场关注经济数据", "channels": ""}
        client.request.side_effect = [[row, row], [], [row]]
        with patch.object(news, "relay_client", return_value=nullcontext(client)), patch.object(news, "NEWS_LIMIT", 2):
            news.collect(self.db, NOW)
            self.assertEqual(news.checkpoint(self.db, "coverage_end"), "")
            news.collect(self.db, NOW+timedelta(minutes=2))
            news.collect(self.db, NOW+timedelta(minutes=4))
        self.assertEqual(client.request.call_count, 3)
        windows = [(call.args[1]["start_date"][-8:], call.args[1]["end_date"][-8:]) for call in client.request.call_args_list]
        self.assertEqual(windows, [("09:00:00", "12:00:00"), ("11:37:00", "12:02:00"), ("09:00:00", "10:30:00")])
        self.assertEqual(news.checkpoint(self.db, "coverage_end"), NOW.replace(hour=10, minute=30).isoformat())
        self.assertEqual(self.db.execute("SELECT count(*) FROM articles").fetchone()[0], 1)

    def test_delayed_publication_after_429_and_empty_reply_is_collected_analysed_and_published(self):
        news.save_article(self.db, report("cached", body="", published_date=NOW.date().isoformat()))
        news.set_checkpoint(self.db, "coverage_end", NOW.isoformat())
        published = NOW-timedelta(minutes=10)
        windows = []
        def upstream(api, params):
            windows.append(params)
            if len(windows) == 1:
                raise news._Failure("rate_limited", 1200)
            if len(windows) == 2 or not (params["start_date"] <= published.strftime("%F %T") <= params["end_date"]):
                return []
            return [{"datetime":published.isoformat(), "content":"迟到快讯的完整正文",
                     "title":"迟到发布的市场快讯", "channels":""}]
        client = Mock()
        client.request.side_effect = upstream
        with patch.object(news, "relay_client", return_value=nullcontext(client)):
            for minutes in (0, 2, 20, 40):
                now = NOW+timedelta(minutes=minutes)
                news.shanghai_now.return_value = now
                news.collect(self.db, now)
                feed = self.feed(now)
                if minutes<40:
                    self.assertEqual([article["id"] for article in feed["articles"]], ["cached"])
        self.assertEqual(len(windows), 3)
        self.assertEqual(feed["collector"]["coverage_end"], now.isoformat())
        def infer(item, cfg, key, **kwargs):
            if "digest_articles" in item:
                return kwargs["validator"]({"summary":"迟到快讯已于本轮纳入总览", "positives":[], "risks":[], "watch":[]})
            return {"summary":"根据快讯正文生成摘要", "sector_impacts":[]}
        with patch.object(news, "infer_article", side_effect=infer):
            news.analyze(self.db, now, 2)
        final = self.feed(now)
        restored = next(item for item in final["articles"] if item["id"] != "cached")
        self.assertEqual(restored["published_at"], published.isoformat())
        self.assertEqual(restored["first_seen_at"], now.isoformat())
        self.assertEqual(restored["llm_status"], "ready")
        self.assertEqual(final["news_digest"]["article_count"], 1)

    def test_long_outage_across_midnight_alternates_latest_headlines_with_late_prior_day(self):
        now = NOW.replace(hour=2)
        cursor = now-timedelta(hours=3)
        news.set_checkpoint(self.db, "coverage_end", cursor.isoformat())
        late = cursor-timedelta(minutes=20)
        windows = []
        def upstream(api, params):
            windows.append(params)
            published = now if len(windows) == 1 else late
            self.assertLessEqual(params["start_date"], published.strftime("%F %T"))
            self.assertGreaterEqual(params["end_date"], published.strftime("%F %T"))
            return [{"datetime":published.isoformat(), "content":"来源正文",
                     "title":"当天即时快讯" if len(windows)==1 else "前一日迟到快讯", "channels":""}]
        client = Mock()
        client.request.side_effect = upstream
        with patch.object(news, "relay_client", return_value=nullcontext(client)):
            news.collect(self.db, now)
            news.collect(self.db, now+timedelta(minutes=20))
        result = self.feed(now+timedelta(minutes=20))
        self.assertEqual([item["title"] for item in result["articles"]], ["当天即时快讯"])
        self.assertEqual([item["title"] for item in result["recent_articles"]], ["前一日迟到快讯"])
        self.assertEqual(result["collector"]["coverage_end"], (cursor+timedelta(hours=1)).isoformat())
        self.assertEqual(client.request.call_count, 2)

    def test_one_second_saturation_records_the_gap_and_allows_later_news_through(self):
        news.set_checkpoint(self.db, "collect_next_start", NOW.isoformat())
        news.set_checkpoint(self.db, "collect_next_end", NOW.isoformat())
        row = {"datetime": NOW.isoformat(), "content": "来源正文", "title": "市场关注经济数据", "channels": ""}
        client = Mock()
        client.request.side_effect = [[row], []]
        with patch.object(news, "relay_client", return_value=nullcontext(client)), patch.object(news, "NEWS_LIMIT", 1):
            news.collect(self.db, NOW)
            news.collect(self.db, NOW+timedelta(minutes=2))
        self.assertEqual(client.request.call_count, 2)
        self.assertEqual(client.request.call_args.args[1]["start_date"][-8:], "12:00:01")
        self.assertEqual(self.feed()["collector"]["last_incomplete_second"], NOW.isoformat())

    def test_malformed_item_preserves_valid_sibling_without_claiming_window_complete(self):
        good = {"datetime": NOW.isoformat(), "content": "来源正文", "title": "市场关注经济数据", "channels": ""}
        client = Mock()
        client.request.return_value = [{**good, "content": False}, good]
        with patch.object(news, "relay_client", return_value=nullcontext(client)):
            news.collect(self.db, NOW)
        result = self.feed()
        self.assertEqual(len(result["articles"]), 1)
        self.assertIsNone(result["collector"]["coverage_end"])
        self.assertEqual(result["collector"]["status"], "partial")
        self.assertEqual(result["collector"]["error_code"], "invalid_news_window")

    def test_late_body_completes_the_stable_article_without_a_second_timeline_item(self):
        original = report("A", body="")
        completed = {**original, "id": "changed-hash", "full_text": "补齐的来源正文",
                     "first_seen_at": (NOW+timedelta(minutes=2)).isoformat()}
        self.assertEqual(news.save_article(self.db, original), 1)
        self.assertEqual(news.save_article(self.db, completed), 1)
        self.assertEqual(news.save_article(self.db, completed), 0)
        stored = [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM articles")]
        self.assertEqual(len(stored), 1)
        self.assertEqual((stored[0]["id"],stored[0]["first_seen_at"],stored[0]["full_text"]),
                         ("A", NOW.isoformat(), completed["full_text"]))

    @staticmethod
    def mock_source(entries, limit=None):
        def respond(api, params):
            assert api == "news"
            selected = [row for row in entries if params["start_date"] <= row["datetime"] <= params["end_date"]]
            return sorted(selected, key=lambda r: r["datetime"], reverse=True)[:limit]
        return Mock(request=Mock(side_effect=respond))

    @staticmethod
    def source_row(stamp, content, title="市场最新经济快讯"):
        return {"datetime": stamp.strftime("%F %T"), "content": content, "title": title, "channels": ""}

    @staticmethod
    def model_answer(article, config, key, **kwargs):
        if "digest_articles" in article:
            return kwargs["validator"]({"summary":"当日新增资讯已汇总。", "positives":[], "risks":[], "watch":[]})
        return {"summary":"根据快讯正文生成摘要", "sector_impacts":[]}

    def test_refresh_calls_model_only_for_new_articles_and_releases_event_after_digest(self):
        flag = news.STATE / "news/analyze.trigger"
        rows = []
        with patch.object(news, "relay_client", return_value=nullcontext(self.mock_source(rows))), \
             patch.object(news, "infer_article", side_effect=self.model_answer) as infer:
            news.collect(self.db, NOW)
            self.assertFalse(flag.exists())
            news.process_analysis_event(self.db, 8)
            infer.assert_not_called()
            rows.append(self.source_row(NOW, "真实新增的完整快讯正文"))
            news.collect(self.db, NOW+timedelta(minutes=2))
            self.assertTrue(flag.exists())
            news.process_analysis_event(self.db, 8)
            self.assertFalse(flag.exists())
            self.assertEqual(self.feed()["articles"][0]["llm_status"], "ready")
            original = self.feed()["news_digest"]
            self.assertEqual(original["article_count"], 1)
            later = NOW+timedelta(minutes=10)
            news.shanghai_now.return_value = later
            news.collect(self.db, later)
            self.assertFalse(flag.exists())
            news.process_analysis_event(self.db, 8)
            self.assertEqual(infer.call_count, 2)  # One article and one digest.
            self.assertEqual(self.feed(later)["news_digest"], original)
            later += timedelta(minutes=10)
            news.shanghai_now.return_value = later
            rows.append(self.source_row(later, "第二条新增快讯的完整正文"))
            news.collect(self.db, later)
            news.process_analysis_event(self.db, 8)
            updated = self.feed(later)["news_digest"]
            self.assertEqual(updated["article_count"], 2)
            self.assertEqual(updated["generated_at"], later.isoformat())

    def test_arrival_while_analyzing_survives_batch_exit_and_worker_restart(self):
        flag = news.STATE / "news/analyze.trigger"
        rows = [self.source_row(NOW, "第一篇消息的正文")]
        collector = news.db_open()
        self.addCleanup(collector.close)
        ids = []
        def infer(article, config, key, **kwargs):
            if "digest_articles" not in article:
                ids.append(article["id"])
                if len(ids) == 1:
                    rows.append(self.source_row(NOW, "另外一篇消息的正文"))
                    news.collect(collector, NOW+timedelta(minutes=2))
            return self.model_answer(article, config, key, **kwargs)
        with patch.object(news, "relay_client", return_value=nullcontext(self.mock_source(rows))), \
             patch.object(news, "infer_article", side_effect=infer):
            news.collect(collector, NOW)
            news.process_analysis_event(self.db, 1)
            self.assertTrue(flag.exists())
            restarted = news.db_open()
            self.addCleanup(restarted.close)
            news.process_analysis_event(restarted, 1)
            self.assertFalse(flag.exists())
            self.assertEqual(len(set(ids)), 2)
            self.assertEqual(self.feed()["news_digest"]["article_count"], 2)

    def test_provider_cooldown_preserves_queue_and_retries_on_an_unchanged_poll(self):
        flag = news.STATE / "news/analyze.trigger"
        rows = [self.source_row(NOW, "先到的快讯")]
        with patch.object(news, "relay_client", return_value=nullcontext(self.mock_source(rows))):
            news.collect(self.db, NOW)
            with patch.object(news, "infer_article", side_effect=news.ModelFailure("http_429", 600)) as infer:
                news.process_analysis_event(self.db, 8)
                infer.assert_called_once()
            self.assertFalse(flag.exists())
            later = NOW+timedelta(minutes=11)
            news.shanghai_now.return_value = later
            with patch.object(news, "infer_article", side_effect=self.model_answer) as infer:
                news.collect(self.db, later)
                self.assertTrue(flag.exists())
                news.process_analysis_event(self.db, 8)
                self.assertEqual(infer.call_count, 2)
            self.assertEqual(self.feed(later)["news_digest"]["article_count"], 1)
            self.assertEqual(self.feed(later)["news_digest"]["generated_at"], later.isoformat())
            self.assertFalse(flag.exists())

    def test_three_hour_poll_and_two_hourly_day_sweep_collect_distinct_delayed_stories(self):
        start = NOW.replace(hour=1, minute=30)
        near = self.source_row(start-timedelta(hours=2, minutes=38), "昨日同题的第一份完整快讯")
        older = self.source_row(start-timedelta(hours=19), "昨日同题的第二份完整快讯")
        entries = []
        client = self.mock_source(entries)
        with patch.object(news, "relay_client", return_value=nullcontext(client)):
            for minute in range(0, 121, 20):
                if minute == 20:
                    entries.extend((near, older))
                at = start+timedelta(minutes=minute)
                news.collect(self.db, at)
                if minute == 20:
                    feed = self.feed(at)
                    self.assertEqual([r["summary"] for r in feed["recent_articles"]], [near["content"]])
                    self.assertEqual(feed["recent_articles"][0]["first_seen_at"], at.isoformat())
        self.assertEqual(client.request.call_count, 7)
        windows = [call.args[1] for call in client.request.call_args_list]
        self.assertEqual(windows[1]["start_date"], (start-timedelta(hours=3)).strftime("%F %T"))
        self.assertEqual(windows[-1]["start_date"], (at-timedelta(days=1)).strftime("%F %T"))
        recent = self.feed(at)["recent_articles"]
        self.assertEqual({r["summary"] for r in recent}, {near["content"], older["content"]})
        self.assertEqual(next(r for r in recent if r["summary"] == older["content"])["first_seen_at"], at.isoformat())

    def test_legacy_short_and_long_headline_keys_preserve_distinct_bodies_and_labels(self):
        for number, title in enumerate(("数据", "今日经济数据统计")):
            prefix = str(number)
            original = report(prefix+"old", title=title, body="指标  +3%",
                              llm_analysis={"status":"ready", "summary":"第一次快讯的标签"})
            legacy = news.headline_key(title)
            if len(legacy)<6:
                legacy += news.sha256(original["full_text"].encode()).hexdigest()
            self.db.execute("INSERT INTO articles(id,day,published,headline,payload,analyzed) VALUES(?,?,?,?,?,1)",
                            (original["id"], NOW.date().isoformat(), NOW.isoformat(), legacy, json.dumps(original)))
            repeat = report(prefix+"duplicate", stamp=NOW+timedelta(seconds=10), title=title, body="指标 +3%")
            changed = report(prefix+"new", title=title, body="指标 -3%")
            self.assertEqual(news.save_article(self.db, repeat), 0)
            self.assertEqual(news.save_article(self.db, changed), 1)
            self.assertEqual(news.save_article(self.db, changed), 0)
            stored = [(key, flag) for key, flag in self.db.execute("SELECT id,analyzed FROM articles WHERE id LIKE ? ORDER BY id", (prefix+"%",))]
            self.assertEqual(stored, [(changed["id"], 0), (original["id"], 1)])

    def test_capped_day_sweep_retains_both_halves_through_restart_and_keeps_live_poll(self):
        entries = [self.source_row(NOW-timedelta(hours=h), "原文"+str(h)) for h in (21, 8, 4)]
        client = self.mock_source(entries, limit=2)
        news.set_checkpoint(self.db, "coverage_end", NOW.isoformat())
        news.set_checkpoint(self.db, "sweep_after", NOW.isoformat())
        with patch.object(news, "relay_client", return_value=nullcontext(client)), patch.object(news, "NEWS_LIMIT", 2):
            news.collect(self.db, NOW)
            windows = self.db.execute("SELECT start_at,end_at FROM news_rechecks ORDER BY start_at").fetchall()
            self.assertEqual(windows, [((NOW-timedelta(days=1)).isoformat(), (NOW-timedelta(hours=12)).isoformat()),
                ((NOW-timedelta(hours=12)+timedelta(seconds=1)).isoformat(), NOW.isoformat())])
            self.db.close()
            self.db = news.db_open()
            self.addCleanup(self.db.close)
            entries.append(self.source_row(NOW+timedelta(minutes=2), "即时新增", title="实时消息"))
            news.collect(self.db, NOW+timedelta(minutes=2))
            self.assertIn("实时消息", [r["title"] for r in self.feed(NOW+timedelta(minutes=2))["articles"]])
            for minute in range(4, 121, 2):
                at = NOW+timedelta(minutes=minute)
                news.collect(self.db, at)
                if not self.feed(at)["collector"]["pending_recheck_windows"]:
                    break
        self.assertLessEqual(minute, 120)
        self.assertEqual(self.feed(at)["collector"]["pending_recheck_windows"], 0)
        stored = {json.loads(row[0])["full_text"] for row in self.db.execute("SELECT payload FROM articles")}
        self.assertEqual(stored, {"原文21", "原文8", "原文4", "即时新增"})
        self.assertEqual(client.request.call_count, minute//2+1)

    def test_empty_response_contradicting_saved_news_is_rechecked_before_gap_closes(self):
        old = NOW-timedelta(hours=8)
        old_row = self.source_row(old, "先前触顶时的已知正文")
        late_row = self.source_row(old+timedelta(seconds=10), "该段随后补齐的新正文")
        start, end = old-timedelta(minutes=1), old+timedelta(minutes=1)
        news.set_checkpoint(self.db, "coverage_end", NOW.isoformat())
        news.set_checkpoint(self.db, "sweep_after", (NOW+timedelta(days=1)).isoformat())
        news.queue_recheck(self.db, start, end, NOW)
        news.save_article(self.db, report("stock-api-news-known", stamp=old, body=old_row["content"],
                                           title=old_row["title"]))
        client = self.mock_source([])
        with patch.object(news, "relay_client", return_value=nullcontext(client)):
            news.collect(self.db, NOW)
            self.assertEqual(news.checkpoint(self.db, "collect_error"), "empty_known_window")
            self.assertEqual(self.feed()["collector"]["pending_recheck_windows"], 1)
            client.request.side_effect = self.mock_source([old_row, late_row]).request.side_effect
            for minute in (2, 20, 40, 60, 80, 100, 120):
                now = NOW+timedelta(minutes=minute)
                news.collect(self.db, now)
        self.assertEqual(self.feed(now)["collector"]["pending_recheck_windows"], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM articles").fetchone()[0], 2)
        self.assertEqual(client.request.call_count, 8)

    def test_terminal_second_is_retried_after_restart_while_fresh_news_advances(self):
        old = NOW-timedelta(hours=7)
        news.set_checkpoint(self.db, "unresolved_second", old.isoformat())
        news.set_checkpoint(self.db, "coverage_end", NOW.isoformat())
        news.set_checkpoint(self.db, "sweep_after", (NOW+timedelta(days=1)).isoformat())
        self.db.commit()
        self.db.close()
        self.db = news.db_open()
        self.addCleanup(self.db.close)
        article = self.source_row(old, "后来可以完整读取的快讯")
        current = self.source_row(NOW+timedelta(minutes=2), "实时更新")
        client = self.mock_source([article, article, current])
        with patch.object(news, "relay_client", return_value=nullcontext(client)), patch.object(news, "NEWS_LIMIT", 2):
            news.collect(self.db, NOW)
            self.assertEqual(self.feed()["collector"]["last_incomplete_second"], old.isoformat())
            client.request.side_effect = self.mock_source([article, current]).request.side_effect
            for minute in (2, 20, 40, 60, 80, 100, 120):
                at = NOW+timedelta(minutes=minute)
                news.collect(self.db, at)
                if minute == 2:
                    self.assertEqual(self.feed(at)["collector"]["coverage_end"], at.isoformat())
        feed = self.feed(at)
        self.assertIsNone(feed["collector"]["last_incomplete_second"])
        self.assertEqual(feed["collector"]["status"], "ready")
        self.assertEqual(client.request.call_args.args[1]["start_date"], old.strftime("%F %T"))
        self.assertEqual(client.request.call_count, 8)

    def test_interrupted_initial_request_after_nine_day_outage_recovers_original_window(self):
        awaited = self.source_row(NOW-timedelta(hours=1), "停机前发布的真实快讯")
        client = Mock()
        with patch.object(news, "relay_client", return_value=nullcontext(client)):
            client.request.side_effect = RuntimeError("interrupted")
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                news.collect(self.db, NOW)
            self.db.close()
            self.db = news.db_open()
            self.addCleanup(self.db.close)
            resume = NOW+timedelta(days=9)
            client.request.side_effect = self.mock_source([awaited]).request.side_effect
            news.collect(self.db, resume)
            news.collect(self.db, resume+timedelta(minutes=2))
        self.assertEqual(client.request.call_args_list[1].args[1]["end_date"], resume.strftime("%F %T"))
        self.assertEqual(client.request.call_args_list[2].args[1], client.request.call_args_list[0].args[1])
        article = self.feed(resume)["recent_articles"][0]
        self.assertEqual(article["first_seen_at"], (resume+timedelta(minutes=2)).isoformat())
        self.assertEqual(self.feed(resume)["collector"]["coverage_end"], NOW.isoformat())

    def test_digest_rate_limit_pauses_model_but_preserves_available_digest(self):
        day = NOW.date().isoformat()
        news.save_article(self.db, report("A", llm_analysis={"summary": "已有快讯的摘要", "status": "ready"}))
        self.db.execute("INSERT INTO digests VALUES (?,?)", (day, json.dumps({"date": day, "summary": "已有总览"})))
        with patch.object(news, "infer_article", side_effect=news.ModelFailure("http_429", 172800)) as infer:
            news.analyze(self.db, NOW, 0)
            news.save_article(self.db, report("B"))
            news.analyze(self.db, NOW+timedelta(minutes=20), 3)
        infer.assert_called_once()
        digest = self.feed()["news_digest"]
        self.assertEqual(digest["summary"], "已有总览")
        self.assertEqual(digest["status"], "temporarily_unavailable")
        self.assertEqual(digest["retry_after"], (NOW+timedelta(days=2)).isoformat())

    def test_shared_model_cooldown_defers_worker_without_recording_an_article_attempt(self):
        news.save_article(self.db, report("A"))
        with patch.object(news, "infer_article", side_effect=news.ModelFailure("shared_model_cooldown", 1800)):
            news.analyze(self.db, NOW, 2)
        self.assertEqual(self.db.execute("SELECT retry_after FROM articles").fetchone()[0], "")
        self.assertEqual(self.db.execute("SELECT count(*) FROM model_usage").fetchone()[0], 0)
        self.assertEqual(news.checkpoint(self.db, "model_pause"), (NOW+timedelta(minutes=30)).isoformat())

    def test_slow_single_item_runs_drain_current_day_and_prior_day_fairly(self):
        old = NOW - timedelta(days=1)
        for i in range(4):
            news.save_article(self.db, report("old-"+str(i), old+timedelta(seconds=i)))
        for i in range(6):
            news.save_article(self.db, report("today-"+str(i), NOW-timedelta(seconds=6-i)))
        published = []
        def infer(article, config, key):
            published.append(article["id"])
            return {"summary":"正文分析已完成", "sector_impacts":[]}
        with patch.object(news,"infer_article",side_effect=infer), patch.object(news,"update_digest"):
            for _ in range(4):
                news.analyze(self.db, NOW, 1)
        self.assertEqual(published, ["today-5","old-0","today-4","today-0"])
        self.assertFalse(self.db.in_transaction)

    def test_completed_article_reaches_disk_before_second_model_call_and_digest(self):
        news.save_article(self.db, report("first", NOW))
        news.save_article(self.db, report("next", NOW-timedelta(seconds=1)))
        def public_states():
            feed = json.loads((news.OUTPUT/"news.json").read_text())
            return {article["id"]:article["llm_status"] for article in feed["articles"]}
        def infer(article, config, key):
            if article["id"] == "next":
                self.assertEqual(public_states(), {"first":"ready","next":"pending"})
            return {"summary":"已完成单篇新闻的解读", "sector_impacts":[]}
        def digest(db, key, day):
            self.assertEqual(public_states(), {"first":"ready","next":"ready"})
        with patch.object(news,"infer_article",side_effect=infer), patch.object(news,"update_digest",side_effect=digest):
            self.assertTrue(news.analyze(self.db,NOW,2))

    def test_digest_transport_backoff_allows_the_next_article(self):
        news.save_article(self.db, report("first"))
        answer = {"summary":"单篇新闻已完成分析", "sector_impacts":[]}
        with patch.object(news, "infer_article", return_value=answer), patch.object(
                news, "update_digest", side_effect=news.ModelFailure("transport_error", 1800)) as digest:
            news.analyze(self.db, NOW, 1)
            self.assertEqual(news.checkpoint(self.db, "model_pause"), "")
            self.assertEqual(news.checkpoint(self.db, "digest_pause"), (NOW+timedelta(minutes=30)).isoformat())
            news.save_article(self.db, report("next"))
            news.analyze(self.db, NOW, 1)
            self.assertEqual(digest.call_count, 1)
        self.assertEqual(self.db.execute("SELECT sum(analyzed) FROM articles").fetchone()[0], 2)

    def test_idle_or_cooling_worker_avoids_model_and_projection(self):
        news.save_article(self.db, report("empty",body=""))
        news.save_article(self.db, report("deferred"))
        later = (NOW+timedelta(minutes=2)).isoformat()
        self.db.execute("UPDATE articles SET retry_after=? WHERE id='deferred'",(later,))
        with patch.object(news,"secret") as key, patch.object(news,"infer_article") as infer, \
             patch.object(news,"publish_news") as project:
            self.assertFalse(news.analyze(self.db,NOW,8))
            news.save_article(self.db,report("ready-to-attempt"))
            news.set_checkpoint(self.db,"model_pause",later)
            self.assertFalse(news.analyze(self.db,NOW,8))
            key.assert_not_called()
            infer.assert_not_called()
            project.assert_not_called()
        states = {a["id"]:a for a in self.feed()["articles"]}
        self.assertEqual(states["empty"]["llm_status"],"waiting_body")
        self.assertEqual(states["ready-to-attempt"]["llm_status"],"provider_cooldown")
        self.assertEqual(states["ready-to-attempt"]["analysis_retry_at"],later)
        news.set_checkpoint(self.db,"model_pause","")
        states = {a["id"]:a for a in self.feed()["articles"]}
        self.assertEqual(states["deferred"]["llm_status"],"retry")
        self.assertEqual(states["deferred"]["analysis_retry_at"],later)
        self.assertEqual(states["ready-to-attempt"]["llm_status"],"pending")

    def test_article_truncation_does_not_pause_other_news_or_the_digest(self):
        news.save_article(self.db, report("A", stamp=NOW-timedelta(minutes=2)))
        news.save_article(self.db, report("B"))
        with patch.object(news, "update_digest") as digest, patch.object(
                news, "infer_article", side_effect=[news.ModelFailure("incomplete_response", 1800),
                                                     {"summary": "一条完整的快讯摘要", "sector_impacts": []}]) as infer:
            news.analyze(self.db, NOW, 3)
        self.assertEqual([call.args[0]["id"] for call in infer.call_args_list], ["B", "A"])
        digest.assert_called_once()
        self.assertEqual(news.checkpoint(self.db, "model_pause"), "")
        self.assertEqual(self.db.execute("SELECT id FROM articles WHERE analyzed=1").fetchall(), [("A",)])

    def test_model_respects_retry_after_seconds_and_http_date(self):
        until = NOW+timedelta(hours=49)
        for code, value in [(429, "176400"), (503, format_datetime(until.astimezone(timezone.utc), usegmt=True))]:
            with self.subTest(code=code):
                opener = Mock()
                opener.open.side_effect = HTTPError("https://example.invalid", code, "", {"Retry-After": value}, io.BytesIO(b"{}"))
                with patch("pipeline.minimax_news.urllib.request.build_opener", return_value=opener), \
                        patch("pipeline.minimax_news.shanghai_now", return_value=NOW), self.assertRaises(news.ModelFailure) as caught:
                    infer_article(report("A"), {}, "fake")
                self.assertEqual(caught.exception.retry, 176400)

    def test_midnight_backlog_shares_article_and_digest_budgets_by_original_day(self):
        yesterday = NOW-timedelta(days=1)
        news.save_article(self.db, report("old", yesterday))
        news.save_article(self.db, report("today"))
        calls = []

        def infer(item, config, key, **kwargs):
            if "digest_articles" in item:
                calls.append(("digest", item["title"][:10]))
                return kwargs["validator"]({"summary": "这一日期已收录的事件要点摘要",
                    "positives": [{"text": "本日事件", "article_ids": [item["digest_articles"][1]["id"]]}],
                    "risks": [], "watch": []})
            calls.append(("article", item["id"]))
            return {"summary": "来源全文的事实摘要", "sector_impacts": []}

        with patch.object(news, "infer_article", side_effect=infer):
            news.analyze(self.db, NOW, 1)
            self.assertEqual(calls, [("article", "today"), ("digest", NOW.date().isoformat())])
            revision = news.checkpoint(self.db, "model_revision")
            news.save_article(self.db, report("today-later"))
            later = NOW+timedelta(minutes=2)
            with patch.object(news, "shanghai_now", return_value=later):
                news.analyze(self.db, later, 1)
        self.assertEqual(calls[2:], [("article", "old"), ("digest", yesterday.date().isoformat())])
        self.assertEqual(news.checkpoint(self.db, "model_revision"), revision)
        memberships = dict(self.db.execute("SELECT id,day FROM digest_members"))
        self.assertEqual(memberships, {"today": NOW.date().isoformat(), "old": yesterday.date().isoformat()})
        published = self.feed(later)
        self.assertEqual(published["news_digest"]["article_ids"], ["today"])
        self.assertEqual(published["recent_articles"][0]["llm_analysis"]["status"], "ready")
        self.assertEqual(published["recent_articles"][0]["llm_analysis"]["scored_at"], later.isoformat())

    def test_prior_day_digest_is_served_even_with_new_ready_current_day_articles(self):
        yesterday = (NOW-timedelta(days=1)).date().isoformat()
        today = NOW.date().isoformat()
        for name, stamp in [("today", NOW), ("old", NOW-timedelta(days=1))]:
            news.save_article(self.db, report(name, stamp, llm_analysis={"status": "ready", "summary": "摘要"}))
        self.assertEqual(news.pending_digest_day(self.db, today, NOW.isoformat()), today)
        news.set_checkpoint(self.db, "last_digest_day", today)
        self.assertEqual(news.pending_digest_day(self.db, today, NOW.isoformat()), yesterday)
        news.set_checkpoint(self.db, "digest_pause:"+yesterday, (NOW+timedelta(minutes=30)).isoformat())
        self.assertEqual(news.pending_digest_day(self.db, today, NOW.isoformat()), today)

    def test_live_articles_use_approved_source_trust_and_the_analyzed_tone(self):
        for key, fact in [("A", "reported_fact"), ("B", "rumor")]:
            news.save_article(self.db, report(key, provider="stock_api", source_id="stock_api:cls",
                credibility="unverified", llm_analysis={"status": "ready", "tone": "mixed", "fact_status": fact}))
        news.save_article(self.db, report("C", source_id="stock_api:cls", credibility="unverified"))
        articles = {a["id"]: a for a in self.feed()["articles"]}
        self.assertEqual([articles[key]["credibility"] for key in ("A", "B", "C")], ["reliable", "rumor", "unverified"])
        self.assertEqual((articles["A"]["sentiment"], articles["A"]["sentiment_method"]), ("mixed", "minimax_full_text"))
