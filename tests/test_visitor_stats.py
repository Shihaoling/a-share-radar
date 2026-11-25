"""Visitor statistics: incremental log ingest, dedupe, retention and snapshot."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from visitor_stats import SHANGHAI, build_snapshot, collect, is_page, parse_line, publish

NOW = datetime(2026, 11, 25, 14, 0, tzinfo=timezone.utc)


def line(at, ip, path, status=200, ua="Mozilla/5.0"):
    return (json.dumps({"at": at, "ip": ip, "host": "mystockmarket.online", "path": path,
                        "status": status, "ua": ua}, ensure_ascii=False) + "\n").encode()


class VisitorStatsTests(unittest.TestCase):
    def test_parse_and_classify(self):
        row = parse_line(line("2026-11-25T14:00:00+00:00", "1.2.3.4", "/")[:-1])
        self.assertEqual(row["ip"], "1.2.3.4")
        self.assertEqual(row["path"], "/")
        self.assertIsNone(parse_line(b"not json\n"))
        self.assertTrue(is_page("/"))
        self.assertTrue(is_page("/sector/"))
        self.assertFalse(is_page("/data/live-quotes.json"))
        self.assertFalse(is_page("/api/admin/visitors"))
        self.assertFalse(is_page("/app.js"))

    def test_incremental_ingest_dedupe_and_snapshot(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            log = root / "visitor-access.log"
            db, state, snap = root / "visits.sqlite3", root / "offset.json", root / "snapshot.json"
            log.write_bytes(
                line("2026-11-25T13:00:00+00:00", "1.1.1.1", "/") +
                line("2026-11-25T13:00:01+00:00", "1.1.1.1", "/data/live-quotes.json") +
                line("2026-11-25T13:00:02+00:00", "2.2.2.2", "/#news") +
                b'{"at":"2026-11-25T13:00:03+00:00"\n'  # incomplete tail
            )
            self.assertEqual(collect(log, db, state, now=NOW), 3)
            self.assertEqual(collect(log, db, state, now=NOW), 0)  # no re-read
            with log.open("ab") as fh:
                fh.write(line("2026-11-25T13:01:00+00:00", "1.1.1.1", "/"))
                fh.write(line("2026-11-25T13:01:00+00:00", "1.1.1.1", "/"))  # duplicate
                fh.write(line("2026-11-25T13:01:01+00:00", "3.3.3.3", "/metrics/"))
            self.assertEqual(collect(log, db, state, now=NOW), 2)  # third line is a dup
            data = build_snapshot(db, now=NOW)
            self.assertEqual(data["schema_version"], 1)
            self.assertEqual(data["totals"]["requests_all"], 5)
            self.assertEqual(data["totals"]["unique_ips_all"], 3)
            self.assertEqual(len(data["hourly_24h"]), 24)
            top = {row["ip"]: row for row in data["top_ips"]}
            self.assertEqual(top["1.1.1.1"]["requests"], 3)
            self.assertEqual(top["1.1.1.1"]["page_views"], 2)  # 3 visits, one is /data/
            self.assertEqual(top["2.2.2.2"]["requests"], 1)
            paths = {row["path"]: row["requests"] for row in data["top_paths"]}
            self.assertNotIn("/data/live-quotes.json", paths)
            self.assertEqual(paths["/metrics/"], 1)
            result = publish(db, snap, now=NOW)
            self.assertEqual(json.loads(snap.read_text(encoding="utf-8"))["totals"]["requests_all"],
                             result["totals"]["requests_all"])

    def test_rotation_resets_offset_and_retention_purges(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            log, db, state = root / "log", root / "visits.sqlite3", root / "offset.json"
            log.write_bytes(line("2026-11-25T13:00:00+00:00", "1.1.1.1", "/"))
            self.assertEqual(collect(log, db, state, now=NOW), 1)
            # simulate log rotation: a brand-new file with a new inode
            log.unlink()
            log.write_bytes(line("2026-11-25T13:10:00+00:00", "4.4.4.4", "/reports/"))
            self.assertEqual(collect(log, db, state, now=NOW), 1)
            old = datetime(2026, 1, 1, tzinfo=timezone.utc)
            log.write_bytes(line("2026-01-01T00:00:00+00:00", "5.5.5.5", "/"))
            collect(log, db, state, now=old)
            collect(log, db, state, now=NOW)  # purge older than retention
            data = build_snapshot(db, now=NOW)
            self.assertNotIn("5.5.5.5", {row["ip"] for row in data["top_ips"]})


if __name__ == "__main__":
    unittest.main()
