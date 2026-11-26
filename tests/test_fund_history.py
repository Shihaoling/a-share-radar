"""History cache contracts: completed sessions, published-code filtering and resumability."""
from datetime import datetime
from pathlib import Path
import json
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "deploy/tencent-preview")]
import fund_history

NOW = datetime.fromisoformat("2026-11-25T16:00:00+08:00")
CODE = "588170.SH"


class FakeClient:
    def __init__(self, calls, rows):
        self.calls = calls
        self.rows = rows

    def request(self, api, params):
        self.calls.append((api, params))
        return self.rows(params["trade_date"])


class FundHistoryTests(unittest.TestCase):
    def test_target_days_use_completed_sessions_oldest_first(self):
        days = fund_history.target_days(NOW, 4)
        self.assertEqual(days, ["2026-11-20", "2026-11-23", "2026-11-24", "2026-11-25"])
        morning = fund_history.target_days(datetime.fromisoformat("2026-11-25T10:00:00+08:00"), 2)
        self.assertEqual(morning, ["2026-11-23", "2026-11-24"])

    def test_clean_day_keeps_only_published_codes_on_the_matching_date(self):
        rows = [
            {"ts_code": CODE, "trade_date": "20261125", "close": 0.616},
            {"ts_code": "512800.SH", "trade_date": "20261125", "close": 1.234},
            {"ts_code": CODE, "trade_date": "20261124", "close": 0.6},
            {"ts_code": CODE, "trade_date": "20261125", "close": 0},
            {"ts_code": "999999.SZ", "trade_date": "20261125", "close": 9.9},
        ]
        self.assertEqual(fund_history.clean_day(rows, "2026-11-25", {CODE}), {CODE: 0.616})

    def test_collect_fetches_missing_days_and_publishes(self):
        with TemporaryOutput() as output:
            calls = []
            rows = lambda day: [{"ts_code": CODE, "trade_date": day.replace("-", ""), "close": 0.6}]
            with patch.dict("os.environ", {"STOCK_API_TOKEN": "synthetic-token"}):
                status = fund_history.collect(NOW, snapshot={"funds": [{"code": CODE}]},
                    client_factory=lambda _: FakeClient(calls, rows),
                    max_requests=40, output=output)
            self.assertEqual(status, "success")
            self.assertEqual(len(calls), 40)
            payload = json.loads(output.read_text())
            self.assertEqual(payload["status"], "success")
            self.assertEqual(len(payload["days"]), 40)
            self.assertEqual(payload["days"][-1], {"date": "2026-11-25", "close": {CODE: 0.6}})

    def test_collect_retries_after_a_respectful_rate_limit(self):
        class FlakyClient(FakeClient):
            def request(self, api, params):
                self.calls.append((api, params))
                if len(self.calls) == 1:
                    raise fund_history._Failure("rate_limited", 1)
                return self.rows(params["trade_date"])

        with TemporaryOutput() as output:
            calls = []
            rows = lambda day: [{"ts_code": CODE, "trade_date": day.replace("-", ""), "close": 0.6}]
            with patch.dict("os.environ", {"STOCK_API_TOKEN": "synthetic-token"}), \
                    patch.object(fund_history.time, "sleep"):
                fund_history.collect(NOW, snapshot={"funds": [{"code": CODE}]},
                    client_factory=lambda _: FlakyClient(calls, rows),
                    max_requests=1, output=output)
            payload = json.loads(output.read_text())
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(payload["days"]), 1)
            self.assertEqual(payload["days"][0]["date"], "2026-11-25")

    def test_collect_reuses_complete_cache_without_network(self):
        with TemporaryOutput() as output:
            cached = fund_history.build_payload(
                [{"date": day, "close": {CODE: 0.6}} for day in fund_history.target_days(NOW)],
                NOW.isoformat(timespec="seconds"), "success")
            fund_history.publish(output, cached)
            calls = []
            with patch.dict("os.environ", {"STOCK_API_TOKEN": "synthetic-token"}):
                status = fund_history.collect(NOW, snapshot={"funds": [{"code": CODE}]},
                    client_factory=lambda _: FakeClient(calls, lambda day: []),
                    max_requests=40, output=output)
            self.assertEqual(status, "success")
            self.assertEqual(calls, [])

    def test_collect_waits_for_a_published_snapshot(self):
        with TemporaryOutput() as output:
            status = fund_history.collect(NOW, snapshot={}, output=output)
            self.assertEqual(status, "pending")
            self.assertFalse(output.exists())


class TemporaryOutput:
    def __enter__(self):
        import tempfile
        self._temp = tempfile.TemporaryDirectory()
        self.path = Path(self._temp.name) / "fund-history.json"
        return self.path

    def __exit__(self, *_):
        self._temp.cleanup()


if __name__ == "__main__":
    unittest.main()
