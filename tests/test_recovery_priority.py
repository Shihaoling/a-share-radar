"""Priority rules for outstanding collection gaps."""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy" / "tencent-preview"))
sys.path.insert(0, str(ROOT))
if "fcntl" not in sys.modules and sys.platform == "win32":
    stub = ModuleType("fcntl")
    stub.LOCK_EX = 2
    stub.LOCK_UN = 8
    stub.LOCK_NB = 4
    stub.flock = lambda *args, **kwargs: None
    sys.modules["fcntl"] = stub

from pipeline.calendar import SHANGHAI  # noqa: E402
from recovery_priority import (GapLedger, WEIGHTS, due_stamp, epoch,  # noqa: E402
                               score, MAX_CONSECUTIVE_DEFERRALS)


def stamp(days_ago=0, hour=15):
    target = datetime(2026, 11, 24, hour, 0, tzinfo=SHANGHAI) - timedelta(days=days_ago)
    return target.timestamp()


NOW = datetime(2026, 11, 24, 18, 0, tzinfo=SHANGHAI).timestamp()


class PriorityScoreTests(unittest.TestCase):
    def test_quote_always_outranks_other_kinds_even_when_they_are_very_old(self):
        fresh_quote = score("quote", NOW)
        for kind in ("news", "announcement", "research", "other"):
            with self.subTest(kind=kind):
                ancient = score(kind, NOW - 365 * 86400, NOW)
                self.assertGreater(fresh_quote, ancient)
        self.assertGreater(WEIGHTS["quote"], max(WEIGHTS[k] for k in WEIGHTS if k != "quote"))

    def test_older_gap_of_same_kind_scores_higher(self):
        recent = score("quote", NOW - 60, NOW)
        older = score("quote", NOW - 6 * 3600, NOW)
        oldest = score("quote", NOW - 3 * 86400, NOW)
        self.assertGreater(older, recent)
        self.assertGreater(oldest, older)

    def test_age_bonus_saturates_after_cap(self):
        at_cap = score("news", NOW - 7 * 86400, NOW)
        beyond_cap = score("news", NOW - 60 * 86400, NOW)
        self.assertAlmostEqual(at_cap, beyond_cap)

    def test_due_stamp_uses_session_close_for_plain_dates(self):
        self.assertEqual(due_stamp("2026-11-24"), stamp(0))
        self.assertEqual(due_stamp("2026-11-23"), stamp(1))
        self.assertEqual(due_stamp("2026-11-24T10:35"),
                         datetime(2026, 11, 24, 10, 35, tzinfo=SHANGHAI).timestamp())
        self.assertAlmostEqual(epoch(NOW), NOW)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.book = GapLedger(Path(self.directory.name) / "gaps.sqlite3")

    def test_first_sighting_is_preserved_and_resolved_gap_restarts(self):
        self.book.observe("quote", [("q1", NOW - 7200)], now=NOW)
        self.book.observe("quote", ["q1"], now=NOW + 3600)
        row = self.book.pending("quote", NOW + 3600)[0]
        self.assertAlmostEqual(row["missing_since"], NOW - 7200)
        self.book.resolve("quote", ["q1"], now=NOW + 4000)
        self.assertEqual(self.book.pending("quote", NOW + 4000), [])
        self.book.observe("quote", ["q1"], now=NOW + 5000)
        row = self.book.pending("quote", NOW + 5000)[0]
        self.assertAlmostEqual(row["missing_since"], NOW + 5000)

    def test_rank_puts_oldest_first_and_quotes_first_across_kinds(self):
        self.book.observe("news", [("news:new", NOW - 60), ("news:old", NOW - 5 * 86400)], now=NOW)
        self.assertEqual(self.book.rank("news", ["news:new", "news:old"], NOW),
                         ["news:old", "news:new"])
        self.book.observe("quote", [("quote:today", NOW - 60)], now=NOW)
        queue = self.book.pending(now=NOW)
        self.assertEqual(queue[0]["kind"], "quote")
        self.assertEqual([row["kind"] for row in queue].count("quote"), 1)

    def test_low_priority_window_defers_only_while_quote_gap_is_urgent(self):
        self.assertFalse(self.book.should_defer("news", NOW))
        # A freshly noticed quote gap does not stop the news backfill yet.
        self.book.observe("quote", [("q1", NOW - 60)], now=NOW)
        self.assertFalse(self.book.should_defer("news", NOW))
        # Once the quote gap survived a recovery window, two windows yield.
        later = NOW + 20 * 60
        self.book.observe("quote", ["q1"], now=later)
        outcomes = [self.book.should_defer("news", later) for _ in range(MAX_CONSECUTIVE_DEFERRALS + 1)]
        self.assertEqual(outcomes, [True] * MAX_CONSECUTIVE_DEFERRALS + [False])
        # A gap last seen long ago no longer counts as active pressure.
        self.assertFalse(self.book.should_defer("news", later + 20 * 60))

    def test_permanently_old_history_cannot_starve_other_kinds(self):
        old = NOW - 3 * 86400
        self.book.observe("quote", [("historic", old)], now=NOW)
        self.assertFalse(self.book.should_defer("announcement", NOW))

    def test_summary_exposes_queue_and_policy(self):
        self.book.observe("quote", [("q1", NOW - 3600)], now=NOW)
        self.book.observe("news", [("n1", NOW - 86400)], now=NOW)
        report = self.book.summary(NOW)
        self.assertEqual(report["open_gaps"], 2)
        self.assertEqual(report["by_kind"], {"quote": 1, "news": 1})
        self.assertTrue(report["policy"]["quote_always_first"])
        self.assertEqual(report["queue"][0]["kind"], "quote")


class CollectorIntegrationTests(unittest.TestCase):
    def test_minute_recovery_takes_the_oldest_quote_gap_in_the_next_window(self):
        import sqlite3
        from contextlib import closing
        import minute_recovery as recovery

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        book = GapLedger(Path(directory.name) / "gaps.sqlite3")
        with closing(sqlite3.connect(Path(directory.name) / "minutes.sqlite3")) as db:
            db.executescript(recovery.SCHEMA)
            db.execute("CREATE TABLE samples(id TEXT, minute TEXT, price REAL)")
            db.executemany("INSERT INTO minute_recovery_tasks(day,id,code) VALUES(?,?,?)",
                           [("2026-11-20", "sh000001", "000001.SH"),
                            ("2026-11-23", "sh000001", "000001.SH")])
            db.commit()
            now = datetime(2026, 11, 24, 18, 0, tzinfo=SHANGHAI)
            recovery.register_gaps(db, book, now)
            task = recovery.next_task(db, now, book)
        self.assertIsNotNone(task)
        self.assertEqual(task[0], "2026-11-20")

    def test_priority_ledger_is_shared_between_collectors(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "gaps.sqlite3"
        producer = GapLedger(path)
        consumer = GapLedger(path)
        producer.observe("announcement", [("announcement|2026-11-20", NOW - 4 * 86400)], now=NOW)
        producer.observe("quote", [("index_minute|2026-11-24|sh000001", NOW - 60)], now=NOW)
        queue = consumer.pending(now=NOW)
        self.assertEqual([row["kind"] for row in queue], ["quote", "announcement"])
        self.assertEqual(consumer.rank("announcement", ["announcement|2026-11-20"], NOW),
                         ["announcement|2026-11-20"])


if __name__ == "__main__":
    unittest.main()
