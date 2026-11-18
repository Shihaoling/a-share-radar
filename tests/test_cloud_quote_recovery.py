"""Observed exchange minutes, immutable samples and latest-quote recovery."""
from contextlib import nullcontext
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

if sys.platform == "win32":
    raise unittest.SkipTest("The Tencent worker uses POSIX file locking")

with patch.object(sys, "path", [str(Path(__file__).resolve().parents[1] / "deploy/tencent-preview"), *sys.path]):
    import relay_quotes as relay


DAY = "2026-11-19"
INDEX = "sh000001"


def now(clock):
    return datetime.fromisoformat(f"{DAY}T{clock}+08:00")


def quote(clock, price, symbol=INDEX):
    return {"id": symbol, "quote_timestamp": now(clock).isoformat(), "price": price, "change_pct": 1.0}


class RelayRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE samples(id TEXT, minute TEXT, source_stamp TEXT, price REAL, change_pct REAL,
                                 PRIMARY KEY(id,minute)) WITHOUT ROWID;
            CREATE TABLE retries(api TEXT,minute TEXT,due TEXT,error TEXT,status TEXT,PRIMARY KEY(api,minute))
                                 WITHOUT ROWID;
        """)

    def retry(self, api, clock, status="awaiting_retry"):
        self.db.execute("INSERT INTO retries VALUES (?,?, '', 'rate_limited', ?)", (api, f"{DAY}T{clock}", status))

    def statuses(self, api):
        return dict(self.db.execute("SELECT substr(minute,12),status FROM retries WHERE api=?", (api,)))

    def test_late_index_quote_fills_chart_and_model_source_without_regressing_latest(self):
        self.retry("rt_idx_k", "10:00")
        self.retry("rt_idx_k", "10:01")
        latest = {INDEX: {"id": INDEX}}
        with self.db:
            self.assertTrue(relay.store_quotes(self.db, "rt_idx_k", [quote("10:02:16", 102)], latest, [INDEX]))
        self.assertEqual(self.statuses("rt_idx_k")["10:01"], "unrecoverable")
        with self.db:
            self.assertTrue(relay.store_quotes(self.db, "rt_idx_k", [quote("10:01:54", 101)], latest, [INDEX]))
            self.assertFalse(relay.store_quotes(self.db, "rt_idx_k", [quote("10:01:01", 99)], latest, [INDEX]))
        self.assertEqual(latest[INDEX], quote("10:02:16", 102))
        self.assertEqual(self.statuses("rt_idx_k"), {"10:00": "unrecoverable", "10:01": "recovered"})
        rows = self.db.execute("SELECT id,source_stamp,price FROM samples WHERE minute=?", (f"{DAY}T10:01",)).fetchall()
        self.assertEqual(rows, [(INDEX, now("10:01:54").isoformat(), 101)])
        with TemporaryDirectory() as folder:
            path = Path(folder)/"today-intraday.json"
            relay.publish_intraday(path, self.db, DAY, {INDEX})
            chart = json.loads(path.read_text())
        self.assertEqual(chart["series"][INDEX], [[now("10:01:54").isoformat(), 101], [now("10:02:16").isoformat(), 102]])
        self.assertEqual(chart["last_sample"], now("10:02:16").isoformat())

    def test_stock_retry_records_observation_without_claiming_stock_history(self):
        self.retry("rt_k", "10:01")
        latest = {"600000.SH": quote("10:02:20", 12, "600000.SH")}
        with self.db:
            self.assertFalse(relay.store_quotes(self.db, "rt_k", [quote("10:01:45", 11, "600000.SH")], latest, [INDEX]))
        self.assertEqual(latest["600000.SH"]["price"], 12)
        self.assertEqual(self.statuses("rt_k")["10:01"], "observed_snapshot_only")
        self.assertEqual(self.db.execute("SELECT count(*) FROM samples").fetchone()[0], 0)

    def test_recovery_requires_persisted_index_sample_and_rechecks_legacy_statuses(self):
        for minute in ("10:00", "10:01"):
            self.retry("rt_idx_k", minute, "recovered")
        self.retry("rt_k", "10:00", "recovered")
        self.db.executemany("INSERT INTO samples VALUES(?,?,?,?,?)", [
            ("801780.SI", f"{DAY}T10:00", now("10:00:11").isoformat(), 88, .3),
            (INDEX, f"{DAY}T10:01", now("10:01:11").isoformat(), 88, .3)])
        relay.reconcile_retry_history(self.db, [INDEX])
        relay.settle_retries(self.db, "rt_idx_k", {f"{DAY}T10:00"}, [INDEX])
        self.assertEqual(self.statuses("rt_idx_k"), {"10:00": "unverified", "10:01": "recovered"})
        self.assertEqual(self.statuses("rt_k"), {"10:00": "observed_snapshot_only"})
        self.db.execute("UPDATE retries SET status='awaiting_retry' WHERE minute=?", (f"{DAY}T10:01",))
        relay.store_quotes(self.db, "rt_idx_k", [quote("10:02:02", 90)], {}, [INDEX])
        self.assertEqual(self.statuses("rt_idx_k")["10:01"], "recovered")

    def test_one_response_can_supply_multiple_real_source_times_for_one_symbol(self):
        base = {"ts_code": "000001.SH", "name": "上证指数", "open": 100, "high": 103,
                "low": 95, "pre_close": 100, "vol": 30, "amount": 50}
        client = Mock()
        client.request.return_value = [
            {**base, "trade_time": f"{DAY} 10:02:12", "close": 102},
            {**base, "trade_time": f"{DAY} 10:01:45", "close": 101},
            {**base, "trade_time": f"{DAY} 10:01:45", "close": 101.5},
            {**base, "trade_time": f"{DAY} 10:06:15", "close": 99}]
        with patch.object(relay, "relay_client", return_value=nullcontext(client)), \
                patch.object(relay, "shanghai_now", return_value=now("10:02:30")):
            rows = relay.collect("fixture", "rt_idx_k", {"000001.SH": INDEX})
        client.request.assert_called_once_with("rt_idx_k", {"ts_code": "000001.SH"})
        self.assertEqual([(r["quote_timestamp"],r["price"]) for r in rows],
                         [(now("10:02:12").isoformat(), 102), (now("10:01:45").isoformat(), 101)])
        latest = {}
        relay.store_quotes(self.db, "rt_idx_k", rows, latest, [INDEX])
        self.assertEqual(latest[INDEX]["price"], 102)
        self.assertEqual(self.db.execute("SELECT count(*) FROM samples").fetchone()[0], 2)

    def test_day_rollover_preserves_evidence_after_later_same_minute_request_failed(self):
        relay.store_quotes(self.db, "rt_idx_k", [quote("14:59:04", 102)], {}, [INDEX])
        self.retry("rt_idx_k", "14:58")
        self.retry("rt_idx_k", "14:59")
        self.retry("rt_k", "14:59")
        relay.expire_prior_day_retries(self.db, "2026-11-20", [INDEX])
        self.assertEqual(self.statuses("rt_idx_k"), {"14:58": "unrecoverable", "14:59": "recovered"})
        self.assertEqual(self.statuses("rt_k"), {"14:59": "unrecoverable"})
