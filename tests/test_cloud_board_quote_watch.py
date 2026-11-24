from contextlib import ExitStack
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy/tencent-preview"))
from board_quote_watch import BoardQuoteWatch


def stocks(start, count):
    return [f"{num:06}.SZ" for num in range(start, start + count)]


class BoardQuoteWatchTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.watch = BoardQuoteWatch(root / "board-watch.json", root)
        self.stamp = 1000
        self.board = "885864.TI"

    def roster(self, board, codes):
        p = self.watch.members_dir / (board + ".json")
        p.write_text(json.dumps({"code": board, "source": "sxdata_sector_members", "status": "ready",
                                 "members": [{"code": c} for c in codes]}))
        return p

    def active(self, *boards):
        self.watch.watch_file.write_text(json.dumps({"sectors": [[b, self.stamp] for b in boards]}))
        self.watch.refresh(self.stamp)

    def test_typical_board_fits_existing_batch_with_direct_and_core_first(self):
        members = stocks(318, 81)
        core, direct = dict.fromkeys(stocks(0, 354)), dict.fromkeys(stocks(5000, 24))
        self.roster(self.board, members + [members[0], "123456.NQ", None])
        self.active(self.board)
        base = {**core, **direct}
        plan = self.watch.choose(base, self.stamp)
        requested = list({**base, **plan[0]})
        self.assertEqual(requested[:378], list(base))
        self.assertEqual(len(requested), 423)
        self.assertEqual(self.watch.tracked_codes, set(members))
        self.assertTrue(set(members).issubset(requested))

    def test_hard_cap_fair_rotation_and_deferred_turn_not_consumed(self):
        boards = [f"88500{i}.TI" for i in range(9)]
        for i, board in enumerate(boards):
            self.roster(board, stocks(i * 100, 30))
        self.active(*boards)
        base = dict.fromkeys(stocks(5000, 477))
        seen = set()
        for _ in range(80):
            plan = self.watch.choose(base, self.stamp)
            self.assertEqual(plan, self.watch.choose(base, self.stamp))
            self.assertEqual(len(plan[0]), 3)
            seen.update(plan[0])
            self.watch.finish(self.stamp, plan, base, base.keys() | plan[0].keys())
        self.assertEqual(seen, self.watch.tracked_codes)
        self.assertEqual(len(seen), 8 * 30)

    def test_provider_truncation_rotates_through_all_stocks_then_recovers(self):
        members = stocks(1000, 81)
        self.roster(self.board, members)
        self.active(self.board)
        core = dict.fromkeys(stocks(0, 354))
        seen = set()
        for _ in range(5):
            plan = self.watch.choose(core, self.stamp)
            returned = set(list(plan[0])[:31])
            seen.update(returned)
            self.watch.finish(self.stamp, plan, core, core.keys() | returned)
        self.assertEqual(seen, set(members))
        self.assertEqual(self.watch.limit, 31)
        for _ in range(130):
            plan = self.watch.choose(core, self.stamp)
            self.watch.finish(self.stamp, plan, core, core.keys() | plan[0].keys())
        self.assertEqual(self.watch.limit, self.watch.max_extra)

    def test_core_loss_or_failure_pauses_only_additions_and_resumes(self):
        self.roster(self.board, stocks(500, 150))
        self.active(self.board)
        core = dict.fromkeys(stocks(0, 354))
        plan = self.watch.choose(core, self.stamp)
        self.watch.finish(self.stamp, plan, core, set(list(core)[:300]) | plan[0].keys())
        self.assertFalse(self.watch.choose(core, self.stamp + 59)[0])
        after = self.watch.choose(core, self.stamp + 60)
        self.assertEqual(len(after[0]), 48)
        self.assertTrue(after[0].keys().isdisjoint(plan[0]))
        self.watch.finish(self.stamp + 60, after, core, set())
        self.assertFalse(self.watch.choose(core, self.stamp + 119)[0])
        self.assertEqual(len(self.watch.choose(core, self.stamp + 120)[0]), 24)

    def test_pending_roster_update_missing_file_transient_corruption_then_expiry(self):
        self.active(self.board, "../other")
        self.assertFalse(self.watch.tracked_codes)
        p = self.roster(self.board, stocks(10, 5))
        self.watch.refresh(self.stamp)
        self.assertEqual(self.watch.tracked_codes, set(stocks(10, 5)))
        self.watch.watch_file.write_text("{")
        p.write_text("{")
        self.watch.refresh(self.stamp + 5)
        self.assertEqual(self.watch.tracked_codes, set(stocks(10, 5)))
        self.watch.refresh(self.stamp + 151)
        self.assertFalse(self.watch.tracked_codes)
        self.assertFalse(self.watch.rosters)

    def run_worker(self, relay, source_clock, receive, tick, slots=None):
        root = self.watch.members_dir
        (root / "sxdata-token").write_text("fixture")
        with ExitStack() as stack:
            stack.enter_context(patch.dict("os.environ", {"CREDENTIALS_DIRECTORY": str(root),
                "RADAR_EXPECTED_STOCK_KEY_SHA256": sha256(b"fixture").hexdigest()}))
            stack.enter_context(patch.multiple(relay, STATE=root, SOURCE=root / "view.json"))
            stack.enter_context(patch("market_daily.read_daily", return_value={}))
            stack.enter_context(patch("market_daily.merge_daily"))
            stack.enter_context(patch.object(relay, "shanghai_now", side_effect=source_clock))
            stack.enter_context(patch.object(relay, "restore_gate_cooldowns"))
            stack.enter_context(patch.object(relay, "collect", side_effect=receive))
            if slots is not None:
                stack.enter_context(patch.object(relay.QuoteSchedule, "admit", side_effect=slots))
            stack.enter_context(patch.object(relay.time, "sleep", side_effect=tick))
            with self.assertRaises(StopIteration):
                relay.start(SimpleNamespace(diagnostic=False, close_snapshot=False))

    @unittest.skipIf(sys.platform == "win32", "Tencent worker requires POSIX file locking")
    def test_worker_publishes_real_aging_quotes_and_keeps_core_healthy(self):
        import relay_quotes as relay

        root = self.watch.members_dir
        public = root / "quote-output"
        instant = datetime.fromisoformat("2026-11-19T13:05:12+08:00")
        old_stamp = instant - timedelta(minutes=1)
        core, direct, first, last = stocks(1, 4)
        relay.publish(root / "view.json", {"market": [], "sectors": [
            {"id": "801780.SI", "stocks": [{"provider": "stock_api", "ts_code": core}]}]})
        relay.publish(root / "asset-research/live-watch.json", {"stocks": [[direct, instant.timestamp()]]})
        relay.publish(root / "asset-research/board-watch.json", {"sectors": [[self.board, instant.timestamp()]]})
        relay.publish(public / "sector-members" / (self.board + ".json"), {"code": self.board,
            "source": "sxdata_sector_members", "status": "ready", "members": [{"code": first}, {"code": last}]})

        def quote(code, at):
            return dict(id=code, price=9, change_pct=2, provider="stock_api", quote_date=at.date().isoformat(),
                        quote_timestamp=at.isoformat(), timestamp_quality="source_reported")

        relay.publish(public / "stock-quotes.json", {"source": "sxdata_relay", "stocks": [quote(last, old_stamp)]})
        snapshots, calls = [], []

        def receive(token, api, symbols, gap):
            self.assertEqual(api, "rt_k")
            self.assertEqual(list(symbols)[:2], [core, direct])
            calls.append(set(symbols))
            return [quote(code, instant) for code in symbols if len(calls) != 1 or code != last]

        def tick(delay):
            nonlocal instant
            snapshots.append(json.loads((public / "stock-quotes.json").read_text())["stocks"])
            instant += timedelta(seconds=15)
            if len(snapshots) == 4:
                raise StopIteration

        self.run_worker(relay, lambda: instant, receive, tick, [None, "rt_k", "rt_k", "rt_k"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0], {core, direct, first, last})
        self.assertEqual([len(batch) for batch in calls], [4, 3, 3])
        ticks = [{q["id"]: q["quote_timestamp"] for q in snap} for snap in snapshots]
        self.assertEqual([snap[last] for snap in ticks[:2]], [old_stamp.isoformat()] * 2)
        self.assertGreater(ticks[-1][last], old_stamp.isoformat())
        live = json.loads((public / "live-quotes.json").read_text())
        self.assertEqual(live["collector"]["batches"]["rt_k"], "ok")

    @unittest.skipIf(sys.platform == "win32", "Tencent worker requires POSIX file locking")
    def test_first_recovered_stock_slot_preserves_watch_after_upstream_failures(self):
        import relay_quotes as relay

        root = self.watch.members_dir
        start = instant = datetime.fromisoformat("2026-11-19T13:05:00+08:00")
        core, direct = stocks(1, 2)
        members = stocks(100, 81)
        relay.publish(root / "view.json", {"market": [], "sectors": [
            {"id": "801780.SI", "stocks": [{"provider": "stock_api", "ts_code": core}]}]})
        relay.publish(root / "quote-output/sector-members" / (self.board + ".json"), {
            "code": self.board, "source": "sxdata_sector_members", "status": "ready",
            "members": [{"code": code} for code in members]})

        def heartbeat():
            relay.publish(root / "asset-research/live-watch.json", {"stocks": [[direct, instant.timestamp()]]})
            relay.publish(root / "asset-research/board-watch.json", {"sectors": [[self.board, instant.timestamp()]]})

        failures = [relay._Failure("rate_limited", 60, provider_code=429, host=True),
                    relay._Failure("transport_error", 60), relay._Failure("http_error", 60, host=True)]
        calls = []

        def receive(token, api, symbols, gap):
            if api == "rt_k":
                calls.append((instant, tuple(symbols)))
                if len(calls) <= len(failures):
                    raise failures[len(calls) - 1]
            return [dict(id=code, price=9, change_pct=2, provider="stock_api", quote_date=instant.date().isoformat(),
                         quote_timestamp=instant.isoformat(), timestamp_quality="source_reported") for code in symbols]

        def tick(delay):
            nonlocal instant
            if len(calls) > len(failures):
                raise StopIteration
            instant += timedelta(seconds=1)
            self.assertLess(instant - start, timedelta(minutes=10))
            heartbeat()

        heartbeat()
        self.run_worker(relay, lambda: instant, receive, tick)
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(b[0] - a[0] >= timedelta(seconds=60) for a, b in zip(calls, calls[1:])))
        self.assertTrue(all(symbols == (core, direct, *members) for _, symbols in calls))
        rows = json.loads((root / "quote-output/stock-quotes.json").read_text())["stocks"]
        self.assertEqual({r["id"] for r in rows}, {core, direct, *members})
        self.assertTrue(all(r["quote_timestamp"] == calls[-1][0].isoformat() for r in rows))
