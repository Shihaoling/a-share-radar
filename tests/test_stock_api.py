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
from pipeline.stock_api import _Failure, _NoRedirect, enrich_with_stock_api as collect_stock_api


NOW = datetime.fromisoformat('2026-11-16T19:00:00+08:00')
SECRET = 'synthetic-test-secret-do-not-persist'
ENDPOINT = 'https://t.xiaodefa.top/'
LIVE_NOW = NOW.replace(hour=14, minute=0)


def enrich_with_stock_api(data, config=None, now=None):
    """Exercise the supported supplement mode in the original safety contracts."""
    options = deepcopy(config or {})
    options.setdefault('stock_api', {}).setdefault('primary', False)
    return collect_stock_api(data, options, now)


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


def trading_dates(count=181):
    days = []
    day = NOW.date()
    while len(days) < count:
        if is_trading_day(day) is True:
            days.append(day.strftime('%Y%m%d'))
        day -= timedelta(days=1)
    return list(reversed(days))


def basic_row(day='20261116', ts_code='600000.SH'):
    return {'ts_code': ts_code, 'trade_date': day, 'close': 11.5,
            'total_mv': 123.4, 'circ_mv': 100, 'pe_ttm': 8.5, 'pb': 0.8,
            'pe': 7.5, 'turnover_rate': 1, 'volume_ratio': 1.1,
            'total_share': 12, 'float_share': 10, 'free_share': 8}


def primary_basic_rows():
    return [dict(basic_row(ts_code=f'{prefix}{number:03}.SH'), total_mv=100 + number)
            for prefix in ('600', '601', '603') for number in range(12)]


def member_rows(l1_code):
    prefix = {'801010.SI': '600', '801030.SI': '601', '801040.SI': '603'}.get(l1_code, '605')
    return [{'l1_code': l1_code, 'l1_name': 'Fixture Sector',
             'l2_code': '801011.SI', 'l2_name': 'Fixture L2',
             'l3_code': '850111.SI', 'l3_name': 'Fixture L3',
             'ts_code': f'{prefix}{number:03}.SH', 'name': f'Stock {number}',
             'in_date': '20200101', 'out_date': None, 'is_new': 'Y'}
            for number in range(12)]


class Provider:
    def __init__(self, overrides=None, primary=False, now=NOW):
        self.requests = []
        self.overrides = overrides or {}
        self.primary = primary
        self.now = now

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
            rows = [basic_row()]
            if self.primary:
                rows = primary_basic_rows()
            return Response(pack(fields, rows))
        if api == 'index_classify':
            return Response(pack(fields, [
                {'index_code': code, 'industry_name': name, 'parent_code': '0',
                 'level': 'L1', 'industry_code': industry, 'is_pub': '1', 'src': 'SW2021'}
                for code, name, industry in [('801010.SI', '农林牧渔', '110000'),
                                             ('801030.SI', '基础化工', '220000')]]))
        if api == 'sw_daily':
            params = payload['params']
            codes = [params['ts_code']] if params.get('ts_code') else ['801010.SI', '801030.SI']
            days = [params['trade_date']] if params.get('trade_date') else ['20261113', '20261116']
            rows = [{'ts_code': code, 'trade_date': day, 'name': 'Fixture SW Index',
                     'open': 1000, 'low': 995, 'high': 1020, 'close': 1010,
                     'change': 10, 'pct_change': 1, 'vol': 123, 'amount': 456,
                     'pe': 18, 'pb': 2, 'float_mv': 3000, 'total_mv': 5000}
                    for code in codes for day in days]
            return Response(pack(fields, rows))
        if api == 'index_member_all':
            return Response(pack(fields, member_rows(payload['params']['l1_code'])))
        if api in ('rt_idx_k', 'rt_sw_k'):
            codes = ['000001.SH'] if api == 'rt_idx_k' else ['801010.SI', '801030.SI']
            trade_time = min(self.now, self.now.replace(hour=15, minute=0, second=0))
            rows = [{'ts_code': code, 'name': 'Fixture live index',
                     'trade_time': trade_time.strftime('%Y-%m-%d %H:%M:%S'),
                     'close': 3111 if api == 'rt_idx_k' else 1011,
                     'pre_close': 3000 if api == 'rt_idx_k' else 1000,
                     'open': 3005 if api == 'rt_idx_k' else 1005,
                     'high': 3120 if api == 'rt_idx_k' else 1020,
                     'low': 3000 if api == 'rt_idx_k' else 1000,
                     'vol': 12345, 'amount': 67890} for code in codes]
            return Response(pack(fields, rows))
        if api == 'news':
            return Response(pack(fields, [{'datetime': '2026-11-16 18:45:00',
                                          'content': 'Fixture financial news content',
                                          'title': 'Fixture financial headline',
                                          'channels': 'A股'}]))
        raise AssertionError('Unexpected fixture API')


@contextmanager
def fake_provider(overrides=None, primary=False, now=NOW, capability_seed=None):
    provider = Provider(overrides, primary=primary, now=now)
    opener = Mock()
    opener.open.side_effect = provider
    with patch.dict(os.environ, {'STOCK_API_TOKEN': SECRET}), \
         patch('pipeline.stock_api.urllib.request.build_opener', return_value=opener), \
         patch('pipeline.stock_api._capability_seed', return_value=capability_seed or {}), \
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


def canonical_seed():
    result = seed()
    result['sectors'] = [
        {'id': code, 'code': code, 'index_code': code, 'name': name,
         'taxonomy': 'SW2021', 'basis': 'index', 'kind': 'industry',
         'history': [], 'stocks': [], 'constituents': [], 'price': None}
        for code, name in [('801010.SI', '农林牧渔'), ('801030.SI', '基础化工')]]
    return result


class PrimaryStockApiTests(unittest.TestCase):
    def assert_safe(self, result):
        self.assertNotIn(SECRET, json.dumps(result, ensure_ascii=False, allow_nan=False))

    def test_primary_missing_token_and_offline_never_initialize_transport(self):
        data = canonical_seed()
        original = deepcopy(data)
        with patch.dict(os.environ, {}, clear=True), \
             patch('pipeline.stock_api.urllib.request.build_opener') as build:
            missing = collect_stock_api(data, now=NOW)
        build.assert_not_called()
        self.assertEqual(source(missing)['error_code'], 'not_configured')
        with patch.dict(os.environ, {'STOCK_API_TOKEN': SECRET}), \
             patch('pipeline.stock_api.urllib.request.build_opener') as build:
            offline = collect_stock_api(data, {'network_enabled': False}, NOW)
        build.assert_not_called()
        self.assertEqual(source(offline)['error_code'], 'offline')
        self.assertEqual(data, original)
        self.assert_safe(missing)
        self.assert_safe(offline)

    def test_primary_uses_canonical_sw_ids_and_archives_previous_sectors(self):
        data = seed(stocks=True)
        original = deepcopy(data)
        with fake_provider(primary=True) as (provider, opener, _):
            result = collect_stock_api(data, {'stock_api': {'max_requests': 999}}, now=NOW)
        self.assertLessEqual(opener.open.call_count, 12)
        self.assertIn('index_classify', [row['api_name'] for row in provider.requests])
        self.assertEqual({row['id'] for row in result['sectors']}, {'801010.SI', '801030.SI'})
        self.assertTrue(all(row['id'] == row['code'] == row['index_code']
                            for row in result['sectors']))
        self.assertTrue(all(row['taxonomy'] == 'SW2021' and row['basis'] == 'index'
                            for row in result['sectors']))
        classification = result['stock_api_state']['classification']
        self.assertEqual(classification['expected_count'], 31)
        self.assertEqual(classification['received_count'], 2)
        self.assertFalse(classification['complete'])
        self.assertIn('fixture_sector', [row['id'] for row in result['archived_sectors']])
        self.assertEqual(data, original)
        self.assert_safe(result)

    def test_industry_keywords_join_official_codes_without_replacing_api_names(self):
        rows = [{'index_code': code, 'industry_name': name, 'parent_code': '0', 'level': 'L1',
                 'industry_code': industry, 'is_pub': '1', 'src': 'SW2021'}
                for code, name, industry in [('801010.SI', '供应商官方行业甲', '110000'),
                                             ('801030.SI', '供应商官方行业乙', '220000'),
                                             ('809999.SI', '农林牧渔', '990000')]]
        override = lambda payload: pack(payload['fields'].split(','), rows)
        options = {'stock_api': {'max_member_calls': 0, 'max_sw_history_calls': 0,
                                 'news_enabled': False}}
        with fake_provider({'index_classify': override}, primary=True):
            result = collect_stock_api(canonical_seed(), options, NOW)
        sectors = {row['id']: row for row in result['sectors']}
        self.assertEqual(sectors['801010.SI']['name'], '供应商官方行业甲')
        self.assertEqual(sectors['801030.SI']['name'], '供应商官方行业乙')
        self.assertIn('生猪', sectors['801010.SI']['keywords'])
        self.assertIn('化肥', sectors['801030.SI']['keywords'])
        self.assertEqual(sectors['809999.SI']['name'], '农林牧渔')
        self.assertEqual(sectors['809999.SI']['keywords'], ['农林牧渔'])
        self.assert_safe(result)

    def test_partial_classification_rechecks_after_fifteen_minutes_and_merges_codes(self):
        options = {'stock_api': {'max_index_calls': 0, 'max_member_calls': 0,
                                 'max_sw_history_calls': 0, 'news_enabled': False}}
        with fake_provider(primary=True):
            first = collect_stock_api(canonical_seed(), options, NOW)
        self.assertFalse(first['stock_api_state']['classification']['complete'])
        with fake_provider(primary=True) as (provider, _, _):
            cached = collect_stock_api(first, options, NOW + timedelta(minutes=5))
        self.assertNotIn('index_classify', [request['api_name'] for request in provider.requests])
        self.assertEqual({sector['id'] for sector in cached['sectors']}, {'801010.SI', '801030.SI'})
        # A legacy complete flag alone is insufficient when only two records exist.
        cached['stock_api_state']['classification']['complete'] = True
        new_row = {'index_code': '801040.SI', 'industry_name': '钢铁', 'parent_code': '0',
                   'level': 'L1', 'industry_code': '230000', 'is_pub': '1', 'src': 'SW2021'}
        override = lambda payload: pack(payload['fields'].split(','), [new_row])
        with fake_provider({'index_classify': override}, primary=True) as (provider, opener, _):
            refreshed = collect_stock_api(cached, options, NOW + timedelta(minutes=16))
        self.assertIn('index_classify', [request['api_name'] for request in provider.requests])
        self.assertEqual({sector['id'] for sector in refreshed['sectors']},
                         {'801010.SI', '801030.SI', '801040.SI'})
        classification = refreshed['stock_api_state']['classification']
        self.assertEqual(len(classification['records']), 3)
        self.assertFalse(classification['complete'])
        self.assertEqual(classification['fetched_at'], (NOW + timedelta(minutes=16)).isoformat(timespec='seconds'))
        self.assertLessEqual(opener.open.call_count, 12)
        self.assert_safe(refreshed)

    def test_quotes_only_requests_two_realtime_apis_and_reuses_sixty_second_cache(self):
        with fake_provider(primary=True, now=LIVE_NOW) as (provider, opener, _):
            result = collect_stock_api(canonical_seed(), {'stock_api': {'quotes_only': True}}, LIVE_NOW)
        self.assertEqual({row['api_name'] for row in provider.requests}, {'rt_idx_k', 'rt_sw_k'})
        self.assertEqual(opener.open.call_count, 2)
        index_quote = result['market'][0]['live_quote']
        sector_quote = result['sectors'][0]['live_quote']
        self.assertEqual(index_quote['volume_unit'], 'unknown')
        self.assertEqual(sector_quote['volume_unit'], 'share')
        self.assertEqual(sector_quote['amount_unit'], 'CNY')
        self.assertEqual(sector_quote['amount'], 67890)
        self.assertEqual(sector_quote['upstream_provider'], 'unknown')
        self.assert_safe(result)
        with fake_provider(primary=True, now=LIVE_NOW + timedelta(seconds=30)) as (_, opener, _):
            cached = collect_stock_api(result, {'stock_api': {'quotes_only': True}}, LIVE_NOW + timedelta(seconds=30))
        self.assertEqual(opener.open.call_count, 0)
        with fake_provider(primary=True, now=LIVE_NOW + timedelta(seconds=61)) as (provider, opener, _):
            refreshed = collect_stock_api(cached, {'stock_api': {'quotes_only': True}}, LIVE_NOW + timedelta(seconds=61))
        self.assertEqual({row['api_name'] for row in provider.requests}, {'rt_idx_k', 'rt_sw_k'})
        self.assertEqual(opener.open.call_count, 2)
        self.assert_safe(refreshed)

    def test_api_permission_denial_only_cools_the_denied_endpoint(self):
        denied = {'code': 2002, 'msg': SECRET, 'data': None}
        with fake_provider({'index_classify': denied}, primary=True) as (provider, _, _):
            result = collect_stock_api(seed(), now=NOW)
        requested = [row['api_name'] for row in provider.requests]
        self.assertIn('index_classify', requested)
        self.assertIn('index_daily', requested)
        self.assertTrue(result['stock_api_state']['endpoints']['index_classify']['cooldown_until'])
        self.assertFalse(result['stock_api_state'].get('cooldown_until'))
        self.assert_safe(result)
        with fake_provider(primary=True) as (provider, _, _):
            again = collect_stock_api(result, now=NOW + timedelta(minutes=5))
        self.assertNotIn('index_classify', [row['api_name'] for row in provider.requests])
        self.assert_safe(again)

    def test_failed_calendar_check_is_not_repeated_within_twenty_four_hours(self):
        malformed = {'code': 0, 'data': {'fields': [], 'items': []}}
        with fake_provider({'trade_cal': malformed}, primary=True):
            result = collect_stock_api(canonical_seed(), now=NOW)
        with fake_provider(primary=True) as (provider, _, _):
            again = collect_stock_api(result, now=NOW + timedelta(minutes=20))
        self.assertNotIn('trade_cal', [request['api_name'] for request in provider.requests])
        self.assert_safe(again)

    def test_recent_permission_probe_skips_unavailable_live_and_news_apis(self):
        probe = {'live_news': {'checked_at': (NOW - timedelta(hours=1)).isoformat(),
                              'results': [{'api': api, 'error_code': 'api_denied'}
                                          for api in ('rt_idx_k', 'rt_sw_k', 'news')]}}
        with fake_provider(primary=True, capability_seed=probe) as (provider, _, _):
            result = collect_stock_api(canonical_seed(), now=NOW)
        requested = {request['api_name'] for request in provider.requests}
        self.assertTrue(requested.isdisjoint({'rt_idx_k', 'rt_sw_k', 'news'}))
        self.assertIn('sw_daily', requested)
        for api in ('rt_idx_k', 'rt_sw_k', 'news'):
            endpoint = result['stock_api_state']['endpoints'][api]
            self.assertEqual(endpoint['error_code'], 'api_denied')
            self.assertTrue(endpoint['capability_probe'])
        self.assert_safe(result)

    def test_old_permission_probe_cannot_override_a_newer_endpoint_success(self):
        probe = {'live_news': {'checked_at': (NOW - timedelta(hours=1)).isoformat(),
                              'results': [{'api': 'rt_idx_k', 'error_code': 'api_denied'}]}}
        data = canonical_seed()
        data['stock_api_state'] = {'endpoints': {'rt_idx_k': {
            'last_success': (NOW - timedelta(minutes=30)).isoformat(),
            'status': 'success', 'cooldown_until': None}}}
        with fake_provider(primary=True, capability_seed=probe) as (provider, _, _):
            result = collect_stock_api(data, {'stock_api': {'quotes_only': True}}, NOW)
        self.assertIn('rt_idx_k', [request['api_name'] for request in provider.requests])
        self.assertEqual(result['stock_api_state']['endpoints']['rt_idx_k']['status'], 'success')
        self.assert_safe(result)

    def test_http_429_still_cools_the_whole_service_in_primary_mode(self):
        error = urllib.error.HTTPError(ENDPOINT, 429, SECRET, {'Retry-After': '120'}, None)
        with fake_provider({'rt_idx_k': error}, primary=True, now=LIVE_NOW) as (_, opener, _):
            result = collect_stock_api(canonical_seed(), {'stock_api': {'quotes_only': True}}, LIVE_NOW)
        self.assertEqual(opener.open.call_count, 1)
        self.assertTrue(result['stock_api_state'].get('cooldown_until'))
        with fake_provider(primary=True, now=LIVE_NOW) as (_, opener, _):
            again = collect_stock_api(result, {'stock_api': {'quotes_only': True}}, LIVE_NOW + timedelta(seconds=61))
        self.assertEqual(opener.open.call_count, 0)
        self.assert_safe(again)

    def test_realtime_timestamp_must_be_complete_same_day_and_not_future(self):
        invalid = ['14:00:00', '2026-11-17 14:00:00', '2026-11-16 14:01:00',
                   '2026-11-13 14:00:00', 'not-a-date']
        for stamp in invalid:
            row = {'ts_code': '000001.SH', 'name': 'Fixture Index', 'trade_time': stamp,
                   'close': 3111, 'pre_close': 3000, 'open': 3005,
                   'high': 3120, 'low': 3000, 'vol': 12345, 'amount': 67890}
            override = lambda payload, value=row: pack(payload['fields'].split(','), [value])
            with self.subTest(stamp=stamp):
                with fake_provider({'rt_idx_k': override}, primary=True, now=LIVE_NOW) as (provider, _, _):
                    result = collect_stock_api(canonical_seed(), {'stock_api': {'quotes_only': True}}, LIVE_NOW)
                self.assertEqual(result['market'][0]['price'], 2990)
                self.assertNotIn('live_quote', result['market'][0])
                self.assertIn('rt_sw_k', [request['api_name'] for request in provider.requests])
                self.assert_safe(result)

    def test_confirmed_daily_close_is_primary_after_close_while_live_observation_is_retained(self):
        denied = {'code': 2002, 'msg': SECRET, 'data': None}
        for live in (False, True):
            data = canonical_seed()
            item = data['market'][0]
            stamp = '2026-11-16T15:00:02+08:00' if live else '2026-11-16T15:00:00+08:00'
            item.update(price=3099, quote_date='2026-11-16', as_of=stamp,
                        timestamp_quality='source_reported' if live else 'daily_close')
            item['history'].append({'date': '2026-11-16', 'close': 3099, 'source': 'other'})
            if live:
                item['live_quote'] = {'price': 3099, 'quote_timestamp': stamp,
                                      'timestamp_quality': 'source_reported', 'provider': 'stock_api'}
            with fake_provider({'rt_idx_k': denied, 'rt_sw_k': denied}, primary=True):
                result = collect_stock_api(data, now=NOW)
            self.assertEqual(result['market'][0]['price'], 3100)
            self.assertEqual(result['market'][0]['timestamp_quality'], 'daily_close')
            self.assertEqual(result['market'][0]['history'][-1]['close'], 3100)
            if live:
                self.assertEqual(result['market'][0]['live_quote']['price'], 3099)
                self.assertEqual(result['market'][0]['live_quote']['quote_timestamp'], stamp)
            self.assertEqual(result['market'][0]['prediction'], item['prediction'])
            self.assert_safe(result)

    def test_sw_history_has_explicit_units_and_market_cap_converts_to_yuan(self):
        with fake_provider(primary=True) as (provider, _, _):
            result = collect_stock_api(canonical_seed(), now=NOW)
        histories = [request for request in provider.requests
                     if request['api_name'] == 'sw_daily' and 'ts_code' in request['params']]
        self.assertLessEqual(len(histories), 2)
        self.assertTrue(histories)
        self.assertEqual(histories[0]['params']['start_date'], '20251116')
        for sector in result['sectors']:
            bar = sector['history'][-1]
            self.assertEqual(bar['date'], '2026-11-16')
            self.assertEqual(bar['volume'], 123)
            self.assertEqual(bar['volume_unit'], '万股')
            self.assertEqual(bar['amount'], 456)
            self.assertEqual(bar['amount_unit'], 'CNY_10000')
            self.assertEqual(bar['amount_cny'], 4560000)
            self.assertEqual(bar['market_cap_cny'], 50000000)
            self.assertEqual(sector['market_cap'], 50000000)
            self.assertEqual(sector['market_cap_unit'], 'CNY')
            self.assertEqual(bar['upstream_provider'], 'unknown')
        self.assert_safe(result)

    def test_short_index_cache_overrides_old_complete_marker_then_becomes_complete(self):
        data = canonical_seed()
        short_history = [{'date': day, 'close': 3000, 'source': 'stock_api', 'complete': True}
                         for day in ('2026-11-13', '2026-11-16')]
        data['market'][0]['source_history'] = {'stock_api': deepcopy(short_history)}
        data['stock_api_state'] = {'index_requests': {'sh000001': {
            'target_date': '2026-11-16', 'complete': True,
            'last_success': (NOW - timedelta(hours=1)).isoformat()}}}
        days = trading_dates()
        def full_index_history(payload):
            rows = [index_row(payload['params']['ts_code'], day, 3000 + position)
                    for position, day in enumerate(days)]
            return pack(payload['fields'].split(','), rows)
        options = {'stock_api': {'max_requests': 999, 'max_member_calls': 0,
                                 'max_sw_history_calls': 0, 'news_enabled': False}}
        with fake_provider({'index_daily': full_index_history}, primary=True) as (provider, opener, _):
            result = collect_stock_api(data, options, NOW)
        requests = [request for request in provider.requests if request['api_name'] == 'index_daily']
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]['params']['start_date'], '20251116')
        self.assertLessEqual(opener.open.call_count, 12)
        self.assertEqual(len(result['market'][0]['source_history']['stock_api']), 181)
        self.assertTrue(result['stock_api_state']['index_requests']['sh000001']['complete'])
        with fake_provider(primary=True) as (provider, opener, _):
            again = collect_stock_api(result, options, NOW + timedelta(minutes=5))
        self.assertNotIn('index_daily', [request['api_name'] for request in provider.requests])
        self.assertLessEqual(opener.open.call_count, 12)
        self.assert_safe(again)

    def test_short_sw_cache_overrides_old_complete_marker_then_becomes_complete(self):
        data = canonical_seed()
        short_history = [{'date': day, 'close': 1010, 'source': 'stock_api',
                          'api': 'sw_daily', 'complete': True}
                         for day in ('2026-11-13', '2026-11-16')]
        requests = {}
        for sector in data['sectors']:
            sector['source_history'] = {'stock_api': deepcopy(short_history)}
            requests[sector['id']] = {'target_date': '2026-11-16', 'complete': True,
                                      'last_success': (NOW - timedelta(hours=1)).isoformat()}
        data['stock_api_state'] = {'sw_history_requests': requests}
        days = trading_dates()
        def full_sw_history(payload):
            params = payload['params']
            codes = [params['ts_code']] if params.get('ts_code') else ['801010.SI', '801030.SI']
            selected = days if params.get('ts_code') else [params['trade_date']]
            rows = [{'ts_code': code, 'trade_date': day, 'name': 'Fixture SW Index',
                     'open': 1000, 'low': 995, 'high': 1020, 'close': 1010, 'change': 10,
                     'pct_change': 1, 'vol': 123, 'amount': 456, 'pe': 18, 'pb': 2,
                     'float_mv': 3000, 'total_mv': 5000} for code in codes for day in selected]
            return pack(payload['fields'].split(','), rows)
        options = {'stock_api': {'max_requests': 999, 'max_index_calls': 0,
                                 'max_member_calls': 0, 'news_enabled': False}}
        with fake_provider({'sw_daily': full_sw_history}, primary=True) as (provider, opener, _):
            result = collect_stock_api(data, options, NOW)
        histories = [request for request in provider.requests if request['api_name'] == 'sw_daily'
                     and 'ts_code' in request['params']]
        self.assertEqual(len(histories), 2)
        self.assertTrue(all(request['params']['start_date'] == '20251116' for request in histories))
        self.assertLessEqual(opener.open.call_count, 12)
        for sector in result['sectors']:
            self.assertEqual(len(sector['source_history']['stock_api']), 181)
            self.assertTrue(result['stock_api_state']['sw_history_requests'][sector['id']]['complete'])
        with fake_provider(primary=True) as (provider, opener, _):
            again = collect_stock_api(result, options, NOW + timedelta(minutes=5))
        self.assertFalse(any(request['api_name'] == 'sw_daily' and 'ts_code' in request['params']
                             for request in provider.requests))
        self.assertLessEqual(opener.open.call_count, 12)
        self.assert_safe(again)

    def test_exactly_one_hundred_eighty_cached_bars_still_require_history_backfill(self):
        data = canonical_seed()
        dates = [datetime.strptime(day, '%Y%m%d').date().isoformat() for day in trading_dates(180)]
        bars = [{'date': day, 'close': 3000, 'source': 'stock_api', 'complete': True} for day in dates]
        data['market'][0]['source_history'] = {'stock_api': deepcopy(bars)}
        state = {'index_requests': {'sh000001': {'target_date': '2026-11-16', 'complete': True}},
                 'sw_history_requests': {}}
        for sector in data['sectors']:
            sector['source_history'] = {'stock_api': [dict(bar, api='sw_daily') for bar in bars]}
            state['sw_history_requests'][sector['id']] = {'target_date': '2026-11-16', 'complete': True}
        data['stock_api_state'] = state
        options = {'stock_api': {'max_member_calls': 0, 'news_enabled': False}}
        with fake_provider(primary=True) as (provider, opener, _):
            result = collect_stock_api(data, options, NOW)
        index_requests = [request for request in provider.requests if request['api_name'] == 'index_daily']
        sw_requests = [request for request in provider.requests if request['api_name'] == 'sw_daily'
                       and 'ts_code' in request['params']]
        self.assertEqual(len(index_requests), 1)
        self.assertEqual(len(sw_requests), 2)
        self.assertTrue(all(request['params']['start_date'] == '20251116'
                            for request in index_requests + sw_requests))
        self.assertFalse(result['stock_api_state']['index_requests']['sh000001']['complete'])
        for sector in result['sectors']:
            self.assertEqual(len(sector['source_history']['stock_api']), 180)
            self.assertFalse(result['stock_api_state']['sw_history_requests'][sector['id']]['complete'])
        self.assertLessEqual(opener.open.call_count, 12)
        self.assert_safe(result)

    def test_backfilled_ohlc_keeps_trade_time_while_valuation_availability_stays_actual(self):
        with fake_provider(primary=True):
            result = collect_stock_api(canonical_seed(), now=NOW)
        stamp = NOW.isoformat(timespec='seconds')
        for sector in result['sectors']:
            past = next(bar for bar in sector['history'] if bar['date'] == '2026-11-13')
            self.assertEqual(past['as_of'], '2026-11-13T15:00:00+08:00')
            self.assertEqual(past['fetched_at'], stamp)
            self.assertNotIn('available_at', past)
            self.assertEqual(past['valuation_available_at'], stamp)
            feature = next(row for row in sector['feature_history'] if row['date'] == '2026-11-13')
            self.assertEqual(feature['available_at'], stamp)
            self.assertGreater(datetime.fromisoformat(feature['available_at']),
                               datetime.fromisoformat(past['as_of']))

        def revised_history(payload):
            code = payload['params']['ts_code']
            rows = [{'ts_code': code, 'trade_date': day, 'name': 'Fixture SW Index',
                     'open': 1000, 'low': 995, 'high': 1020, 'close': 1010, 'change': 10,
                     'pct_change': 1, 'vol': 123, 'amount': 456, 'pe': 21 if day == '20261113' else 18,
                     'pb': 2, 'float_mv': 3000, 'total_mv': 5000}
                    for day in ('20261113', '20261116')]
            return pack(payload['fields'].split(','), rows)

        for status in result['stock_api_state']['sw_history_requests'].values():
            status['complete'] = False
        later = NOW + timedelta(hours=1)
        with fake_provider({'sw_daily': revised_history}, primary=True):
            refreshed = collect_stock_api(result, now=later)
        for sector in refreshed['sectors']:
            features = {row['date']: row for row in sector['feature_history']}
            self.assertEqual(features['2026-11-13']['vendor_pe'], 21)
            self.assertEqual(features['2026-11-13']['available_at'], later.isoformat(timespec='seconds'))
            self.assertEqual(features['2026-11-16']['available_at'], stamp)
            past = next(bar for bar in sector['history'] if bar['date'] == '2026-11-13')
            self.assertNotIn('available_at', past)
            self.assertEqual(past['valuation_available_at'], later.isoformat(timespec='seconds'))
        self.assert_safe(refreshed)

    def test_legacy_valuation_marker_migrates_without_removing_explicit_price_constraints(self):
        data = canonical_seed()
        stamp = (NOW - timedelta(hours=1)).isoformat(timespec='seconds')
        classification = []
        for position, sector in enumerate(data['sectors']):
            bar = {'date': '2026-11-13', 'open': 1000, 'close': 1010, 'api': 'sw_daily',
                   'source': 'stock_api', 'as_of': '2026-11-13T15:00:00+08:00',
                   'fetched_at': stamp, 'available_at': stamp}
            if position:
                bar['price_available_at'] = '2026-11-13T15:00:00+08:00'
            sector['history'] = [deepcopy(bar)]
            sector['source_history'] = {'stock_api': [deepcopy(bar)]}
            sector['feature_history'] = [{'date': '2026-11-13', 'vendor_pe': 18,
                                          'available_at': stamp, 'fetched_at': stamp}]
            classification.append({'index_code': sector['id'], 'industry_name': sector['name'],
                                   'parent_code': '0', 'level': 'L1', 'industry_code': '110000',
                                   'is_pub': 1, 'src': 'SW2021'})
        data['stock_api_state'] = {'classification': {'records': classification,
                                   'fetched_at': stamp, 'complete': False,
                                   'expected_count': 31, 'received_count': 2}}
        original = deepcopy(data)
        with fake_provider(primary=True) as (_, opener, _):
            result = collect_stock_api(data, {'stock_api': {'max_requests': 0}}, NOW)
        self.assertEqual(opener.open.call_count, 0)
        migrated, constrained = result['sectors']
        for bars in (migrated['history'], migrated['source_history']['stock_api'],
                     result['history'][migrated['id']]):
            self.assertNotIn('available_at', bars[0])
            self.assertEqual(bars[0]['valuation_available_at'], stamp)
            self.assertEqual(bars[0]['as_of'], '2026-11-13T15:00:00+08:00')
        for bars in (constrained['history'], constrained['source_history']['stock_api']):
            self.assertEqual(bars[0]['available_at'], stamp)
            self.assertEqual(bars[0]['price_available_at'], '2026-11-13T15:00:00+08:00')
        self.assertEqual(migrated['feature_history'][0]['available_at'], stamp)
        self.assertEqual(constrained['feature_history'][0]['available_at'], stamp)
        self.assertEqual(data, original)
        self.assert_safe(result)

    def test_numeric_conversions_cannot_introduce_nonfinite_derived_values(self):
        sw_row = {'ts_code': '801010.SI', 'trade_date': '20261116', 'name': 'Fixture',
                  'open': 1000, 'low': 995, 'high': 1020, 'close': 1010, 'change': 10,
                  'pct_change': 1, 'vol': 123, 'amount': 1e308, 'pe': 18, 'pb': 2,
                  'float_mv': 3000, 'total_mv': 5000}
        override = lambda payload: pack(payload['fields'].split(','), [sw_row])
        with fake_provider({'sw_daily': override}, primary=True):
            result = collect_stock_api(canonical_seed(), now=NOW)
        self.assert_safe(result)
        self.assertFalse(result['sectors'][0]['history'])
        rt_row = {'ts_code': '000001.SH', 'name': 'Fixture', 'trade_time': '2026-11-16 14:00:00',
                  'close': 1e308, 'pre_close': 1e-308, 'open': 1e308, 'high': 1e308,
                  'low': 1e308, 'vol': 1, 'amount': 1}
        override = lambda payload: pack(payload['fields'].split(','), [rt_row])
        with fake_provider({'rt_idx_k': override}, primary=True, now=LIVE_NOW):
            result = collect_stock_api(canonical_seed(), {'stock_api': {'quotes_only': True}}, LIVE_NOW)
        self.assert_safe(result)
        self.assertEqual(result['market'][0]['price'], 2990)

    def test_membership_preserves_all_constituents_and_ranks_top_ten_by_same_day_cap(self):
        with fake_provider(primary=True) as (provider, _, _):
            result = collect_stock_api(canonical_seed(), now=NOW)
        member_calls = [request for request in provider.requests if request['api_name'] == 'index_member_all']
        self.assertEqual(len(member_calls), 2)
        self.assertTrue(all(request['params']['is_new'] == 'Y' for request in member_calls))
        for sector in result['sectors']:
            self.assertEqual(len(sector['constituents']), 12)
            self.assertEqual(len(sector['stocks']), 10)
            self.assertEqual(sector['rank_basis'], 'total_market_cap')
            self.assertTrue(sector['membership_complete'])
            caps = [stock['market_cap'] for stock in sector['stocks']]
            self.assertEqual(caps, sorted(caps, reverse=True))
            self.assertEqual(caps[0], 1110000)
            self.assertEqual(caps[-1], 1020000)
            self.assertTrue(sector['valuation_coverage']['complete'])
            stock = sector['stocks'][0]
            self.assertEqual(stock['market_cap_date'], '2026-11-16')
            self.assertEqual(stock['turnover_rate'], 1)
            self.assertEqual(stock['volume_ratio'], 1.1)
            self.assertEqual(stock['rank_basis'], 'total_market_cap')
            self.assertNotIn('pe_ttm', stock)
            self.assertEqual(stock['stock_api_daily_basic']['vendor_pe_ttm'], 8.5)
        self.assert_safe(result)

    def test_membership_at_provider_row_limit_cannot_claim_complete(self):
        def truncated_members(payload):
            code = payload['params']['l1_code']
            rows = [{'l1_code': code, 'l1_name': 'Fixture Sector', 'l2_code': '801011.SI',
                     'l2_name': 'Fixture L2', 'l3_code': '850111.SI', 'l3_name': 'Fixture L3',
                     'ts_code': f'{prefix}{number:03}.SH', 'name': f'Stock {number}',
                     'in_date': '20200101', 'out_date': None, 'is_new': 'Y'}
                    for prefix in ('600', '601') for number in range(1000)]
            return pack(payload['fields'].split(','), rows)
        with fake_provider({'index_member_all': truncated_members}, primary=True):
            result = collect_stock_api(canonical_seed(), now=NOW)
        for sector in result['sectors']:
            membership = result['stock_api_state']['members'][sector['id']]
            self.assertEqual(len(sector['constituents']), 2000)
            self.assertTrue(membership['possibly_truncated'])
            self.assertFalse(membership['complete'])
            self.assertFalse(sector['membership_complete'])
            self.assertFalse(sector['valuation_coverage']['complete'])
        self.assert_safe(result)

    def test_empty_after_close_snapshot_retains_previous_date_and_marks_stale(self):
        data = canonical_seed()
        for sector in data['sectors']:
            sector.update(price=999, quote_date='2026-11-13', as_of='2026-11-13T15:00:00+08:00')
            sector['daily_quote'] = {'quote_date': '2026-11-13', 'price': 999}
        denied = {'code': 2002, 'msg': SECRET, 'data': None}
        with fake_provider({'rt_idx_k': denied, 'rt_sw_k': denied,
                            'sw_daily': lambda payload: pack(payload['fields'].split(','), [])},
                           primary=True) as (provider, _, _):
            result = collect_stock_api(data, now=NOW.replace(hour=15, minute=5))
        snapshots = [request for request in provider.requests if request['api_name'] == 'sw_daily']
        self.assertEqual(snapshots[0]['params']['trade_date'], '20261116')
        for sector in result['sectors']:
            self.assertEqual(sector['price'], 999)
            self.assertEqual(sector['quote_date'], '2026-11-13')
            self.assertTrue(sector['stale'])
            self.assertTrue(sector['daily_stale'])
        self.assert_safe(result)

    def test_unpublished_today_snapshot_falls_back_without_blocking_sw_history(self):
        def staged_sw(payload):
            params = payload['params']
            if params.get('trade_date') == '20261116':
                return pack(payload['fields'].split(','), [])
            codes = [params['ts_code']] if params.get('ts_code') else ['801010.SI', '801030.SI']
            rows = [{'ts_code': code, 'trade_date': '20261113', 'name': 'Fixture SW Index',
                     'open': 1000, 'low': 995, 'high': 1020, 'close': 1010, 'change': 10,
                     'pct_change': 1, 'vol': 123, 'amount': 456, 'pe': 18, 'pb': 2,
                     'float_mv': 3000, 'total_mv': 5000} for code in codes]
            return pack(payload['fields'].split(','), rows)
        denied = {'code': 2002, 'msg': SECRET, 'data': None}
        overrides = {'rt_idx_k': denied, 'rt_sw_k': denied, 'sw_daily': staged_sw}
        options = {'stock_api': {'news_enabled': False}}
        with fake_provider(overrides, primary=True) as (provider, _, _):
            result = collect_stock_api(canonical_seed(), options, NOW)
        requests = [request for request in provider.requests if request['api_name'] == 'sw_daily']
        self.assertIn('20261116', [request['params'].get('trade_date') for request in requests])
        self.assertIn('20261113', [request['params'].get('trade_date') for request in requests])
        self.assertTrue(any('ts_code' in request['params'] for request in requests))
        self.assertFalse(result['stock_api_state']['endpoints']['sw_daily'].get('cooldown_until'))
        for sector in result['sectors']:
            self.assertEqual(sector['daily_quote']['quote_date'], '2026-11-13')
            self.assertTrue(sector['daily_stale'])
        with fake_provider(overrides, primary=True) as (provider, _, _):
            again = collect_stock_api(result, options, NOW + timedelta(minutes=5))
        self.assertFalse(any(request['api_name'] == 'sw_daily' and
                             request['params'].get('trade_date') == '20261116'
                             for request in provider.requests))
        self.assert_safe(again)

    def test_configured_catchup_can_fetch_more_than_two_memberships_and_histories(self):
        rows = [{'index_code': code, 'industry_name': name, 'parent_code': '0', 'level': 'L1',
                 'industry_code': industry, 'is_pub': '1', 'src': 'SW2021'}
                for code, name, industry in [('801010.SI', '农林牧渔', '110000'),
                                             ('801030.SI', '基础化工', '220000'),
                                             ('801040.SI', '钢铁', '230000')]]
        override = lambda payload: pack(payload['fields'].split(','), rows)
        options = {'stock_api': {'max_requests': 999, 'max_index_calls': 0,
                                 'max_member_calls': 8, 'max_sw_history_calls': 8,
                                 'news_enabled': False}}
        with fake_provider({'index_classify': override}, primary=True) as (provider, opener, _):
            result = collect_stock_api(canonical_seed(), options, NOW)
        members = [request for request in provider.requests if request['api_name'] == 'index_member_all']
        histories = [request for request in provider.requests if request['api_name'] == 'sw_daily'
                     and 'ts_code' in request['params']]
        self.assertEqual(len(members), 3)
        self.assertEqual(len(histories), 3)
        self.assertLessEqual(opener.open.call_count, 12)
        self.assertEqual(len(result['sectors']), 3)
        self.assertTrue(all(len(sector['constituents']) == 12 for sector in result['sectors']))
        self.assert_safe(result)

    def test_news_is_separate_normalized_and_has_no_invented_sentiment(self):
        row = {'datetime': '2026-11-16 18:45:00', 'content': '<p>Content ' + SECRET + '</p>',
               'title': '<b>Headline</b>', 'channels': 'A股'}
        with fake_provider({'news': lambda payload: pack(payload['fields'].split(','), [row])}, primary=True) as (provider, _, _):
            result = collect_stock_api(canonical_seed(), now=NOW)
        news_request = next(request for request in provider.requests if request['api_name'] == 'news')
        self.assertEqual(news_request['params']['src'], 'cls')
        self.assertIn('start_date', news_request['params'])
        self.assertIn('end_date', news_request['params'])
        story = result['stock_api_news'][0]
        self.assertEqual(story['title'], 'Headline')
        self.assertEqual(story['source'], 'cls')
        self.assertEqual(story['upstream_provider'], 'unknown')
        self.assertNotIn('sentiment', story)
        self.assertNotIn('sector_ids', story)
        self.assertEqual(result.get('news', []), [])
        self.assert_safe(result)

    def test_mixed_a_b_and_unknown_codes_preserve_valid_rows_with_quality_counts(self):
        basic = primary_basic_rows() + [basic_row(ts_code=code)
                                      for code in ('200001.SZ', '900901.SH', '921001.BJ')]
        def mixed_members(payload):
            rows = member_rows(payload['params']['l1_code'])
            rows.extend(dict(rows[0], ts_code=code) for code in ('200001.SZ', '900901.SH', '921001.BJ'))
            return pack(payload['fields'].split(','), rows)
        overrides = {'daily_basic': lambda payload: pack(payload['fields'].split(','), basic),
                     'index_member_all': mixed_members}
        with fake_provider(overrides, primary=True):
            result = collect_stock_api(canonical_seed(), now=NOW)
        cache = result['stock_api_state']['daily_basic']
        quality = cache['quality']
        self.assertEqual((quality['received_count'], quality['accepted_count'],
                          quality['filtered_count'], quality['rejected_count']), (39, 36, 2, 1))
        self.assertTrue(all(not row['ts_code'].startswith(('200', '900', '921')) for row in cache['records']))
        self.assertLessEqual(len(quality['rejection_sample']), 10)
        for sector in result['sectors']:
            member_quality = result['stock_api_state']['members'][sector['id']]['quality']
            self.assertEqual((member_quality['received_count'], member_quality['accepted_count'],
                              member_quality['filtered_count'], member_quality['rejected_count']), (15, 12, 2, 1))
            self.assertEqual(len(sector['constituents']), 12)
            self.assertFalse(sector['membership_complete'])
            self.assertFalse(sector['valuation_coverage']['complete'])
            self.assertEqual(len(sector['stocks']), 10)
        self.assert_safe(result)

    def test_bad_optional_fields_are_null_without_discarding_valid_stock_rows(self):
        rows = primary_basic_rows()
        rows[0].update(pe_ttm=SECRET, pb='--', turnover_rate=-1, volume_ratio='NaN', circ_mv='--')
        rows[1].update(pe_ttm=0, pb=-2)
        with fake_provider({'daily_basic': lambda payload: pack(payload['fields'].split(','), rows)}, primary=True):
            result = collect_stock_api(canonical_seed(), now=NOW)
        cache = result['stock_api_state']['daily_basic']
        quality = cache['quality']
        self.assertEqual(quality['accepted_count'], 36)
        self.assertEqual(quality['rejected_count'], 0)
        records = {row['ts_code']: row for row in cache['records']}
        row = records['600000.SH']
        for key in ('vendor_pe_ttm', 'vendor_pb', 'turnover_rate', 'volume_ratio', 'circ_mv'):
            self.assertIsNone(row[key])
        for field in ('pe_ttm', 'pb', 'turnover_rate', 'volume_ratio', 'circ_mv'):
            self.assertGreaterEqual(quality['field_invalid_counts'].get(field, 0), 1)
        self.assertEqual(row['close'], 11.5)
        self.assertEqual(row['market_cap_cny'], 1000000)
        self.assertEqual(records['600001.SH']['vendor_pb'], -2)
        self.assertIsNone(records['600001.SH']['vendor_pe_ttm'])
        self.assertTrue(all(sector['valuation_coverage']['matched_count'] == 12 for sector in result['sectors']))
        self.assertTrue(all(not sector['valuation_coverage']['complete'] for sector in result['sectors']))
        self.assert_safe(result)

    def test_nonstandard_json_nan_still_rejects_the_payload_instead_of_persisting_it(self):
        rows = primary_basic_rows()
        rows[0]['pb'] = float('nan')
        with fake_provider({'daily_basic': lambda payload: pack(payload['fields'].split(','), rows)}, primary=True):
            result = collect_stock_api(canonical_seed(), now=NOW)
        self.assertFalse(result['stock_api_state'].get('daily_basic', {}).get('records'))
        self.assertEqual(result['stock_api_state']['endpoints']['daily_basic']['error_code'], 'invalid_number')
        self.assert_safe(result)

    def test_exited_members_are_filtered_before_irrelevant_bad_code_and_hierarchy(self):
        def exited_rows(payload):
            rows = member_rows(payload['params']['l1_code'])
            rows.append(dict(rows[0], is_new='N', ts_code=SECRET, l1_code='bad',
                             l2_code=None, l3_code=None, in_date='bad-date', out_date='bad-date'))
            rows.append(dict(rows[0], is_new='Y', ts_code=SECRET, l1_code='bad',
                             l2_code=None, l3_code=None, in_date='bad-date', out_date='20261101'))
            return pack(payload['fields'].split(','), rows)
        with fake_provider({'index_member_all': exited_rows}, primary=True):
            result = collect_stock_api(canonical_seed(), now=NOW)
        for sector in result['sectors']:
            quality = result['stock_api_state']['members'][sector['id']]['quality']
            self.assertEqual((quality['received_count'], quality['accepted_count'],
                              quality['filtered_count'], quality['rejected_count']), (14, 12, 2, 0))
            self.assertTrue(sector['membership_complete'])
            self.assertEqual(len(sector['stocks']), 10)
        self.assert_safe(result)

    def test_missing_or_bad_lower_hierarchy_keeps_verified_l1_mapping_but_marks_partial(self):
        def partial_rows(payload):
            rows = member_rows(payload['params']['l1_code'])
            rows[0].update(l2_code=None, l3_code=SECRET)
            return pack(payload['fields'].split(','), rows)
        with fake_provider({'index_member_all': partial_rows}, primary=True):
            result = collect_stock_api(canonical_seed(), now=NOW)
        for sector in result['sectors']:
            quality = result['stock_api_state']['members'][sector['id']]['quality']
            self.assertEqual(quality['accepted_count'], 12)
            self.assertEqual(quality['rejected_count'], 0)
            self.assertGreaterEqual(quality['field_invalid_counts'].get('l2_code', 0), 1)
            self.assertGreaterEqual(quality['field_invalid_counts'].get('l3_code', 0), 1)
            row = sector['constituents'][0]
            self.assertIsNone(row['l2_code'])
            self.assertIsNone(row['l3_code'])
            self.assertEqual(row['l1_code'], sector['id'])
            self.assertFalse(sector['membership_complete'])
            self.assertEqual(len(sector['stocks']), 10)
        self.assert_safe(result)

    def test_exact_duplicates_deduplicate_but_conflicting_codes_are_fully_quarantined(self):
        first, conflict, final = [basic_row(ts_code=f'60000{number}.SH') for number in range(3)]
        basic = [first, deepcopy(first), conflict, dict(conflict, total_mv=999), final]
        def duplicate_members(payload):
            first, conflict, final = member_rows(payload['params']['l1_code'])[:3]
            rows = [first, deepcopy(first), conflict, dict(conflict, name='Conflicting name'), final]
            return pack(payload['fields'].split(','), rows)
        overrides = {'daily_basic': lambda payload: pack(payload['fields'].split(','), basic),
                     'index_member_all': duplicate_members}
        with fake_provider(overrides, primary=True):
            result = collect_stock_api(canonical_seed(), now=NOW)
        cache = result['stock_api_state']['daily_basic']
        quality = cache['quality']
        self.assertEqual((quality['received_count'], quality['accepted_count'],
                          quality['duplicate_count'], quality['rejected_count']), (5, 2, 1, 2))
        self.assertEqual({row['ts_code'] for row in cache['records']}, {'600000.SH', '600002.SH'})
        for sector in result['sectors']:
            quality = result['stock_api_state']['members'][sector['id']]['quality']
            self.assertEqual((quality['received_count'], quality['accepted_count'],
                              quality['duplicate_count'], quality['rejected_count']), (5, 2, 1, 2))
            self.assertEqual(len(sector['constituents']), 2)
            self.assertTrue(all(not row['code'].endswith('001') for row in sector['constituents']))
            self.assertFalse(sector['membership_complete'])
        self.assert_safe(result)

    def test_one_empty_membership_only_cools_that_industry_and_other_industries_continue(self):
        def rows_for_sector(payload):
            code = payload['params']['l1_code']
            rows = member_rows(code)
            if code == '801010.SI':
                rows = [dict(rows[0], ts_code='921001.BJ')]
            return pack(payload['fields'].split(','), rows)
        with fake_provider({'index_member_all': rows_for_sector}, primary=True) as (provider, _, _):
            result = collect_stock_api(canonical_seed(), now=NOW)
        requested = [request['params']['l1_code'] for request in provider.requests
                     if request['api_name'] == 'index_member_all']
        self.assertEqual(requested, ['801010.SI', '801030.SI'])
        sectors = {sector['id']: sector for sector in result['sectors']}
        self.assertEqual(len(sectors['801010.SI']['constituents']), 0)
        self.assertEqual(len(sectors['801030.SI']['constituents']), 12)
        self.assertEqual(len(sectors['801030.SI']['stocks']), 10)
        self.assertTrue(result['stock_api_state']['member_attempts']['801010.SI']['cooldown_until'])
        self.assertFalse(result['stock_api_state']['endpoints']['index_member_all'].get('cooldown_until'))
        with fake_provider({'index_member_all': rows_for_sector}, primary=True) as (provider, _, _):
            cached = collect_stock_api(result, now=NOW + timedelta(minutes=5))
        self.assertNotIn('801010.SI', [request['params']['l1_code'] for request in provider.requests
                                    if request['api_name'] == 'index_member_all'])
        cached['stock_api_state']['endpoints']['index_member_all']['cooldown_until'] = (NOW + timedelta(hours=1)).isoformat()
        with fake_provider(primary=True) as (provider, _, _):
            blocked = collect_stock_api(cached, now=NOW + timedelta(minutes=20))
        self.assertNotIn('index_member_all', [request['api_name'] for request in provider.requests])
        self.assert_safe(blocked)

    def test_unavailable_valuation_still_shows_names_in_membership_order_without_prices(self):
        denied = {'code': 2002, 'msg': SECRET, 'data': None}
        def reversed_members(payload):
            rows = list(reversed(member_rows(payload['params']['l1_code'])))
            return pack(payload['fields'].split(','), rows)
        with fake_provider({'daily_basic': denied, 'index_member_all': reversed_members}, primary=True):
            result = collect_stock_api(canonical_seed(), now=NOW)
        for sector in result['sectors']:
            self.assertEqual(len(sector['constituents']), 12)
            self.assertEqual(len(sector['stocks']), 10)
            self.assertEqual([stock['name'] for stock in sector['stocks']],
                             [f'Stock {number}' for number in range(11, 1, -1)])
            self.assertEqual(sector['rank_basis'], 'membership_order')
            self.assertIsNone(sector['rank_date'])
            for stock in sector['stocks']:
                self.assertIsNone(stock['price'])
                self.assertEqual(stock['rank_basis'], 'membership_order')
                self.assertIsNone(stock['rank_date'])
        self.assert_safe(result)


if __name__ == '__main__':
    unittest.main()
