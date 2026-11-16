import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo
from pipeline.run import run
from pipeline.review import daily_review
TZ=ZoneInfo('Asia/Shanghai')
class PipelineTests(unittest.TestCase):
    def test_stock_api_only_reuses_other_sources_and_passes_state(self):
        now=datetime(2026,11,16,19,2,tzinfo=TZ)
        cached={'data_mode':'live','market':[],'sectors':[], 'news':[{'id':'old'}], 'sources':[], 'stock_api_state':{'index_cursor':2}}
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'dashboard.json'
            path.write_text(json.dumps(cached))
            with patch('pipeline.run.datetime') as clock, patch('pipeline.run.fetch_data') as fetch, patch('pipeline.run.enrich_with_fallback') as daily, patch('pipeline.run.collect_intraday',side_effect=lambda data,*args:data) as live, patch('pipeline.run.enrich_with_stock_api',side_effect=lambda data,*args:data) as api:
                clock.now.side_effect=[now,now,now]
                result=run(output=path,stock_api_only=True)
            fetch.assert_not_called()
            daily.assert_not_called()
            live.assert_called_once()
            self.assertFalse(live.call_args.args[1]['network_enabled'])
            api.assert_called_once()
            self.assertEqual(api.call_args.args[0]['stock_api_state'],cached['stock_api_state'])
            self.assertEqual([n['id'] for n in result['news']],[n['id'] for n in cached['news']])

    def test_quote_only_never_refreshes_news_or_daily_history(self):
        now=datetime(2026,11,16,10,2,tzinfo=TZ)
        cached={'data_mode':'live','market':[],'sectors':[], 'news':[{'id':'old'}], 'sources':[], 'intraday':{'saved':{'points':[]}}}
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'dashboard.json'
            path.write_text(json.dumps(cached))
            with patch('pipeline.run.datetime') as clock, patch('pipeline.run.fetch_data') as fetch, patch('pipeline.run.enrich_with_fallback') as daily:
                clock.now.side_effect=[now,now,now]
                result=run(offline=True,output=path,quotes_only=True)
            fetch.assert_not_called()
            daily.assert_not_called()
            self.assertEqual([n['id'] for n in result['news']],[n['id'] for n in cached['news']])
            self.assertIn('saved',result['intraday'])

    def test_quote_only_holiday_skips_network_and_write(self):
        now=datetime(2026,10,1,10,2,tzinfo=TZ)
        with tempfile.TemporaryDirectory() as tmp, patch('pipeline.run.datetime') as clock, patch('pipeline.run.collect_intraday') as collect:
            clock.now.return_value=now
            path=Path(tmp)/'dashboard.json'
            self.assertEqual(run(output=path,quotes_only=True),{})
            self.assertFalse(path.exists())
            collect.assert_not_called()

    def test_collection_boundary_never_backdates_prediction(self):
        start=datetime(2026,11,16,9,24,tzinfo=TZ)
        end=datetime(2026,11,16,9,26,tzinfo=TZ)
        payload={'market':[], 'sectors':[], 'news':[{'id':'n','first_seen_at':start.isoformat(timespec='seconds'),'fetched_at':start.isoformat(timespec='seconds')}], 'sources':[]}
        with tempfile.TemporaryDirectory() as tmp, patch('pipeline.run.datetime') as clock, patch('pipeline.run.fetch_data',return_value=payload):
            clock.now.side_effect=[start,end,end]
            result=run(offline=True,output=Path(tmp)/'dashboard.json')
            self.assertEqual(result['market_session']['target_kind'],'today_close')
            self.assertEqual(result['news'][0]['first_seen_at'],end.isoformat())
            self.assertEqual(result['generated_at'],end.isoformat())
    def test_sentiment_alignment_requires_exact_final_daily_bars(self):
        news=[{'first_seen_at':'2026-11-16T08:00:00+08:00','sentiment':'positive','sector_ids':['a'],'credibility':'trusted'}]
        today={'date':'2026-11-16','open':101,'close':102}
        missing={'date':'2026-11-12','open':100,'close':100}
        prev={'date':'2026-11-13','open':100,'close':100}
        now=datetime(2026,11,16,16,tzinfo=TZ)
        def result(rows):
            return daily_review({'news':news,'sectors':[{'id':'a','history':rows}]},now)['sentiment']
        self.assertEqual(result([missing,today])['alignment_count'],0)
        self.assertEqual(result([prev,{**today,'complete':False}])['alignment_count'],0)
        self.assertEqual(result([prev,{**today,'available_at':'2026-11-17T09:00:00+08:00'}])['alignment_count'],0)
        self.assertEqual(result([prev,today])['alignment_rate'],1)
    def test_late_target_review_uses_original_target_day(self):
        row={'id':'a','sector_id':'a','issued_at':'2026-11-12T16:00:00+08:00','target_date':'2026-11-13','target_kind':'next_open','actual_price':100,'error_pct':1,'baseline_error_pct':2,'direction_correct':True}
        r=daily_review({'ledger':[row]},datetime(2026,11,16,10,tzinfo=TZ),target_date='2026-11-13')
        self.assertEqual(r['date'],'2026-11-13')
        self.assertEqual(r['settled_count'],1)
if __name__=='__main__':unittest.main()
