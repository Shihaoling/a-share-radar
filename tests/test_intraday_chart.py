from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'deploy/tencent-preview')]
from asset_research import ResearchStore
from intraday_chart import archive_heat, relative_changes
from relay_quotes import publish

NOW=datetime.fromisoformat('2026-11-19T12:10:00+08:00')


class IntradayChartTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        self.store=ResearchStore(root,root/'public',root/'runtime')

    def test_archives_source_heat_without_inventing_an_index_level(self):
        code='885864.TI'; path=self.store.out/'sector-heat-samples.json'
        base={'ts_code':code,'data_type':'概念板块','trade_date':'20261119'}
        rows=[{**base,'rank_time':'2026-11-19 10:30:00','pct_change':-1.2},
              {**base,'rank_time':'2026-11-19 11:00:00','pct_change':0.0},
              {**base,'rank_time':'2026-11-19 15:00:00','pct_change':8.0}]
        archive_heat(path,rows,{code},NOW,publish)
        with closing(self.store.db()) as db:
            chart=self.store.intraday.render(db,'sector',code,{},NOW)
        self.assertEqual(chart['measure'],'change_pct')
        self.assertEqual([p[1] for p in chart['points']],[-1.2,0.0])
        self.assertIsNone(chart['previous_close'])

    def test_local_capture_preserves_real_time_gaps_and_latest_sample_in_minute(self):
        code='000001.SZ'
        for stamp,price in [('10:05:04',12),('10:05:35',13),('10:08:17',12.5)]:
            q={'id':code,'quote_date':'2026-11-19','quote_timestamp':'2026-11-19T'+stamp+'+08:00','price':price}
            publish(self.store.out/'stock-quotes.json',{'stocks':[q]})
            self.store.intraday.capture(NOW)
        with closing(self.store.db()) as db:
            chart=self.store.intraday.render(db,'stock',code,q,NOW)
        self.assertEqual(chart['points'],[['2026-11-19T10:05:35+08:00',13],['2026-11-19T10:08:17+08:00',12.5]])

    def test_relative_strength_never_uses_a_future_or_distant_benchmark(self):
        day="2026-11-19"
        def at(hhmmss):return day+"T"+hhmmss+"+08:00"
        series=[[at("10:05:45"),102],[at("10:08:00"),99]]
        ref={"quote_date":day,"previous_close":100}
        bench=[[at("10:05:00"),101],[at("10:05:50"),150]]
        paired=relative_changes(series,100,False,ref,bench,day)
        self.assertEqual(len(paired),1)
        self.assertAlmostEqual(paired[0][1],1)
        self.assertEqual(relative_changes(series,None,False,ref,bench,day),[])

    def test_first_open_prefers_archived_current_day_when_last_quote_is_from_yesterday(self):
        code='000685.SH';stamp='2026-11-19T10:00:02+08:00'
        publish(self.store.out/'sector-intraday'/(code+'.json'),
                {'id':code,'trade_date':'2026-11-19','points':[[stamp,123],['2026-11-19T14:00:00+08:00',150]]})
        old=dict(quote_date='2026-11-18',quote_timestamp='2026-11-18T14:59:58+08:00',price=100,previous_close=80)
        with closing(self.store.db()) as db:
            chart=self.store.intraday.render(db,'sector',code,old,NOW)
        self.assertEqual((chart['day'],chart['points'],chart['previous_close']),('2026-11-19',[[stamp,123]],None))

    def test_sw_percentage_without_index_level_is_saved_and_reopens_independently(self):
        code='801001.SI';stamp='2026-11-19T10:20:10+08:00'
        quote=dict(id=code,quote_date='2026-11-19',quote_timestamp=stamp,change_pct=-1.8,basis='constituent_estimate')
        publish(self.store.out/'stock-quotes.json',{'board_estimates':[quote]})
        self.store.intraday.capture(NOW)
        with closing(self.store.db()) as db:
            chart=self.store.intraday.render(db,'sector',code,{},NOW)
        self.assertEqual((chart['measure'],chart['points'],chart['previous_close']),('weighted_change_pct',[[stamp,-1.8]],None))

    def test_index_series_uses_matching_code_and_date(self):
        published={'trade_date':'2026-11-19','series':{'sh000300':[['2026-11-19T10:05:00+08:00',3901]]}}
        publish(self.store.out/'today-intraday.json',published)
        publish(self.store.out/'minute-recovery.json',{'trade_date':'2026-11-18','series':published['series']})
        with closing(self.store.db()) as db:
            chart=self.store.intraday.render(db,'index','000300.SH',{},NOW)
        self.assertEqual(chart['points'],published['series']['sh000300'])
