"""Primary-source ownership and latest-quote integration contracts."""
import unittest
from datetime import datetime, date, timedelta
from unittest.mock import patch
from pipeline.sources import fetch_data
from pipeline.fallback import _apply_bars
from pipeline.intraday import collect_intraday, _stock_symbol
from pipeline.run import bootstrap_data, bootstrap_coverage
from pipeline.calendar import is_trading_day


class PrimaryPipelineTests(unittest.TestCase):
    def test_primary_refresh_does_not_reseed_legacy_sectors(self):
        bank = {'id':'801780.SI','name':'银行','taxonomy':'SW2021','level':'L1','history':[],'stocks':[]}
        result = fetch_data({'sectors':[bank]}, {'network_enabled':False,'primary_provider':'stock_api'}, datetime.fromisoformat('2026-11-16T14:00:00+08:00'))
        self.assertEqual([s['id'] for s in result['sectors']], ['801780.SI'])
        self.assertEqual(result['sectors'][0]['taxonomy'], 'SW2021')

    def test_backup_preserves_primary_same_day_close(self):
        item = {'history':[{'date':'2026-11-16','close':3602.5698,'source':'stock_api'}],
                'price':3602.5698,'provider':'stock_api','as_of':'2026-11-16T15:00:00+08:00'}
        _apply_bars(item, [{'date':'2026-11-16','close':3602.57,'source':'tencent'}], 'tencent', datetime.fromisoformat('2026-11-16T17:00:00+08:00'), 'https://example.test')
        self.assertEqual(item['history'][0]['source'], 'stock_api')
        self.assertEqual(item['price'],3602.5698)
        self.assertEqual(item['provider'],'stock_api')

    def test_fresh_primary_snapshot_prevents_redundant_backup(self):
        quote = {'provider':'stock_api','price':3602.57,'quote_timestamp':'2026-11-16T14:00:00+08:00','timestamp_quality':'exchange_timestamp'}
        data = {'market':[{'id':'sh000001','live_quote':quote}], 'sectors':[], 'sources':[],
                'intraday':{'sh000001':{'latest':{**quote,'provider':'tencent','price':3599,'quote_timestamp':'2026-11-16T13:55:00+08:00'}}}}
        with patch('pipeline.intraday._BatchTransport') as transport:
            result = collect_intraday(data, {'primary_provider':'stock_api'}, datetime.fromisoformat('2026-11-16T14:01:00+08:00'))
        transport.assert_not_called()
        self.assertEqual(result['market'][0]['live_quote']['provider'],'stock_api')
        self.assertTrue(result['market'][0]['live_quote']['is_live'])
        self.assertEqual(_stock_symbol('000001.SZ'),'sz000001')
        self.assertEqual(_stock_symbol('600000.SH'),'sh600000')

    def test_bootstrap_stops_on_cooldown_without_retrying(self):
        with patch('pipeline.run.run', return_value={'stock_api_state': {'cooldown_until':'2099-01-01T00:00:00+08:00'}}) as run:
            bootstrap_data(8)
        self.assertEqual(run.call_count, 1)

    def test_bootstrap_reuses_other_sources_and_stops_without_progress(self):
        with patch('pipeline.run.run', return_value={}) as run:
            bootstrap_data(8)
        self.assertEqual(run.call_count, 2)
        self.assertFalse(run.call_args_list[0].kwargs['stock_api_only'])
        self.assertTrue(run.call_args_list[1].kwargs['stock_api_only'])

    def test_bootstrap_requires_real_labeled_samples_not_raw_bar_count(self):
        bars = []
        day = date(2026, 1, 1)
        while len(bars) < 181:
            if is_trading_day(day):
                bars.append({'date': day.isoformat(), 'open': 100., 'close': 101., 'high':102., 'low':99., 'complete':True})
            day += timedelta(days=1)
        entity = {'id':'801780.SI', 'taxonomy':'SW2021', 'level':'L1', 'source_history':{'stock_api':bars[:180]}}
        now = datetime.fromisoformat('2026-11-16T18:00:00+08:00')
        self.assertEqual(bootstrap_coverage({'sectors':[entity]}, now=now)['history_ready'], 0)
        entity['source_history']['stock_api'] = bars
        self.assertEqual(bootstrap_coverage({'sectors':[entity]}, now=now)['history_ready'], 1)
        entity['source_history']['stock_api'] = bars[:170] + bars[:50]
        self.assertEqual(bootstrap_coverage({'sectors':[entity]}, now=now)['history_ready'], 0)


if __name__ == '__main__':
    unittest.main()
