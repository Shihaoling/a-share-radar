"""Offline minute recovery tests with an observed-quote database and an in-memory provider."""
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy/tencent-preview"))
import minute_recovery as recovery
from pipeline.calendar import SHANGHAI


class RecoveryTests(unittest.TestCase):
    day = "2026-11-19"
    code = "000001.SH"
    symbol = "sh000001"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.output = self.path / "minute-recovery.json"
        self.db = sqlite3.connect(self.path / "quotes.sqlite3")
        self.addCleanup(self.db.close)
        self.db.execute("""CREATE TABLE samples(id TEXT,minute TEXT,source_stamp TEXT,price REAL,change_pct REAL,
                                PRIMARY KEY(id,minute)) WITHOUT ROWID""")
        self.now = self.at("09:34")
        self.symbols = {self.code: self.symbol}
        self.calls = []
        self.response = []
        self.error = None

    def at(self, time, day=None):
        return datetime.fromisoformat(f"{day or self.day}T{time}").replace(tzinfo=SHANGHAI)

    def observe(self, minute, price=12.1, symbol=None, day=None):
        stamp = self.at(minute, day)
        self.db.execute("INSERT OR REPLACE INTO samples VALUES(?,?,?,?,NULL)",
                        (symbol or self.symbol, stamp.isoformat()[:16], stamp.isoformat(), price))
        self.db.commit()

    def row(self, minute, price=12.3, code=None, day=None):
        return {"ts_code": code or self.code, "trade_time": self.at(minute, day).isoformat(), "close": price}

    def request(self, api, params):
        self.calls.append((api, params))
        if self.error:
            raise self.error
        return self.response

    @contextmanager
    def client(self, borrow_slot):
        yield self

    def cycle(self, client=None):
        result = recovery.run(self.db, self.output, self.symbols, client or self.client, lambda: self.now)
        payload = json.loads(self.output.read_text())
        self.assertEqual(payload["status"], result["status"])
        return payload

    def test_partial_recovery_remains_available_during_late_live_publication(self):
        self.observe("09:31", price=50)
        self.response = [None, {}, self.row("09:31", 8), self.row("09:33", 11),
                         self.row("09:33", 11), self.row("09:34", float("nan")), self.row("09:35"),
                         self.row("09:32:32"), self.row("11:45"), self.row("09:32", code="399001.SZ"),
                         self.row("09:32", day="2026-11-18")]
        result = self.cycle()
        self.assertEqual(self.calls, [("idx_mins", {"ts_code": self.code, "freq": "1min", "start_date":
                                    self.day+" 09:30:00", "end_date": self.day+" 09:34:00", "limit": 300})])
        self.assertEqual(result["series"], {self.symbol: [[self.at("09:33").isoformat(), 11.0]]})
        self.assertEqual(result["source"], "sxdata_minute_recovery")
        self.assertEqual(result["last_sample"], self.at("09:33").isoformat())
        self.assertEqual(result["coverage"], {"elapsed_minutes": 4, "symbols": 1, "expected_symbol_minutes": 4,
                                              "missing_symbol_minutes": 2, "recovered_symbol_minutes": 1})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(self.db.execute("SELECT available_at FROM minute_recovery").fetchone()[0], self.now.isoformat())
        self.assertEqual(self.db.execute("SELECT price FROM samples").fetchall(), [(50.0,)])
        self.observe("09:33:38", price=15)
        after = self.cycle()
        self.assertEqual(after["series"], result["series"])
        self.assertEqual(after["last_sample"], result["last_sample"])
        self.assertEqual(after["coverage"]["recovered_symbol_minutes"], 1)
        self.assertEqual(after["coverage"]["missing_symbol_minutes"], 2)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.db.execute("SELECT price FROM samples WHERE minute LIKE '%09:33'").fetchone(), (15.0,))

    def test_last_success_counts_empty_invalid_known_and_new_minutes_on_existing_database(self):
        self.observe("09:31:45")
        self.cycle()
        # Restore the pre-diagnostics layout with an existing task and unchanged samples.
        self.db.execute("DROP TABLE minute_recovery_last_response")
        self.db.commit()
        query = """SELECT received_at,response_rows,valid_minutes,added_minutes
                   FROM minute_recovery_last_response WHERE day=? AND id=?"""
        cases = [
            ([], (0, 0, 0)),
            ([self.row("09:32:20"), self.row("09:32", code="399001.SZ")], (2, 0, 0)),
            ([self.row("09:31"), self.row("09:31")], (2, 1, 0)),
            ([self.row("09:31"), self.row("09:32"), self.row("09:32")], (3, 2, 1)),
        ]
        for rows, expected in cases:
            self.now += timedelta(hours=2)
            self.response = rows
            self.cycle()
            last = self.db.execute(query, (self.day, self.symbol)).fetchone()
            self.assertEqual(last, (self.now.isoformat(), *expected))
        self.now += timedelta(hours=2)
        self.error = recovery._Failure("transport_error", 60)
        self.cycle()
        self.assertEqual(self.db.execute(query, (self.day, self.symbol)).fetchone(), last)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM minute_recovery_last_response").fetchone(), (1,))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM samples").fetchone(), (1,))

    def test_full_clock_coverage_skips_provider_and_excludes_lunch(self):
        self.now = self.at("17:31")
        window = recovery.elapsed_minutes(self.day, self.now)
        self.assertEqual(len(window.expected), 240)
        self.assertTrue(all(m not in window.expected for m in
                            (self.day+"T11:30", self.day+"T12:00", self.day+"T15:00")))
        self.db.executemany("INSERT INTO samples VALUES (?,?,?,?,NULL)",
                            ((self.symbol, s, s+":01+08:00", 21) for s in window.expected))
        result = self.cycle()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["coverage"]["missing_symbol_minutes"], 0)
        self.assertEqual(self.calls, [])

    def test_neighboring_real_timestamps_do_not_fill_missing_opening_clock_minute(self):
        self.now = self.at("09:32:30")
        self.observe("09:31:55")
        self.response = [self.row("09:32")]
        result = self.cycle()
        self.assertEqual(result["coverage"]["elapsed_minutes"], 2)
        self.assertEqual(result["coverage"]["missing_symbol_minutes"], 1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["series"][self.symbol], [[self.at("09:32").isoformat(), 12.3]])
        self.assertEqual(self.db.execute("SELECT complete FROM minute_recovery_tasks").fetchone(), (0,))

    def test_rollover_fairly_rotates_today_and_old_pending_partial(self):
        yesterday = "2026-11-18"
        self.symbols["399001.SZ"] = "sz399001"
        self.observe("10:00", day=yesterday)
        self.now = self.at("15:42", yesterday)
        self.response = [self.row("09:31", day=yesterday)]
        first = self.cycle()
        self.assertEqual(first["trade_date"], yesterday)
        self.assertTrue(first["series"])
        self.now = self.at("10:02")
        self.response = []
        previous = self.cycle()
        self.assertEqual(previous["trade_date"], self.day)
        self.assertEqual(previous["series"], {})
        self.assertIsNone(previous["last_sample"])
        self.now += timedelta(seconds=61)
        self.cycle()
        self.assertEqual([q["start_date"][:10] for _, q in self.calls], [yesterday, yesterday, self.day])
        self.now += timedelta(seconds=61)
        self.cycle()
        self.assertEqual(self.calls[-1][1]["start_date"][:10], yesterday)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM minute_recovery_tasks WHERE complete=0").fetchone()[0], 4)

    def test_tied_jobs_prioritize_homepage_then_yield_to_untried_industries(self):
        self.symbols = {"000032.SH": "000032.SH", "399001.SZ": "sz399001",
                        "000300.SH": "sh000300", "000001.SH": "sh000001"}
        for _ in self.symbols:
            self.cycle()
            # Older tasks are eligible again, but still follow each untried task.
            self.now += timedelta(minutes=6)
        codes = [params["ts_code"] for api, params in self.calls]
        self.assertEqual(codes[0], "000001.SH")
        self.assertEqual(set(codes[1:3]), {"399001.SZ", "000300.SH"})
        self.assertEqual(codes[3:], ["000032.SH"])
        self.assertTrue(all(api == "idx_mins" for api, params in self.calls))

    def test_provider_cooldown_persists_with_empty_jobs_and_true_attempt_counts(self):
        self.symbols["399001.SZ"] = "sz399001"
        self.error = recovery._Failure("rate_limited", 7200, host=True)
        self.assertEqual(self.cycle()["status"], "cooldown")
        self.assertEqual(self.db.execute("SELECT SUM(attempts) FROM minute_recovery_tasks").fetchone(), (1,))
        self.now += timedelta(hours=1)
        self.assertEqual(self.cycle()["status"], "cooldown")
        self.assertEqual(len(self.calls), 1)
        self.now = self.at("13:00")
        self.error = None
        self.cycle()
        self.assertEqual(len(self.calls), 2)
        self.assertNotEqual(self.calls[0][1]["ts_code"], self.calls[1][1]["ts_code"])
        self.assertEqual(self.db.execute("SELECT SUM(complete) FROM minute_recovery_tasks").fetchone(), (0,))

    def test_worker_rate_limit_survives_reopen_and_lunch_only_recovers_closed_bars(self):
        self.symbols["399001.SZ"] = "sz399001"
        self.cycle()
        self.now += timedelta(seconds=59)
        reopened = sqlite3.connect(self.path / "quotes.sqlite3")
        try:
            recovery.run(reopened, self.output, self.symbols, self.client, lambda: self.now)
        finally:
            reopened.close()
        self.assertEqual(len(self.calls), 1)
        self.now += timedelta(seconds=1)
        self.cycle()
        self.assertEqual(len(self.calls), 2)
        self.now = self.at("12:20")
        result = self.cycle()
        self.assertEqual(result["coverage"]["elapsed_minutes"], 120)
        self.assertEqual(self.calls[-1][1]["end_date"], self.day + " 11:30:00")
        self.assertEqual(len(self.calls), 3)

    def test_busy_relay_and_shared_cooldown_do_not_consume_any_attempt(self):
        gate = self.path / "gate.json"
        with patch.dict(os.environ, {"RADAR_SERVICE_GATE_FILE": str(gate)}):
            with gate.with_suffix(".lock").open("a") as held:
                recovery.fcntl.flock(held, recovery.fcntl.LOCK_EX | recovery.fcntl.LOCK_NB)
                self.assertEqual(self.cycle(lambda borrow: recovery.low_priority_client("test", borrow))["status"], "waiting")
            with patch.object(recovery, "gate_state", return_value=(1800, 0, "rate_limited")):
                result = self.cycle(lambda borrow: recovery.low_priority_client("test", borrow))
        self.assertEqual(result["status"], "cooldown")
        self.assertEqual(self.db.execute("SELECT SUM(attempts) FROM minute_recovery_tasks").fetchone(), (0,))
        self.assertEqual(recovery.meta(self.db, "next_request"), "")

    def test_five_second_live_traffic_yields_one_bounded_slot_after_persistent_deferrals(self):
        from pipeline import data_service, stock_api

        gate = self.path / "shared-gate.json"
        starts = []
        def admit(api, params):
            stock_api._service_before(api, 11, 6)
            starts.append(self.now.timestamp())
            return [self.row("09:32")]
        def advance(seconds):
            self.now += timedelta(seconds=seconds)
        with (patch.dict(os.environ, {"RADAR_SERVICE_GATE_FILE": str(gate)}),
              patch.object(recovery.time, "time", side_effect=lambda: self.now.timestamp()),
              patch.object(recovery.time, "monotonic", side_effect=lambda: self.now.timestamp()),
              patch.object(recovery.time, "sleep", side_effect=advance),
              patch.object(recovery, "_Client") as client):
            client.return_value.request.side_effect = admit
            client.return_value.last = None
            for tick in range(6):
                self.now = self.at("10:00") + timedelta(minutes=tick)
                # A batch took the latest five-second realtime slot one second ago.
                gate.write_text(json.dumps({"last": self.now.timestamp()-1, "last_api": "rt_k"}))
                result = self.cycle(lambda borrow: recovery.low_priority_client("test", borrow))
                if tick < 5:
                    self.assertEqual(starts, [])
            self.assertEqual(starts, [self.at("10:05:05").timestamp()])
            self.assertEqual(result["coverage"]["recovered_symbol_minutes"], 1)
            self.assertEqual(data_service.gate_state("rt_k", 5)[1], 6)
            self.assertEqual(self.db.execute("SELECT sum(attempts) FROM minute_recovery_tasks").fetchone(), (1,))
            self.assertEqual(float(recovery.meta(self.db, "next_request")), starts[0] + 60)

    def test_response_spanning_midnight_preserves_previous_date_and_receipt_time(self):
        self.now = self.at("23:59:58")
        client = self
        @contextmanager
        def midnight(borrow_slot):
            class Delayed:
                def request(_, api, params):
                    client.now += timedelta(seconds=4)
                    return [client.row("14:58")]
            yield Delayed()
        result = self.cycle(midnight)
        self.assertEqual(result["trade_date"], "2026-11-20")
        self.assertEqual(result["series"], {})
        row = self.db.execute("SELECT source_stamp,available_at FROM minute_recovery").fetchone()
        self.assertEqual(row, (self.at("14:58").isoformat(), self.now.isoformat()))

    def test_end_labelled_response_retains_two_closes_and_retries_true_opening_gaps(self):
        self.now = self.at("16:00")
        window = recovery.elapsed_minutes(self.day, self.now)
        self.observe("09:31", price=50)
        self.response = [self.row(label[11:]) for label in sorted(window.bar_ends)]
        result = self.cycle()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["coverage"]["recovered_symbol_minutes"], 239)
        self.assertEqual(result["coverage"]["missing_symbol_minutes"], 2)
        points = dict(result["series"][self.symbol])
        self.assertIn(self.at("11:30").isoformat(), points)
        self.assertIn(self.at("15:00").isoformat(), points)
        # Simulate a closed task from an old database and re-evaluate it on upgrade.
        self.db.execute("UPDATE minute_recovery_tasks SET complete=1,retry_at=?", (self.now.timestamp()+86400,))
        self.db.execute("DELETE FROM minute_recovery_meta WHERE key='coverage_policy'")
        self.db.commit()
        self.now += timedelta(minutes=2)
        self.cycle()
        self.assertEqual(self.db.execute("SELECT complete,no_progress,attempts FROM minute_recovery_tasks").fetchone(),
                         (0, 1, 2))
        self.now += timedelta(seconds=61)
        self.cycle()
        self.assertEqual(len(self.calls), 2)
        self.now += timedelta(minutes=30)
        self.response = [self.row("09:30"), self.row("13:00")]
        result = self.cycle()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["coverage"]["missing_symbol_minutes"], 0)
        self.assertEqual(self.db.execute("SELECT complete,attempts FROM minute_recovery_tasks").fetchone(), (1, 3))
        self.now += timedelta(hours=1)
        self.cycle()
        self.assertEqual(len(self.calls), 3)

    def test_calendar_seed_catches_downtime_without_weekends(self):
        self.observe("15:00", day="2026-11-13")
        self.cycle()
        days = [r[0] for r in self.db.execute("SELECT DISTINCT day FROM minute_recovery_tasks ORDER BY day")]
        self.assertEqual(days, ["2026-11-13", "2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19"])
        self.assertEqual(len(self.calls), 1)

    def test_changed_market_universe_enrolls_today_and_preserves_earlier_queue(self):
        self.observe("09:32", day="2026-11-18")
        self.cycle()
        market = [{"id": self.symbol}, {"id": "sz399001"}, {"id": "ignored", "kind": "etf"}]
        updated = recovery.index_symbols(market)
        self.assertEqual(updated["399001.SZ"], "sz399001")
        self.assertNotIn("ignored", updated.values())
        with self.assertRaises(ValueError):
            recovery.index_symbols([{"id": "invalid"}])
        self.symbols["399001.SZ"] = updated["399001.SZ"]
        self.now += timedelta(seconds=61)
        self.cycle()
        query = "SELECT day FROM minute_recovery_tasks WHERE id=? ORDER BY day"
        self.assertEqual(self.db.execute(query, ("sz399001",)).fetchall(), [(self.day,)])
        self.now = self.at("10:20", "2026-11-24")
        self.symbols["000300.SH"] = "sh000300"
        self.cycle()
        self.assertEqual(self.db.execute(query, ("sh000300",)).fetchall(), [("2026-11-24",)])
        self.assertEqual(self.db.execute(query, ("sz399001",)).fetchall(),
                         [(self.day,), ("2026-11-20",), ("2026-11-23",), ("2026-11-24",)])

    def test_live_universe_has_exact_exchange_ids(self):
        data = json.loads((ROOT / "web/data/view.json").read_text())
        symbols = recovery.index_symbols(data["market"])
        self.assertEqual(symbols["000001.SH"], "sh000001")
        self.assertEqual(symbols["399001.SZ"], "sz399001")
        self.assertEqual(symbols["000032.SH"], "000032.SH")
        self.assertTrue(all(code.endswith((".SH", ".SZ")) for code in symbols))
