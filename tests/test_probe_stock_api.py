"""Bounded capability probes against synthetic HTTP only."""
from contextlib import redirect_stdout
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from pipeline import probe_stock_api as probe
from pipeline import stock_api

NOW = datetime.fromisoformat('2026-11-16T19:00:00+08:00')
SECRET = 'fixture-credential-never-persist'


class Clock:
    def __init__(self):
        self.now = 100.0

    def sleep(self, seconds):
        self.now += seconds

    def monotonic(self):
        return self.now


class Response:
    status = 200
    headers = {}

    def __init__(self, payload):
        self.body = json.dumps(payload).encode('utf-8')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return stock_api.ENDPOINT

    def read(self, size):
        return self.body[:size]


class CapabilityProbeTests(unittest.TestCase):
    def exercise(self, group='audit_15000', override=None):
        clock = Clock()
        requests = []

        def transport(request, **kwargs):
            body = json.loads(request.data)
            requests.append((clock.now, body))
            special = override(body, len(requests)) if override else None
            if special is not None:
                if isinstance(special, Exception):
                    raise special
                return Response(special)
            fields = body['fields'].split(',')
            params = body['params']
            row = {key: 1.0 for key in fields}
            for key in ('ts_code', 'trade_date', 'nav_date', 'date'):
                if key in fields:
                    row[key] = str(params.get(key, 'fixture')).split(',')[0]
            if 'end_date' in fields:
                row['end_date'] = params.get('period', '20241231')
            if 'month' in fields:
                row['month'] = params.get('m')
            if 'quarter' in fields:
                row['quarter'] = params.get('q')
            if 'name' in fields:
                row['name'] = SECRET
            clock.now += 0.1
            return Response({'code': 0, 'data': {'fields': fields,
                             'items': [[row[key] for key in fields]]}})

        opener = Mock(open=transport)
        output = io.StringIO()
        original_fields = dict(stock_api.FIELDS)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capabilities.json'
            path.write_text(json.dumps({'core': {'preserved': True}}))
            with patch.dict('os.environ', {'STOCK_API_TOKEN': SECRET}), \
                 patch.object(probe, 'shanghai_now', return_value=NOW), \
                 patch.object(stock_api.urllib.request, 'build_opener', return_value=opener), \
                 patch.object(stock_api.time, 'monotonic', side_effect=clock.monotonic), \
                 patch.object(stock_api.time, 'sleep', side_effect=clock.sleep), \
                 redirect_stdout(output):
                result = probe.probe(group, path=path)
            saved_text = path.read_text()
            saved = json.loads(saved_text)
        self.assertEqual(stock_api.FIELDS, original_fields)
        self.assertNotIn(SECRET, saved_text)
        self.assertNotIn(SECRET, output.getvalue())
        return result, saved, requests, output.getvalue()

    def test_audit_twenty_sequential_small_requests_and_preserves_existing(self):
        result, saved, requests, _ = self.exercise()
        self.assertEqual(len(requests), 20)
        self.assertEqual(result['requests'], 20)
        self.assertEqual(result['groups_completed'], list(probe.AUDIT_GROUPS))
        self.assertEqual(saved['core'], {'preserved': True})
        self.assertEqual(saved['financials']['requests'], 7)
        self.assertEqual(saved['audit_15000']['requests'], 20)
        self.assertTrue(all(row['status'] == 'success' for row in result['results']))
        for earlier, later in zip(requests, requests[1:]):
            self.assertGreaterEqual(later[0] - earlier[0], 3.0 - 1e-9)
        for _, request in requests:
            self.assertEqual(request['params']['limit'], 3)
            self.assertEqual(request['token'], SECRET)
            if request['api_name'] in probe.NEWS_APIS:
                self.assertNotIn('content', request['fields'].split(','))
                self.assertNotIn('title', request['fields'].split(','))

    def test_permission_denial_does_not_abort_other_apis(self):
        def denied(body, count):
            if count == 1:
                return {'code': -2001, 'msg': '抱歉，您没有访问该接口的权限 ' + SECRET}
        result, _, requests, _ = self.exercise(override=denied)
        self.assertEqual(len(requests), 20)
        self.assertEqual(result['results'][0]['provider_code'], -2001)
        self.assertEqual(result['results'][0]['provider_reason'], 'permission_denied')
        self.assertIsNone(result['stopped_reason'])

    def test_business_rate_limit_stops_every_later_group(self):
        result, saved, requests, _ = self.exercise(override=lambda body, count:
            {'code': -2001, 'msg': '每分钟最多访问 1 次 ' + SECRET})
        self.assertEqual(len(requests), 1)
        self.assertEqual(result['stopped_reason'], 'rate_limited')
        self.assertNotIn('fund_macro', saved)

    def test_http_rate_limit_and_auth_stop_audit(self):
        for code in ('rate_limited', 'authorization_denied', 'budget_exhausted'):
            with self.subTest(code=code):
                result, _, requests, _ = self.exercise(override=lambda body, count:
                    stock_api._Failure(code))
                self.assertEqual(len(requests), 1)
                self.assertEqual(result['stopped_reason'], code)

    def test_empty_result_is_not_permission_failure(self):
        def empty(body, count):
            return {'code': 0, 'data': {'fields': body['fields'].split(','), 'items': []}}
        result, _, requests, _ = self.exercise('financials', empty)
        self.assertEqual(len(requests), 7)
        self.assertTrue(all(row['status'] == 'empty' for row in result['results']))
        self.assertTrue(all(row['query_match'] is None for row in result['results']))

    def test_wrong_period_or_security_is_flagged(self):
        self.assertFalse(probe._matches([{'ts_code': '000001.SZ', 'end_date': '20241231'}],
                                       {'ts_code': '600519.SH', 'period': '20241231'}))
        self.assertFalse(probe._matches([{'ts_code': '600519.SH', 'end_date': '20231231'}],
                                       {'ts_code': '600519.SH', 'period': '20241231'}))
        self.assertTrue(probe._matches([{'ts_code': '399001.SZ'}],
                                      {'ts_code': '000001.SH,399001.SZ'}))

    def test_provider_reason_is_bounded_and_never_contains_message(self):
        cases = [('参数 period 无效', 'invalid_parameters'),
                 ('积分不足', 'points_insufficient'),
                 ('未知接口', 'unsupported_api'),
                 ('invalid token', 'authorization_denied'),
                 ('unclassified private text', 'provider_error')]
        for message, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(stock_api._Failure) as caught:
                    stock_api._rows({'code': -9999, 'msg': message + ' ' + SECRET}, 'income')
                failure = caught.exception
                self.assertEqual(failure.code, 'api_denied')
                self.assertEqual(failure.provider_reason, expected)
                self.assertNotIn(message, str(failure))
                self.assertNotIn(SECRET, repr(failure.__dict__))
        failure = stock_api._Failure('api_denied', provider_code=10**20,
                                     provider_reason='arbitrary secret')
        self.assertIsNone(failure.provider_code)
        self.assertIsNone(failure.provider_reason)

    def test_news_sample_keeps_only_identity_and_times(self):
        for api in probe.NEWS_APIS:
            sample = probe._sample([{'title': 'private', 'content': 'private',
                                    'url': 'private', 'ann_date': '20250101',
                                    'ts_code': '600519.SH'}], api)
            self.assertEqual(sample, [{'ann_date': '20250101', 'ts_code': '600519.SH'}])

    def test_missing_token_does_not_access_network(self):
        with patch.dict('os.environ', {}, clear=True), \
             patch.object(stock_api.urllib.request, 'build_opener') as opener:
            with self.assertRaises(SystemExit):
                probe.probe('financials')
            opener.assert_not_called()


if __name__ == '__main__':
    unittest.main()
