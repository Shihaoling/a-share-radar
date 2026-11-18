"""Daily universe history transport/checkpoint contracts; no live requests."""
from contextlib import contextmanager
from datetime import datetime
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pipeline.equity_data import (
    DailyClient, FIELDS, _Failure, _atomic_json, _parse_payload,
    collect_equity_history, load_history, trading_dates,
)

NOW = datetime.fromisoformat("2026-11-16T19:00:00+08:00")
SECRET = "synthetic-equity-test-token-never-persist"


def row(code="600000.SH", day="20261116"):
    return {"ts_code": code, "trade_date": day, "open": 10, "high": 11,
            "low": 9, "close": 10.5, "pre_close": 10.1,
            "pct_chg": 3.9604, "vol": 100, "amount": 104}


def payload(rows):
    fields = FIELDS.split(",")
    return {"code": 0, "data": {"fields": fields,
            "items": [[record.get(field) for field in fields] for record in rows]}}


def dashboard():
    return {"market": [{"history": [
        {"date": "2026-11-12", "close": 3000},
        {"date": "2026-11-13", "close": 3001},
        {"date": "2026-11-16", "close": 3002},
    ]}], "stock_api_state": {"calendar": {"records": [
        {"cal_date": "20261114", "is_open": 0},
        {"cal_date": "20261117", "is_open": 1},
    ]}}}


class Response:
    status = 200
    headers = {}

    def __init__(self, value, url="http://tus.sxdata.top/tu_share/"):
        self.body = json.dumps(value).encode()
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def geturl(self):
        return self.url

    def read(self, size):
        return self.body[:size]


class EquityDataTests(unittest.TestCase):
    def test_dates_exclude_unfinished_today_and_future(self):
        self.assertEqual(trading_dates(dashboard(), NOW, days=2), ["2026-11-16", "2026-11-13"])
        self.assertEqual(trading_dates(dashboard(), NOW.replace(hour=14)), ["2026-11-13", "2026-11-12"])
        self.assertEqual(trading_dates({}, NOW), [])

    def test_parse_preserves_ex_rights_reference_and_units(self):
        rows, quality, complete = _parse_payload(payload([row()]), "2026-11-16")
        self.assertTrue(complete)
        self.assertEqual(rows[0]["pre_close"], 10.1)
        self.assertEqual(rows[0]["amount_unit"], "CNY_1000")
        self.assertEqual(rows[0]["date"], "2026-11-16")
        self.assertIsNone(rows[0]["adj_factor"])
        self.assertEqual(quality["membership_completeness"], "not_verified")

    def test_boundary_and_duplicates_do_not_enter_training(self):
        records = [row("60%04d.SH" % number) for number in range(3000)]
        _, quality, complete = _parse_payload(payload(records), "2026-11-16")
        self.assertFalse(complete)
        self.assertTrue(quality["possibly_truncated"])
        _, quality, complete = _parse_payload(payload([row(), row()]), "2026-11-16")
        self.assertFalse(complete)
        self.assertEqual(quality["duplicate_count"], 1)

    def test_wrong_day_non_a_share_and_nonfinite_rejected(self):
        invalid = [row(day="20261113"), row("510300.SH"), {**row(), "close": float("nan")}]
        _, quality, complete = _parse_payload(payload(invalid), "2026-11-16")
        self.assertFalse(complete)
        self.assertEqual(quality["rejected_count"], 2)
        self.assertEqual(quality["filtered_count"], 1)

    def test_non_a_security_does_not_drop_valid_batch(self):
        rows, quality, complete = _parse_payload(payload([row(), row("510300.SH")]), "2026-11-16")
        self.assertTrue(complete)
        self.assertEqual([item["ts_code"] for item in rows], ["600000.SH"])
        self.assertEqual(quality["reason_counts"], {"excluded_out_of_scope": 1})
        self.assertEqual(quality["status"], "accepted_partial")

    def test_zero_ohlc_suspended_record_is_not_a_price_label(self):
        suspended = {**row("000635.SZ"), "open": 0, "high": 0, "low": 0, "close": 11,
                     "pre_close": 11, "vol": 0, "amount": 0}
        rows, quality, complete = _parse_payload(payload([row(), suspended]), "2026-11-16")
        self.assertTrue(complete)
        self.assertEqual(len(rows), 1)
        self.assertEqual(quality["rejected_count"], 0)
        self.assertEqual(quality["suspended_no_trade_excluded"], 1)
        self.assertEqual(quality["excluded_out_of_scope"], 0)
        self.assertEqual(quality["reason_counts"], {"suspended_no_trade_excluded": 1})

    def test_small_invalid_fraction_is_excluded_but_valid_rows_trainable(self):
        valid = [row("600%03d.SH" % number) for number in range(20)]
        invalid = {**row("600888.SH"), "open": 0}
        rows, quality, complete = _parse_payload(payload(valid + [invalid]), "2026-11-16")
        self.assertTrue(complete)
        self.assertEqual(quality["status"], "accepted_partial")
        self.assertEqual(quality["rejected_count"], 1)
        self.assertEqual(quality["exclusion_sample"], [{"reason": "invalid_number", "ts_code": "600888.SH", "field": "open"}])
        with tempfile.TemporaryDirectory() as temporary:
            _atomic_json(Path(temporary) / "daily/20261116.json.gz", {
                "trade_date": "2026-11-16", "complete": complete, "quality": quality, "rows": rows}, compressed=True)
            history = load_history(temporary)
            self.assertEqual(len(history), 20)
            self.assertNotIn("600888.SH", history)

    def test_date_mixing_remains_fatal_even_at_small_fraction(self):
        valid = [row("600%03d.SH" % number) for number in range(20)]
        _, quality, complete = _parse_payload(payload(valid + [row("600888.SH", day="20261113")]), "2026-11-16")
        self.assertFalse(complete)
        self.assertEqual(quality["reason_counts"]["wrong_trade_date"], 1)

    def test_provider_failure_never_echoes_secret_or_response(self):
        with self.assertRaises(_Failure) as failure:
            _parse_payload({"code": 403, "msg": SECRET + " no permission"}, "2026-11-16")
        self.assertEqual(str(failure.exception), "api_denied")
        self.assertNotIn(SECRET, str(vars(failure.exception)))
        self.assertEqual(failure.exception.retry_seconds, 86400)

    def test_transport_uses_whole_day_fixed_endpoint_and_budget(self):
        client = DailyClient(SECRET, max_requests=1, now=NOW)
        with patch.object(client.opener, "open", return_value=Response(payload([row()]))) as mocked:
            client.request("2026-11-16")
            request = mocked.call_args.args[0]
            sent = json.loads(request.data)
            self.assertEqual(request.full_url, "http://tus.sxdata.top/tu_share/")
            self.assertEqual(sent["params"], {"trade_date": "20261116", "limit": 6000})
            self.assertEqual(sent["api_name"], "daily")
            with self.assertRaises(_Failure) as failure:
                client.request("2026-11-13")
            self.assertEqual(str(failure.exception), "budget_exhausted")
            self.assertEqual(mocked.call_count, 1)

    def test_redirect_body_not_accepted(self):
        client = DailyClient(SECRET, now=NOW)
        with patch.object(client.opener, "open", return_value=Response(payload([row()]), "https://elsewhere.invalid/")):
            with self.assertRaises(_Failure) as failure:
                client.request("2026-11-16")
            self.assertEqual(str(failure.exception), "redirect_blocked")

    def test_resumes_missing_days_only_and_loads_sorted_bars(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict("os.environ", {"STOCK_API_TOKEN": SECRET}):
            def request(_client, day):
                _client.count += 1
                return _parse_payload(payload([row(day=day.replace("-", ""))]), day)
            with patch.object(DailyClient, "request", request):
                one = collect_equity_history(dashboard(), {"equity": {"history_days": 3}}, NOW, max_requests=1, output_dir=temporary)
                self.assertEqual(one["collected_days"], 1)
                two = collect_equity_history(dashboard(), {"equity": {"history_days": 3}}, NOW, max_requests=2, output_dir=temporary)
            self.assertEqual(two["cached_days"], 1)
            self.assertEqual(two["collected_days"], 2)
            self.assertEqual(two["status"], "ready")
            history = load_history(temporary)
            self.assertEqual([r["date"] for r in history["600000.SH"]], ["2026-11-12", "2026-11-13", "2026-11-16"])
            for path in Path(temporary).rglob("*"):
                if path.is_file():
                    content = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
                    self.assertNotIn(SECRET.encode(), content)

    def test_cached_complete_batch_has_no_network_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            rows, quality, _ = _parse_payload(payload([row()]), "2026-11-16")
            _atomic_json(Path(temporary) / "daily/20261116.json.gz", {"trade_date": "2026-11-16", "complete": True,
                "quality": quality, "rows": rows}, compressed=True)
            with patch.object(DailyClient, "request") as mocked:
                result = collect_equity_history(dashboard(), {"equity": {"history_days": 1}}, NOW, output_dir=temporary)
            self.assertEqual(result["status"], "ready")
            mocked.assert_not_called()

    def test_failure_checkpoint_applies_cooldown_without_retry_storm(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict("os.environ", {"STOCK_API_TOKEN": SECRET}):
            with patch.object(DailyClient, "request", side_effect=_Failure("rate_limited", 3600)) as mocked:
                first = collect_equity_history(dashboard(), {}, NOW, output_dir=temporary)
                second = collect_equity_history(dashboard(), {}, NOW, output_dir=temporary)
            self.assertEqual(first["status"], "unavailable")
            self.assertEqual(second["status"], "cooldown")
            self.assertEqual(mocked.call_count, 1)

    def test_unknown_business_error_is_not_labeled_permission_denied(self):
        with self.assertRaises(_Failure) as caught:
            _parse_payload({'code': -1, 'msg': 'temporary upstream error'}, '2026-11-16')
        self.assertEqual(caught.exception.code, 'provider_error')
        self.assertEqual(caught.exception.retry_seconds, 300)

    def test_one_transient_retry_in_entire_batch_then_stops(self):
        attempts = []
        def request(client, day):
            client.count += 1
            attempts.append(day)
            raise _Failure('provider_error', 300, provider_code=-1, provider_reason='provider_error')
        with tempfile.TemporaryDirectory() as temporary, patch.dict('os.environ', {'STOCK_API_TOKEN': SECRET}), \
             patch.object(DailyClient, 'request', request), patch('pipeline.equity_data.time.sleep') as sleep:
            result = collect_equity_history(dashboard(), {}, NOW, max_requests=10, output_dir=temporary)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        sleep.assert_called_once_with(60)
        self.assertEqual(result['provider_code'], -1)
        self.assertEqual(result['status'], 'unavailable')

    def test_shared_guard_releases_for_retry_wait_and_checkpoint(self):
        events = []
        @contextmanager
        def guard():
            events.append("lock")
            try:
                yield
            finally:
                events.append("unlock")

        def request(client, day):
            events.append("request")
            client.count += 1
            raise _Failure("transport_error", 60)

        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {"STOCK_API_TOKEN": SECRET}), \
             patch.object(DailyClient, "request", request), \
             patch("pipeline.equity_data.time.sleep", side_effect=lambda seconds: events.append(seconds)), \
             patch("pipeline.equity_data._atomic_json", side_effect=lambda *args: events.append("checkpoint")):
            collect_equity_history(dashboard(), {}, NOW, max_requests=2, output_dir=folder, request_guard=guard)
        self.assertEqual(events, ["lock", "request", "unlock", 60, "lock", "request", "unlock", "checkpoint"])

    def test_explicit_permission_error_never_retries(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict('os.environ', {'STOCK_API_TOKEN': SECRET}), \
             patch.object(DailyClient, 'request', side_effect=_Failure('api_denied', 86400, provider_code=403, provider_reason='authorization_denied')) as mocked, \
             patch('pipeline.equity_data.time.sleep') as sleep:
            result = collect_equity_history(dashboard(), {}, NOW, output_dir=temporary)
        mocked.assert_called_once()
        sleep.assert_not_called()
        self.assertEqual(result['provider_code'], 403)

    def test_incomplete_or_mismatched_file_never_loaded(self):
        with tempfile.TemporaryDirectory() as temporary:
            rows, _, _ = _parse_payload(payload([row()]), "2026-11-16")
            _atomic_json(Path(temporary) / "daily/20261116.json.gz", {"trade_date": "2026-11-16", "complete": False,
                "rows": rows}, compressed=True)
            _atomic_json(Path(temporary) / "daily/20261113.json.gz", {"trade_date": "2026-11-16", "complete": True,
                "rows": rows}, compressed=True)
            self.assertEqual(load_history(temporary), {})

    def test_no_credentials_or_offline_never_calls_network(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict("os.environ", {}, clear=True):
            with patch.object(DailyClient, "request") as mocked:
                result = collect_equity_history(dashboard(), {}, NOW, output_dir=temporary)
                offline = collect_equity_history(dashboard(), {"network_enabled": False}, NOW, output_dir=temporary)
            self.assertEqual(result["status"], "missing_credentials")
            self.assertEqual(offline["status"], "offline_cache")
            mocked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
