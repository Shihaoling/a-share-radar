"""Batched intraday transport and timestamp contracts, with no real HTTP."""
from copy import deepcopy
from datetime import datetime
import unittest
import urllib.error
from unittest.mock import Mock, patch

from pipeline.intraday import collect_intraday, parse_tencent

NOW = datetime.fromisoformat('2026-11-16T14:59:30+08:00')


def payload(stamp='20261116145920', price='3100'):
    fields = [''] * 31
    for index, value in {1:'Fixture Index', 2:'000001', 3:price, 4:'3000', 5:'3010', 6:'100', 30:stamp}.items():
        fields[index] = value
    return ('v_sh000001="' + '~'.join(fields) + '";').encode()


class Response:
    status = 200
    headers = {}
    def __init__(self, body): self.body = body
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, size): return self.body[:size]


class IntradayTests(unittest.TestCase):
    def seed(self):
        return {'market':[{'id':'sh000001', 'name':'Index', 'price':2990,
                           'as_of':'2026-11-13T15:00:00+08:00', 'market_cap':123,
                           'history':[{'date':'2026-11-13','open':2995,'close':3000}]}], 'sectors':[]}

    def test_cold_start_counts_robots_and_never_exceeds_two_http(self):
        data = self.seed()
        original = deepcopy(data)
        opener = Mock()
        opener.open.side_effect = [Response(b'User-agent: *\nAllow: /\n'), Response(payload())]
        with patch('pipeline.intraday.urllib.request.build_opener', return_value=opener), patch('pipeline.sources.time.sleep'):
            result = collect_intraday(data, now=NOW)
        self.assertEqual(opener.open.call_count, 2)
        self.assertTrue(opener.open.call_args_list[0].args[0].full_url.endswith('/robots.txt'))
        self.assertEqual(result['intraday_state']['http_requests'], 2)
        self.assertEqual(data, original)
        for field in ('price', 'as_of', 'history', 'market_cap'):
            self.assertEqual(result['market'][0][field], original['market'][0][field])
        live = result['market'][0]['live_quote']
        self.assertEqual(live['quote_timestamp'], '2026-11-16T14:59:20+08:00')
        self.assertEqual(live['timestamp_quality'], 'source_reported')
        self.assertTrue(live['is_live'])
        self.assertIsNone(live['pe_ttm'])
        self.assertEqual(result['price_observations'][0]['field'], 'last')

    def test_cache_and_offline_restore_quote_but_recompute_freshness(self):
        data = self.seed()
        quote = {'price':3100, 'quote_timestamp':'2026-11-16T14:59:20+08:00', 'timestamp_quality':'source_reported', 'is_live':True, 'provider':'tencent'}
        data['intraday'] = {'sh000001': {'latest':quote, 'providers':{'tencent':quote}}}
        data['intraday_state'] = {'last_attempt_at':'2026-11-16T14:59:30+08:00'}
        original = deepcopy(data)
        now = datetime.fromisoformat('2026-11-16T15:00:00+08:00')
        with patch('pipeline.intraday.urllib.request.build_opener', side_effect=AssertionError('unexpected network')):
            cached = collect_intraday(data, now=now)
            offline = collect_intraday(data, {'network_enabled':False}, now=now)
        for result in (cached, offline):
            self.assertEqual(result['market'][0]['live_quote']['price'],3100)
            self.assertFalse(result['market'][0]['live_quote']['is_live'])
            self.assertEqual(result['market'][0]['live_quote']['age_seconds'],40)
        self.assertEqual(data, original)

    def test_429_retry_after_and_domain_rotation_stay_inside_budget(self):
        opener = Mock()
        opener.open.side_effect = [urllib.error.HTTPError('https://qt.gtimg.cn/robots.txt',429,'Limited',{'Retry-After':'120'},None),
                                  urllib.error.HTTPError('https://hq.sinajs.cn/robots.txt',403,'Forbidden',{},None)]
        with patch('pipeline.intraday.urllib.request.build_opener',return_value=opener), patch('pipeline.sources.time.sleep'):
            result = collect_intraday(self.seed(), now=NOW)
        self.assertEqual(opener.open.call_count,2)
        self.assertEqual(result['intraday_state']['http_requests'],2)
        source = next(s for s in result['sources'] if s['id']=='tencent_intraday')
        self.assertEqual(source['retry_after'],'2026-11-16T15:01:30+08:00')
        self.assertNotIn('live_quote',result['market'][0])

    def test_invalid_or_future_market_timestamp_is_never_source_verified(self):
        for timestamp in ('', '20261117145920', 'not-a-time'):
            quote = parse_tencent(payload(timestamp).decode(), NOW)['sh000001']
            self.assertIsNone(quote['quote_timestamp'])
            self.assertEqual(quote['timestamp_quality'],'retrieved_only')


if __name__ == '__main__': unittest.main()
