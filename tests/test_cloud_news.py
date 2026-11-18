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

    def test_midnight_snapshot_preserves_prior_day_bodies_and_digest_without_changing_today(self):
        article = report("A")
        news.save_article(self.db, article)
        digest = {"date": NOW.date().isoformat(), "summary": "昨日经济数据公布", "generated_at": NOW.isoformat(),
                  "positives": [{"text": "昨日的消息", "article_ids": ["A"]}]}
        self.db.execute("INSERT INTO digests VALUES (?,?)", (digest["date"], json.dumps(digest)))
        result = self.feed(NOW+timedelta(days=1))
        self.assertEqual(result["articles"], [])
        self.assertEqual(result["news_digest"]["current_article_count"], 0)
        self.assertEqual(result["previous_digest"], digest)
        story = result["recent_articles"][0]
        self.assertEqual(story["published_at"], article["published_at"])
        body = news.OUTPUT/"articles"/Path(story["article_text_url"]).name
        self.assertEqual(json.loads(body.read_text())["full_text"], article["full_text"])

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
        row = {"datetime": "2026-11-18 11:03:15", "content": "来源正文", "title": "市场关注经济数据", "channels": ""}
        client.request.side_effect = [[row, row], [], [row]]
        with patch.object(news, "relay_client", return_value=nullcontext(client)), patch.object(news, "NEWS_LIMIT", 2):
            news.collect(self.db, NOW)
            self.assertEqual(news.checkpoint(self.db, "coverage_end"), "")
            news.collect(self.db, NOW+timedelta(minutes=2))
            news.collect(self.db, NOW+timedelta(minutes=4))
        self.assertEqual(client.request.call_count, 3)
        windows = [(call.args[1]["start_date"][-8:], call.args[1]["end_date"][-8:]) for call in client.request.call_args_list]
        self.assertEqual(windows, [("11:00:00", "12:00:00"), ("11:37:00", "12:02:00"), ("11:00:00", "11:30:00")])
        self.assertEqual(news.checkpoint(self.db, "coverage_end"), NOW.replace(hour=11, minute=30).isoformat())
        self.assertEqual(self.db.execute("SELECT count(*) FROM articles").fetchone()[0], 1)

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
