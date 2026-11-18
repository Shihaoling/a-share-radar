"""Offline coverage for the scheduled historical news collector and label queue."""

from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from pipeline.calendar import SHANGHAI

if sys.platform == "win32":
    raise unittest.SkipTest("Tencent's systemd worker uses Linux file locks")

with patch.object(sys, "path", [str(Path(__file__).resolve().parents[1] / "deploy/tencent-preview"), *sys.path]):
    import training_news as news


class HistoricalNewsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.now = datetime(2026, 6, 2, 14, tzinfo=SHANGHAI)
        self.begin = self.now.replace(hour=0) - timedelta(days=1)
        self.end = self.begin + timedelta(days=1)
        self.patch("ROOT", Path(temp.name))
        self.patch("OUTPUT", Path(temp.name) / "status/progress.json")
        self.patch("shanghai_now", side_effect=lambda: self.now)
        self.patch("credential", return_value="local-fixture-token")
        self.patch("gate_delay", return_value=(0, 0))
        self.client = Mock(count=1)
        self.patch("relay_client", return_value=nullcontext(self.client))
        self.db = news.open_dataset()
        self.addCleanup(self.db.close)
        with self.db:
            news.set_meta(self.db, "start_day", self.begin.date().isoformat())
            news.set_meta(self.db, "end_day", self.end.date().isoformat())
            self.db.execute("INSERT INTO windows(begin_at,end_at) VALUES (?,?)",
                            (self.begin.isoformat(), self.end.isoformat()))

    def patch(self, name, *args, **kwargs):
        patcher = patch.object(news, name, *args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def window(self):
        return self.db.execute("SELECT * FROM windows ORDER BY begin_at LIMIT 1").fetchone()

    def article(self, title="新闻"):
        return {"datetime": self.begin.isoformat(), "title": title, "content": "正文", "channels": []}

    def collect(self, result):
        self.client.request.return_value = result
        news.collect(self.db, self.now)

    def test_malformed_article_retains_valid_neighbour_and_recovers(self):
        self.collect([self.article(), self.article({"incorrect": "type"})])
        self.assertEqual(self.window()["state"], "unverified")
        status = news.progress(self.db)
        self.assertEqual((status["collected"], status["audited_days"]), (1, 0))
        self.assertEqual(status["days"][0]["verification_retry_windows"], 1)
        self.collect([])
        self.assertEqual(self.client.request.call_count, 1)
        self.now += timedelta(minutes=31)
        self.collect([])
        self.assertEqual(self.window()["outcome"], "recheck_less_than_original")
        self.assertEqual(self.window()["rows_received"], 2)
        self.now += timedelta(minutes=31)
        self.collect([self.article(), self.article("第二篇")])
        self.assertEqual(self.window()["state"], "complete")
        self.assertEqual(news.progress(self.db)["collected"], 2)
        self.assertEqual(news.progress(self.db)["audited_days"], 1)

    def test_repeated_bad_window_is_left_for_review_after_bounded_retries(self):
        for _ in range(4):
            self.collect([self.article(None) | {"datetime": None}])
            self.now += timedelta(hours=1)
        self.assertEqual(self.client.request.call_count, 3)
        status = news.progress(self.db)["days"][0]
        self.assertEqual((status["incomplete_windows"], status["verification_retry_windows"]), (1, 0))

    def test_preexisting_capped_minute_splits_without_spending_a_request(self):
        with self.db:
            self.db.execute("UPDATE windows SET state='capped',end_at=?,rows_received=1500",
                            ((self.begin + timedelta(minutes=1)).isoformat(),))
            self.db.execute("INSERT INTO window_audit VALUES (?,1500,1500,1500,0,?)",
                            (self.begin.isoformat(), self.now.isoformat()))
        self.collect([])
        self.client.request.assert_not_called()
        records = self.db.execute("SELECT begin_at,end_at,state FROM windows ORDER BY begin_at").fetchall()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["end_at"], records[1]["begin_at"])
        self.assertEqual(news.progress(self.db)["days"][0]["audited_windows"], 0)

    def test_shared_cooldown_does_not_charge_a_window_attempt_or_retry_early(self):
        self.client.count = 0
        self.client.request.side_effect = news._Failure("rate_limited", 7200)
        self.collect([])
        self.assertEqual(self.window()["attempts"], 0)
        self.assertEqual(datetime.fromisoformat(news.progress(self.db)["collection_retry_at"]),
                         self.now + timedelta(hours=2))
        self.now += timedelta(hours=1)
        self.collect([])
        self.assertEqual(self.client.request.call_count, 1)

    def test_preblocked_non_rate_error_leaves_windows_and_rate_streak_untouched(self):
        self.client.count = 0
        self.client.request.side_effect = news._Failure("response_too_large", 120, deferred=True)
        self.collect([])
        self.assertEqual(self.db.execute("SELECT count(*) FROM windows").fetchone()[0], 1)
        self.assertEqual(self.window()["attempts"], 0)
        self.assertEqual(news.metadata(self.db, "collection_rate_limit_streak", "0"), "0")
        status = news.progress(self.db)
        self.assertEqual(status["collection_pause_reason"], "response_too_large")

    def test_partial_label_batch_delays_only_the_invalid_article(self):
        self.collect([self.article(), self.article("第二篇")])
        first, second = self.db.execute("SELECT id FROM articles ORDER BY id").fetchall()
        inference = self.patch("label_batch", return_value={"results": {first["id"]: {"tone": "neutral"}}})
        news.label(self.db, self.now, 32)
        self.now += timedelta(seconds=61)
        news.label(self.db, self.now, 32)
        self.assertEqual(inference.call_count, 1)
        self.now += timedelta(minutes=1)
        inference.return_value = {"results": {second["id"]: {"tone": "neutral"}}}
        news.label(self.db, self.now, 32)
        self.assertEqual(news.progress(self.db)["labelled"], 2)

    def test_shared_model_cooldown_does_not_consume_label_attempts_or_usage(self):
        self.collect([self.article()])
        inference = self.patch("label_batch", side_effect=news.ModelFailure("shared_model_cooldown", 900))
        news.label(self.db, self.now, 32)
        self.assertEqual(self.db.execute("SELECT tries FROM articles").fetchone()[0], 0)
        status = news.progress(self.db)
        self.assertEqual(status["usage"]["calls"], 0)
        self.assertEqual(datetime.fromisoformat(status["label_retry_at"]), self.now + timedelta(minutes=15))
        self.now += timedelta(minutes=14)
        news.label(self.db, self.now, 32)
        self.assertEqual(inference.call_count, 1)
