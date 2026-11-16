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
    def test_collection_boundary_never_backdates_prediction(self):
        start=datetime(2026,11,16,9,24,tzinfo=TZ)
        end=datetime(2026,11,16,9,26,tzinfo=TZ)
        payload={'market':[], 'sectors':[], 'news':[{'id':'n','first_seen_at':start.isoformat(timespec='seconds'),'fetched_at':start.isoformat(timespec='seconds')}], 'sources':[]}
        with tempfile.TemporaryDirectory() as tmp, patch('pipeline.run.datetime') as clock, patch('pipeline.run.fetch_data',return_value=payload):
            clock.now.side_effect=[start,end]
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
