"""Observed exchange minutes, immutable samples and latest-quote recovery."""
from contextlib import ExitStack, nullcontext
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
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

    def test_restart_rollover_outage_and_fresh_quotes_publish_without_fabricating_fields(self):
        yesterday = "2026-11-18"
        board_a, board_b, board_c = list(relay.INDUSTRIES)[:3]
        stock = "600000.SH"

        def observed(symbol, day, clock, price):
            return {"id": symbol, "price": price, "change_pct": .8, "quote_date": day,
                    "quote_timestamp": f"{day}T{clock}+08:00", "timestamp_quality": "source_reported",
                    "provider": "stock_api", "open": 88}

        with TemporaryDirectory() as folder, ExitStack() as stack:
            root = Path(folder)
            (root/"relay").mkdir()
            for code in (board_a, board_b, board_c):
                for minute, price in [("14:58", 86), ("14:59", 89)]:
                    self.db.execute("INSERT INTO samples VALUES (?,?,?,?,?)",
                        (code, yesterday+"T"+minute, yesterday+"T"+minute+":03+08:00", price, .8))
            self.db.commit()
            disk = sqlite3.connect(root/"relay/minute-observations.sqlite3")
            self.db.backup(disk)
            disk.close()
            pub = root/"quote-output"
            relay.publish(pub/"live-quotes.json", {"source": "sxdata_relay", "market": [], "sectors": [
                {"id": board_a}, observed(board_b, yesterday, "14:59:58", 93), observed(board_c, yesterday, "14:58:20", 87)]})
            relay.publish(pub/"stock-quotes.json", {"source": "sxdata_relay", "stocks": [observed(stock, yesterday, "14:59:11", 38)]})
            relay.publish(root/"view.json", {"market": [], "sectors": [{"id": "801780.SI",
                "stocks": [{"provider": "stock_api", "ts_code": stock}]}]})
            (root/"sxdata-token").write_text("fixture")
            stack.enter_context(patch.dict("os.environ", {"CREDENTIALS_DIRECTORY": folder,
                "RADAR_EXPECTED_STOCK_KEY_SHA256": sha256(b"fixture").hexdigest()}))
            stack.enter_context(patch.object(sys, "path", [str(Path(relay.__file__).parent), *sys.path]))
            stack.enter_context(patch("market_daily.read_daily", return_value={}))
            stack.enter_context(patch("market_daily.merge_daily"))
            stack.enter_context(patch.multiple(relay, STATE=root, SOURCE=root/"view.json"))
            clock = [datetime.fromisoformat(yesterday+"T15:01:00+08:00")]
            stack.enter_context(patch.object(relay, "shanghai_now", side_effect=lambda: clock[0]))
            stack.enter_context(patch.object(relay, "restore_gate_cooldowns"))
            stack.enter_context(patch.object(relay.QuoteSchedule, "admit", side_effect=[None, "rt_idx_k", "rt_idx_k", "rt_k"]))
            collector = stack.enter_context(patch.object(relay, "collect", side_effect=[
                relay._Failure("transport_error", 60), [observed(board_a, DAY, "09:35:00", 100)],
                [observed(stock, DAY, "09:35:05", 39)]]))
            snapshots = []

            def next_tick(_delay):
                snapshots.append(tuple(json.loads((pub/(name+".json")).read_text()) for name in
                                       ("live-quotes", "stock-quotes", "today-intraday")))
                clock[0] = now("09:35:10")
                if len(snapshots) == 4:
                    raise StopIteration

            stack.enter_context(patch.object(relay.time, "sleep", side_effect=next_tick))
            with self.assertRaises(StopIteration):
                relay.start(SimpleNamespace(diagnostic=False, close_snapshot=False))
        self.assertEqual(collector.call_count, 3)
        live, stocks, intraday = snapshots[1]
        self.assertEqual(live["trade_date"], DAY)
        boards = {row["id"]: row for row in live["sectors"]}
        for code in (board_a, board_b, board_c):
            self.assertTrue(boards[code]["stale"])
            self.assertEqual(boards[code]["quote_date"], yesterday)
        self.assertEqual(boards[board_a]["price"], 89)
        self.assertEqual(boards[board_b]["price"], 93)
        self.assertEqual(boards[board_b]["open"], 88)
        self.assertEqual(boards[board_c]["price"], 89)
        for board in [boards[board_a], boards[board_c]]:
            self.assertFalse({"open", "high", "low", "volume", "previous_close"}.intersection(board))
            self.assertEqual(board["quote_timestamp"], yesterday+"T14:59:03+08:00")
        self.assertTrue(stocks["stocks"][0]["stale"])
        self.assertEqual(stocks["stocks"][0]["quote_date"], yesterday)
        self.assertEqual(stocks["board_estimates"], [])
        self.assertEqual(intraday["series"], {})
        fresh = next(row for row in snapshots[2][0]["sectors"] if row["id"] == board_a)
        self.assertEqual((fresh["price"], fresh["quote_date"]), (100, DAY))
        self.assertFalse(fresh.get("stale", False))
        self.assertEqual(snapshots[2][2]["series"][board_a], [[DAY+"T09:35:00+08:00", 100]])
        self.assertEqual(snapshots[3][1]["stocks"][0]["price"], 39)
        self.assertFalse(snapshots[3][1]["stocks"][0].get("stale", False))
