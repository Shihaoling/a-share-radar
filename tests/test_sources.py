"""Source safety contracts using synthetic provider responses; never network."""
from contextlib import nullcontext
from datetime import datetime
from types import SimpleNamespace
import json
import unittest
import urllib.error
from unittest.mock import Mock, patch

from pipeline.sources import fetch_data, _Gate, RateLimited, SourceUnavailable

NOW = datetime.fromisoformat('2026-11-16T10:00:00+08:00')


class SourceTests(unittest.TestCase):
    def test_offline_bootstrap_has_no_invented_values_or_network(self):
        with patch('pipeline.sources.urllib.request.urlopen', side_effect=AssertionError('unexpected network')), patch('pipeline.sources.importlib.import_module', side_effect=AssertionError('unexpected provider import')):
            data = fetch_data({}, {'network_enabled': False}, NOW)
        json.dumps(data, allow_nan=False)
        self.assertGreaterEqual(len(data['sectors']), 51)
        self.assertEqual(data['news'], [])
        for entity in data['market'] + data['sectors']:
            self.assertIsNone(entity['price'])
            self.assertIsNone(entity['change_pct'])
            self.assertEqual(entity['history'], [])
            self.assertEqual(entity['market_cap_history'], [])

    def test_retrieved_quote_is_not_a_verified_exchange_timestamp(self):
        provider = SimpleNamespace(
            index_zh_spot_em=lambda **kw: [{'代码': '000001', '最新价': 3000.0, '涨跌幅': float('nan')}],
            stock_board_industry_name_em=lambda: [],
            stock_board_concept_name_em=lambda: [])
        config = {'feeds': [], 'max_history_calls': 0, 'max_constituent_calls': 0}
        with patch('pipeline.sources.urllib.request.urlopen', side_effect=AssertionError('unexpected network')), patch.object(_Gate, 'guard_requests', return_value=nullcontext()), patch('pipeline.sources.importlib.import_module', return_value=provider):
            data = fetch_data({}, config, NOW)
        quote = next(r for r in data['market'] if r['id'] == 'sh000001')
        self.assertEqual(quote['price'], 3000)
        self.assertEqual(quote['timestamp_quality'], 'retrieved_only')
        self.assertIsNone(quote['change_pct'])
        json.dumps(data, allow_nan=False)

    def test_feed_excludes_unrelated_world_news_and_labels_rules(self):
        feed = {'id': 'test_rss', 'name': 'Test feed', 'type': 'rss', 'url': 'https://example.test/rss', 'credibility': 'trusted'}
        rss = b'''<rss><channel>
          <item><title>Semiconductor growth boosts production</title><link>https://example.test/chips</link></item>
          <item><title>Central bank monetary policy decision</title><link>https://example.test/rates</link></item>
          <item><title>A local football team wins a friendly match</title><link>https://example.test/sports</link></item>
          <item><title>Unconfirmed semiconductor expansion rumor</title><link>https://example.test/rumor</link></item>
        </channel></rss>'''
        with patch.object(_Gate, 'get', return_value=(rss, {})), patch('pipeline.sources.urllib.request.urlopen', side_effect=AssertionError('unexpected network')):
            data = fetch_data({}, {'akshare': {'enabled': False}, 'feeds': [feed]}, NOW)
        by_url = {n['url']: n for n in data['news']}
        self.assertNotIn('https://example.test/sports', by_url)
        self.assertEqual(len(by_url), 3)
        self.assertIn('semiconductor', by_url['https://example.test/chips']['sector_ids'])
        self.assertTrue(by_url['https://example.test/rates']['market_ids'])
        for n in by_url.values():
            self.assertEqual(n['market_scope'], 'A-share')
            self.assertEqual(n['analysis_method'], 'keyword_rules')
            self.assertEqual(n['sentiment_method'], 'lexicon_rules')
            self.assertFalse(n['relevance_verified'])
            self.assertEqual(n['impact'], 'uncertain')
        self.assertEqual(by_url['https://example.test/rumor']['credibility'], 'rumor')
        self.assertFalse(by_url['https://example.test/rumor']['include_in_sentiment'])

    def test_429_honors_retry_after_and_stops_same_domain_requests(self):
        url = 'https://data.example.test/feed'
        gate = _Gate({'respect_robots': False, 'user_agent': 'test', 'min_request_interval_seconds': 0, 'max_retries': 1})
        error = urllib.error.HTTPError(url, 429, 'rate limited', {'Retry-After': '120'}, None)
        with patch('pipeline.sources.urllib.request.urlopen', side_effect=error) as request:
            with self.assertRaises(RateLimited) as caught:
                gate.get(url)
            self.assertEqual(caught.exception.retry_seconds, 120)
            with self.assertRaises(SourceUnavailable):
                gate.get(url + '?second=1')
        self.assertEqual(request.call_count, 1)

    def test_oldest_sector_beyond_fiftieth_is_still_scheduled(self):
        previous = {'sectors': [
            {'id': f'watch_{i:02}', 'code': f'BK{i:04}', 'name': f'Fixture {i}', 'kind': 'industry', 'enabled': True,
             'history_attempt_at': None if i == 54 else '2026-11-13T16:00:00+08:00'}
            for i in range(55)]}
        rows = [{'板块代码': s['code'], '板块名称': s['name'], '最新价': 1000, '涨跌幅': 0} for s in previous['sectors']]
        history = Mock(return_value=[{'日期': '2026-11-13', '开盘': 1000, '收盘': 1001}])
        provider = SimpleNamespace(index_zh_spot_em=lambda **kw: [], stock_board_industry_name_em=lambda: rows,
                                   stock_board_concept_name_em=lambda: [], stock_board_industry_hist_em=history)
        config = {'feeds': [], 'akshare': {'market_indices': []}, 'max_history_calls': 1, 'max_constituent_calls': 0}
        with patch('pipeline.sources.urllib.request.urlopen', side_effect=AssertionError('unexpected network')), patch.object(_Gate, 'guard_requests', return_value=nullcontext()), patch('pipeline.sources.importlib.import_module', return_value=provider):
            data = fetch_data(previous, config, NOW)
        history.assert_called_once()
        self.assertEqual(history.call_args.kwargs['symbol'], 'BK0054')
        target = next(s for s in data['sectors'] if s['id'] == 'watch_54')
        self.assertEqual(target['history'][-1]['date'], '2026-11-13')


if __name__ == '__main__':
    unittest.main()
