"""Credential, transport, date and unit contracts using synthetic HTTP only."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
import json
import os
import unittest
import urllib.error
from unittest.mock import Mock, patch

from pipeline.calendar import is_trading_day
from pipeline.stock_api import _Failure, _NoRedirect, enrich_with_stock_api


NOW = datetime.fromisoformat('2026-11-16T19:00:00+08:00')
SECRET = 'synthetic-test-secret-do-not-persist'
ENDPOINT = 'https://t.xiaodefa.top/'


class Response:
    status = 200
    headers = {}

    def __init__(self, value, url=ENDPOINT):
        self.body = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        return self.body[:size]

    def geturl(self):
        return self.url


def seed(indices=1, stocks=False):
    codes = ['sh000001', 'sz399001', 'sz399006', 'sh000300', 'sh000688']
    result = {
        'market': [{'id': code, 'name': 'Fixture Index', 'price': 2990,
                    'quote_date': '2026-11-13',
                    'as_of': '2026-11-13T15:00:00+08:00',
                    'provider': 'other', 'timestamp_quality': 'daily_close',
                    'history': [{'date': '2026-11-13', 'open': 2995, 'close': 3000,
                                 'source': 'other'}],
                    'prediction': {'target_date': '2026-11-17', 'issued_price': 2990}}
                   for code in codes[:indices]],
        'sectors': [], 'sources': [], 'price_observations': []}
    if stocks:
        result['sectors'] = [{'id': 'fixture_sector', 'stocks': [
            {'code': '600000', 'name': 'Fixture Stock', 'price': 12,
             'as_of': '2026-11-16T14:00:00+08:00', 'market_cap': None}]}]
    return result


def pack(fields, rows):
    return {'code': 0, 'msg': None,
            'data': {'fields': fields, 'items': [[row.get(field) for field in fields]
                                                for row in rows]}}


def calendar_rows():
    rows = []
    for delta in range(-7, 8):
        day = NOW.date() + timedelta(days=delta)
        previous = day - timedelta(days=1)
        while previous.weekday() >= 5:
            previous -= timedelta(days=1)
        rows.append({'exchange': 'SSE', 'cal_date': day.strftime('%Y%m%d'),
                     'is_open': int(day.weekday() < 5),
                     'pretrade_date': previous.strftime('%Y%m%d')})
    return rows


def index_row(ts_code='000001.SH', day='20261116', close=3100):
    return {'ts_code': ts_code, 'trade_date': day, 'close': close, 'open': close - 20,
            'high': close + 10, 'low': close - 30, 'pre_close': 3000, 'change': 100,
            'pct_chg': 3.3333, 'vol': 123.5, 'amount': 456.75}


def basic_row(day='20261116', ts_code='600000.SH'):
    return {'ts_code': ts_code, 'trade_date': day, 'close': 11.5,
            'total_mv': 123.4, 'circ_mv': 100, 'pe_ttm': 8.5, 'pb': 0.8,
            'pe': 7.5, 'turnover_rate': 1, 'volume_ratio': 1.1,
            'total_share': 12, 'float_share': 10, 'free_share': 8}


class Provider:
    def __init__(self, overrides=None):
        self.requests = []
        self.overrides = overrides or {}

    def __call__(self, request, **kwargs):
        payload = json.loads(request.data)
        self.requests.append(payload)
        api = payload['api_name']
        if api in self.overrides:
            value = self.overrides[api]
            if isinstance(value, Exception):
                raise value
            return Response(value(payload) if callable(value) else value)
        fields = payload['fields'].split(',')
        if api == 'trade_cal':
            return Response(pack(fields, calendar_rows()))
        if api == 'index_daily':
            code = payload['params']['ts_code']
            return Response(pack(fields, [index_row(code, '20261113', 3000), index_row(code)]))
        if api == 'daily_basic':
            return Response(pack(fields, [basic_row()]))
        raise AssertionError('Unexpected fixture API')


@contextmanager
def fake_provider(overrides=None):
    provider = Provider(overrides)
    opener = Mock()
    opener.open.side_effect = provider
    with patch.dict(os.environ, {'STOCK_API_TOKEN': SECRET}), \
         patch('pipeline.stock_api.urllib.request.build_opener', return_value=opener), \
         patch('pipeline.stock_api.time.sleep') as sleep:
        yield provider, opener, sleep


def source(result):
    return next(row for row in result['sources'] if row['id'] == 'stock_api')


class StockApiTests(unittest.TestCase):
    def assert_safe(self, result):
        self.assertNotIn(SECRET, json.dumps(result, ensure_ascii=False, allow_nan=False))

    def test_missing_token_and_offline_make_zero_requests_and_copy_input(self):
        data = seed(stocks=True)
        original = deepcopy(data)
        with patch.dict(os.environ, {}, clear=True), \
             patch('pipeline.stock_api.urllib.request.build_opener') as build:
            result = enrich_with_stock_api(data, {'stock_api': {'token': SECRET}}, now=NOW)
        build.assert_not_called()
        self.assertEqual(source(result)['status'], 'pending')
        self.assertEqual(source(result)['error_code'], 'not_configured')
        self.assertEqual(data, original)
        self.assertIsNot(result, data)
        with patch.dict(os.environ, {'STOCK_API_TOKEN': SECRET}), \
             patch('pipeline.stock_api.urllib.request.build_opener') as build:
            offline = enrich_with_stock_api(data, {'network_enabled': False}, NOW)
        build.assert_not_called()
        self.assertEqual(source(offline)['status'], 'pending')
        self.assert_safe(offline)

    def test_endpoint_cannot_redirect_token_to_another_destination(self):
        endpoints = ['http://t.xiaodefa.top/', 'https://example.test/',
                     'https://t.xiaodefa.top/elsewhere', 'https://t.xiaodefa.top/?token=leak',
                     'https://user@t.xiaodefa.top/', 'https://t.xiaodefa.top/#fragment',
                     'https://t.xiaodefa.top.evil.test/', 'https://[']
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint):
                with fake_provider() as (_, opener, _):
                    result = enrich_with_stock_api(seed(), {'stock_api': {'endpoint': endpoint}}, NOW)
                self.assertEqual(opener.open.call_count, 0)
                self.assertEqual(source(result)['error_code'], 'invalid_endpoint')
                self.assert_safe(result)

    def test_redirect_handler_never_reissues_credential_post(self):
        with self.assertRaises(_Failure) as caught:
            _NoRedirect().redirect_request(None, None, 307, SECRET, {}, 'https://example.test/')
        self.assertEqual(str(caught.exception), 'redirect_blocked')
        error = urllib.error.HTTPError(ENDPOINT, 307, SECRET,
                                      {'Location': 'https://example.test/'}, None)
        with fake_provider({'trade_cal': error}) as (_, opener, _):
            result = enrich_with_stock_api(seed(), now=NOW)
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(source(result)['error_code'], 'redirect_blocked')
        self.assert_safe(result)

    def test_post_contract_budget_rotation_and_no_daily_basic_without_stocks(self):
        with fake_provider() as (provider, opener, sleep):
            result = enrich_with_stock_api(seed(indices=5), {'stock_api': {'max_requests': 99}}, NOW)
        self.assertLessEqual(opener.open.call_count, 4)
        self.assertEqual(sum(row['api_name'] == 'index_daily' for row in provider.requests), 2)
        self.assertNotIn('daily_basic', [row['api_name'] for row in provider.requests])
        for call in opener.open.call_args_list:
            request = call.args[0]
            self.assertEqual(request.full_url, ENDPOINT)
            self.assertEqual(request.get_method(), 'POST')
        for payload in provider.requests:
            self.assertEqual(set(payload), {'api_name', 'token', 'params', 'fields'})
            self.assertEqual(payload['token'], SECRET)
        self.assertGreaterEqual(len(sleep.call_args_list), opener.open.call_count - 1)
        self.assertTrue(all(call.args[0] >= 2.9 for call in sleep.call_args_list))
        self.assert_safe(result)
        first_codes = {r['params']['ts_code'] for r in provider.requests if r['api_name'] == 'index_daily'}
        with fake_provider() as (provider2, _, _):
            again = enrich_with_stock_api(result, now=NOW + timedelta(minutes=5))
        second_codes = {r['params']['ts_code'] for r in provider2.requests if r['api_name'] == 'index_daily'}
        self.assertTrue(second_codes - first_codes)
        self.assert_safe(again)

    def test_429_cooldown_survives_next_run_without_retry(self):
        error = urllib.error.HTTPError(ENDPOINT, 429, SECRET, {'Retry-After': '120'}, None)
        with fake_provider({'trade_cal': error}) as (_, opener, _):
            result = enrich_with_stock_api(seed(stocks=True), now=NOW)
        self.assertEqual(opener.open.call_count, 1)
        self.assert_safe(result)
        with fake_provider() as (_, opener, _):
            again = enrich_with_stock_api(result, now=NOW + timedelta(seconds=30))
        self.assertEqual(opener.open.call_count, 0)
        self.assert_safe(again)

    def test_permission_http_errors_and_business_codes_are_safe(self):
        failures = [urllib.error.HTTPError(ENDPOINT, status, SECRET, {}, None)
                    for status in (401, 403)]
        failures += [{'code': code, 'msg': SECRET, 'data': None} for code in (2002, 2010, -2001)]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with fake_provider({'trade_cal': failure}) as (_, opener, _):
                    result = enrich_with_stock_api(seed(), now=NOW)
                self.assertEqual(opener.open.call_count, 1)
                self.assert_safe(result)

    def test_index_units_unknown_upstream_and_existing_prediction_preserved(self):
        data = seed()
        original = deepcopy(data)
        with fake_provider():
            result = enrich_with_stock_api(data, now=NOW)
        item = result['market'][0]
        self.assertEqual(item['price'], 3100)
        self.assertEqual(item['quote_date'], '2026-11-16')
        self.assertEqual(item['prediction'], original['market'][0]['prediction'])
        bars = item['source_history']['stock_api']
        self.assertEqual(bars[-1]['volume'], 123.5)
        self.assertEqual(bars[-1]['amount'], 456.75)
        self.assertEqual(bars[-1]['volume_unit'], 'lot')
        self.assertEqual(bars[-1]['amount_unit'], 'CNY_thousand')
        observations = [r for r in result['price_observations'] if r['provider'] == 'stock_api']
        self.assertTrue(observations)
        self.assertTrue(all(r['upstream_provider'] == 'unknown' for r in observations))
        self.assertEqual(data, original)
        self.assert_safe(result)

    def test_current_daily_bars_before_1830_and_future_bars_never_applied(self):
        def rows(payload):
            fields = payload['fields'].split(',')
            code = payload['params']['ts_code']
            return pack(fields, [index_row(code, '20261113', 3000),
                                 index_row(code, '20261116', 3100),
                                 index_row(code, '20261117', 3200)])
        before = NOW.replace(hour=18, minute=29)
        with fake_provider({'index_daily': rows}) as (provider, _, _):
            result = enrich_with_stock_api(seed(), now=before)
        index_request = next(row for row in provider.requests if row['api_name'] == 'index_daily')
        self.assertEqual(index_request['params']['end_date'], '20261113')
        self.assertNotEqual(result['market'][0]['price'], 3100)
        self.assertNotEqual(result['market'][0]['price'], 3200)
        self.assertFalse(any(bar['date'] > '2026-11-13'
                             for bar in result['market'][0].get('source_history', {}).get('stock_api', [])))

    def test_existing_same_day_quote_is_retained_while_vendor_observation_is_saved(self):
        data = seed()
        data['market'][0].update(price=3099, quote_date='2026-11-16',
                                 as_of='2026-11-16T15:00:00+08:00')
        with fake_provider():
            result = enrich_with_stock_api(data, now=NOW)
        self.assertEqual(result['market'][0]['price'], 3099)
        self.assertEqual(result['market'][0]['provider'], 'other')
        self.assertTrue(any(row['provider'] == 'stock_api' and row['price'] == 3100
                            for row in result['price_observations']))

    def test_partial_dates_preserve_newer_quote_and_remain_eligible_for_later_refresh(self):
        data = seed()
        data['market'][0].update(price=3099, quote_date='2026-11-16',
                                 as_of='2026-11-16T15:00:00+08:00')
        def old_rows(payload):
            return pack(payload['fields'].split(','),
                        [index_row(payload['params']['ts_code'], '20261113', 3000)])
        with fake_provider({'index_daily': old_rows}):
            result = enrich_with_stock_api(data, now=NOW)
        self.assertEqual(result['market'][0]['price'], 3099)
        self.assertEqual(result['market'][0]['source_history']['stock_api'][-1]['date'], '2026-11-13')
        attempt = result['stock_api_state']['index_requests']['sh000001']
        self.assertFalse(attempt.get('last_success'))
        self.assertEqual(attempt['latest_trade_date'], '2026-11-13')
        with fake_provider() as (provider, _, _):
            enrich_with_stock_api(result, now=NOW + timedelta(minutes=20))
        self.assertNotIn('index_daily', [row['api_name'] for row in provider.requests])
        with fake_provider() as (provider, _, _):
            refreshed = enrich_with_stock_api(result, now=NOW + timedelta(hours=7))
        self.assertIn('index_daily', [row['api_name'] for row in provider.requests])
        self.assertEqual(refreshed['market'][0]['price'], 3099)
        self.assertTrue(refreshed['stock_api_state']['index_requests']['sh000001'].get('last_success'))

    def test_vendor_calendar_conflict_is_observation_without_changing_official_cutoff(self):
        def conflicting_calendar(payload):
            rows = calendar_rows()
            for row in rows:
                if row['cal_date'] == '20261116':
                    row['is_open'] = 0
            return pack(payload['fields'].split(','), rows)
        self.assertTrue(is_trading_day(NOW.date()))
        with fake_provider({'trade_cal': conflicting_calendar}) as (provider, _, _):
            result = enrich_with_stock_api(seed(), now=NOW)
        self.assertTrue(is_trading_day(NOW.date()))
        index_request = next(row for row in provider.requests if row['api_name'] == 'index_daily')
        self.assertEqual(index_request['params']['end_date'], '20261116')
        calendar = result['stock_api_state']['calendar']
        self.assertTrue(calendar['official_rules_unchanged'])
        monday = next(row for row in calendar['records'] if row['cal_date'] == '2026-11-16')
        self.assertFalse(monday['matches_official_calendar'])

    def test_oversized_response_is_rejected_without_retaining_raw_body(self):
        with fake_provider({'trade_cal': b'x' * (4 * 1024 * 1024 + 1)}) as (_, opener, _):
            result = enrich_with_stock_api(seed(), now=NOW)
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(result['market'][0]['price'], 2990)
        self.assertLess(len(json.dumps(result)), 100000)
        self.assert_safe(result)

    def test_malformed_shapes_bad_dates_codes_and_nonfinite_values_do_not_apply(self):
        invalid = [
            {'code': 0, 'data': {'fields': ['close'], 'items': [{'close': 3}]}},
            {'code': 0, 'data': {'fields': ['close', 'close'], 'items': [[3, 3]]}},
            {'code': 0, 'data': {'fields': ['ts_code'], 'items': [['000001.SH', 'extra']]}},
            {'code': '0', 'msg': SECRET, 'data': {}},
        ]
        invalid += [lambda payload, row=row: pack(payload['fields'].split(','), [row])
                    for row in (index_row('600000.SH'), index_row(day='20260230'),
                                index_row(day='2026-11-16'), index_row(close=float('nan')),
                                index_row(close=float('inf')))]
        for value in invalid:
            with self.subTest(value=str(type(value))):
                with fake_provider({'index_daily': value}):
                    result = enrich_with_stock_api(seed(), now=NOW)
                self.assertEqual(result['market'][0]['price'], 2990)
                self.assert_safe(result)

    def test_timeout_preserves_existing_data_and_does_not_echo_error(self):
        data = seed()
        with fake_provider({'trade_cal': TimeoutError(SECRET)}) as (_, opener, _):
            result = enrich_with_stock_api(data, now=NOW)
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(result['market'][0]['price'], data['market'][0]['price'])
        self.assertEqual(result['market'][0]['history'], data['market'][0]['history'])
        self.assert_safe(result)

    def test_daily_basic_converts_cap_without_overwriting_price_timestamp_or_metrics(self):
        data = seed(indices=5, stocks=True)
        with fake_provider() as (provider, opener, _):
            result = enrich_with_stock_api(data, {'stock_api': {'max_requests': 999}}, now=NOW)
        self.assertEqual(opener.open.call_count, 4)
        calls = [r for r in provider.requests if r['api_name'] == 'daily_basic']
        self.assertEqual(len(calls), 1)
        self.assertNotIn('limit', calls[0]['params'])
        self.assertNotIn('offset', calls[0]['params'])
        stock = result['sectors'][0]['stocks'][0]
        vendor = stock['stock_api_daily_basic']
        self.assertAlmostEqual(vendor['market_cap_cny'], 1234000)
        self.assertEqual(vendor['close'], 11.5)
        self.assertEqual(vendor['vendor_pe_ttm'], 8.5)
        self.assertEqual(vendor['vendor_pb'], 0.8)
        self.assertEqual(stock['price'], 12)
        self.assertEqual(stock['as_of'], data['sectors'][0]['stocks'][0]['as_of'])
        self.assertNotIn('pe_ttm', stock)
        self.assertNotIn('pb', stock)
        with fake_provider() as (provider, _, _):
            enrich_with_stock_api(result, now=NOW + timedelta(minutes=1))
        self.assertNotIn('daily_basic', [r['api_name'] for r in provider.requests])
        self.assert_safe(result)

    def test_six_thousand_daily_basic_rows_are_marked_possibly_truncated(self):
        rows = [basic_row(ts_code=f'{prefix}{suffix:03}.SH')
                for prefix in ('600', '601', '603', '605', '688', '689')
                for suffix in range(1000)]
        override = lambda payload: pack(payload['fields'].split(','), rows)
        with fake_provider({'daily_basic': override}):
            result = enrich_with_stock_api(seed(stocks=True), now=NOW)
        cache = result['stock_api_state']['daily_basic']
        self.assertEqual(cache['row_count'], 6000)
        self.assertTrue(cache['possibly_truncated'])
        self.assert_safe(result)

    def test_nonpositive_vendor_pe_is_unavailable_without_affecting_price(self):
        for pe in (-5, 0, None):
            row = dict(basic_row(), pe_ttm=pe)
            override = lambda payload, r=row: pack(payload['fields'].split(','), [r])
            with fake_provider({'daily_basic': override}):
                result = enrich_with_stock_api(seed(stocks=True), now=NOW)
            stock = result['sectors'][0]['stocks'][0]
            self.assertIsNone(stock['stock_api_daily_basic']['vendor_pe_ttm'])
            self.assertEqual(stock['price'], 12)
            self.assertNotIn('pe_ttm', stock)
            self.assert_safe(result)

    def test_daily_basic_wrong_date_and_code_are_never_attached(self):
        for row in (basic_row('20261117'), basic_row('20261113'), basic_row(ts_code='600000.SZ')):
            with fake_provider({'daily_basic': lambda payload, r=row: pack(payload['fields'].split(','), [r])}):
                result = enrich_with_stock_api(seed(stocks=True), now=NOW)
            self.assertNotIn('stock_api_daily_basic', result['sectors'][0]['stocks'][0])
            self.assert_safe(result)


if __name__ == '__main__':
    unittest.main()
