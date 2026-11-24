"""Collector write-lock coordination: busy timeout, WAL opt-in and bounded retry."""
import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy" / "tencent-preview"))
import collector_db


class CollectorDbTests(unittest.TestCase):
    def test_locked_recognizes_busy_and_locked_errors(self):
        for message in ("database is locked", "database is busy", "database table is locked"):
            self.assertTrue(collector_db.locked(sqlite3.OperationalError(message)))
        self.assertFalse(collector_db.locked(sqlite3.OperationalError("no such table")))
        self.assertFalse(collector_db.locked(ValueError("database is locked")))

    def test_retry_locked_recovers_and_stops_on_fourth_failure(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        self.assertEqual(collector_db.retry_locked(flaky, attempts=4, base=0.01), "ok")
        self.assertEqual(len(calls), 3)

        def always_locked():
            calls.append(1)
            raise sqlite3.OperationalError("database is locked")

        with self.assertRaises(sqlite3.OperationalError):
            collector_db.retry_locked(always_locked, attempts=3, base=0.01)

    def test_connect_applies_busy_timeout_and_writes(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "sample.sqlite3"
            db = collector_db.connect(path)
            try:
                self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], 30000)
                db.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, value TEXT)")
                collector_db.commit(db)
                db.execute("INSERT INTO items VALUES(1,'ok')")
                collector_db.commit(db)
                self.assertEqual(db.execute("SELECT value FROM items").fetchone()[0], "ok")
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
