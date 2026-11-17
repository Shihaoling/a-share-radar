"""Page-only collection, timestamp honesty, bounded access and recovery."""
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import json
from pipeline.page_quotes import parse_rows, merge_snapshot, collection_open, Collector

NOW = datetime.fromisoformat('2026-11-17T10:05:00+08:00')

class PageQuotesTests(TestCase):
    def test_rendered_index_and_board_rows_keep_distinct_identity(self):
        index = {'headers':['代码','名称','最新价','涨跌幅','昨收'], 'cells':['000001','上证指数','3,584.58','-0.50%','3602.57']}
        board = {'headers':['板块名称','涨跌幅','最新价'], 'cells':['银行','+1.23%','1300.5'], 'links':['//quote.eastmoney.com/bk/90.BK0475.html']}
        i, b = parse_rows([index], 'index', NOW)[0], parse_rows([board], 'sector', NOW)[0]
        self.assertEqual(i['id'],'sh000001'); self.assertEqual(i['price'],3584.58)
        self.assertEqual(b['id'],'em:BK0475'); self.assertEqual(b['taxonomy'],'EASTMONEY')
        self.assertIsNone(i['quote_timestamp']); self.assertEqual(i['timestamp_quality'],'page_observed')

    def test_missing_invalid_and_duplicate_rows_never_create_values(self):
        rows=[{'headers':['代码','名称','最新价'], 'cells':['000001','上证指数',v]} for v in ['--','NaN','0','-10','Infinity']]
        self.assertEqual(parse_rows(rows,'index',NOW),[])
        good={'headers':['代码','名称','最新价'], 'cells':['000001','上证指数','3584.58']}
        self.assertEqual(len(parse_rows([good,good],'index',NOW)),1)

    def test_diagnostic_closed_market_never_adds_intraday_ticks(self):
        q={'id':'sh000001','kind':'index','observed_at':NOW.isoformat(),'price':3584.58}
        result=merge_snapshot({},[q],{'status':'ok'},NOW,sampled=False)
        self.assertEqual(result['intraday'],{}); self.assertEqual(len(result['market']),1)

    def test_failure_keeps_previous_sample_time_and_curve(self):
        q={'id':'sh000001','kind':'index','observed_at':NOW.isoformat(),'price':3584.58}
        old=merge_snapshot({},[q],{'status':'ok'},NOW)
        result=merge_snapshot(old,[],{'status':'error'},datetime.fromisoformat('2026-11-18T10:00:00+08:00'))
        self.assertEqual(result['trade_date'],'2026-11-17'); self.assertEqual(result['intraday'],old['intraday'])
        self.assertEqual(result['collector']['status'],'error')

    def test_holiday_lunch_and_after_hours_do_not_launch_browser(self):
        self.assertTrue(collection_open(NOW))
        for value in ['2026-11-17T12:00:00+08:00','2026-11-17T16:00:00+08:00','2026-11-21T10:00:00+08:00']:
            self.assertFalse(collection_open(datetime.fromisoformat(value)))
        with TemporaryDirectory() as folder, patch('pipeline.page_quotes._Gate') as gate:
            Collector(Path(folder)/'q.json').run_once(datetime.fromisoformat('2026-11-17T12:00:00+08:00'))
            gate.assert_not_called()

    def test_persisted_cooldown_prevents_any_requests(self):
        with TemporaryDirectory() as folder, patch('pipeline.page_quotes._Gate') as gate:
            path=Path(folder)/'q.json';old={'collector':{'retry_at':'2026-11-17T11:00:00+08:00'}};path.write_text(json.dumps(old))
            self.assertEqual(Collector(path).run_once(NOW),old);gate.assert_not_called()
