import gzip
import json
from datetime import datetime
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'deploy/tencent-preview')]
from asset_research import ResearchStore
from relay_quotes import watched_stocks

NOW=datetime.fromisoformat("2026-11-19T11:30:30+08:00")


class ResearchIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        public=self.root/'public'
        self.save(public/'view.json',{'market':[{'id':'sh000300','name':'沪深300','history':[
            {'date':'2026-11-17','close':4000}]}], 'sectors':[]})
        self.save(public/'sector-catalog.json',{'items':[{'code':'000685.SH','name':'科创芯片'}]})
        self.save(self.root/'sector-directory/stock-names.json',{'000001.SZ':'平安银行'})
        self.store=ResearchStore(self.root,public,self.root/'runtime')
        self.q=dict(id='000001.SZ',name='平安银行',price=12,previous_close=10,open=11,low=10,high=12,
                    change_pct=20,quote_date='2026-11-19',quote_timestamp='2026-11-19T11:30:00+08:00')
        self.save(self.store.out/'live-quotes.json',{'market':[{**self.q,'id':'sh000300','price':10.2,'change_pct':2}]})

    @staticmethod
    def save(p,obj):
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(json.dumps(obj))

    def test_partial_feeds_recover_and_stock_demand_is_bounded(self):
        self.assertIsNone(self.store.instrument('stock','999999.SZ',NOW))
        self.assertIsNone(self.store.instrument('index','000001.SZ',NOW))
        packet=self.store.instrument('stock','000001.SZ',NOW)
        self.assertIsNone(packet['price'])
        path=self.store.private/'live-watch.json'
        self.assertEqual(watched_stocks(path,NOW),{'000001.SZ':'000001.SZ'})
        self.save(self.store.out/'stock-quotes.json',{'stocks':[self.q]})
        packet=self.store.instrument('stock','000001.SZ',NOW)
        values={m['key']:m['value'] for m in packet['metrics']}
        self.assertEqual(packet['price'],12)
        self.assertAlmostEqual(values['amplitude_pct'],20)
        self.assertAlmostEqual(values['relative_hs300_pp'],18)
        self.save(self.store.out/'stock-quotes.json',{'stocks':[]})
        self.assertEqual(self.store.instrument('stock','000001.SZ',NOW)['price'],12)
        later=datetime.fromisoformat('2026-11-19T11:35:00+08:00')
        self.assertEqual(watched_stocks(path,later),{})

    def test_one_broken_publication_does_not_block_other_series(self):
        member=self.store.out/'sector-members/000685.SH.json'
        self.save(member,{'members':[None]})
        self.save(self.store.out/'sector-quotes.json',{'sectors':[{**self.q,'id':'000685.SH'}]})
        out=self.store.instrument('sector','000685.SH',NOW)
        self.assertEqual(out['price'],12)
        self.assertTrue(any(m['value'] is None and m['key']=='breadth_up_pct' for m in out['metrics']))
        self.save(self.store.out/'sector-quotes.json',{'sectors':'broken'})
        self.assertEqual(self.store.instrument('sector','000685.SH',NOW)['price'],12)
        self.save(self.store.out/'sector-members/000686.SH.json',{'members':[{'code':'834683.NQ','name':'爹地宝贝'}]})
        self.store.sync_prices()
        self.assertEqual(self.store.instrument('stock','834683.NQ',NOW)['name'],'爹地宝贝')
        self.save(member,{'members':[{'code':'000001.SZ','name':'平安银行'}]})
        self.store.sync_prices()
        self.assertEqual(self.store.instrument('stock','000001.SZ',NOW)['name'],'平安银行')

    def test_source_close_ingestion_and_marked_coverage(self):
        p=self.root/'runtime/data/equity/daily/20261117.json.gz'
        p.parent.mkdir(parents=True)
        row={'ts_code':'000001.SZ','date':'2026-11-17','close':11,'pre_close':10,'pct_chg':10}
        with gzip.open(p,'wt') as f:
            json.dump({'rows':[row],'trade_date':'2026-11-17','complete':False,'row_count':1},f)
        self.store.sync_prices()
        self.store.sync_prices()
        packet=self.store.instrument('stock','000001.SZ',NOW)
        self.assertEqual((packet['quote_basis'],packet['quote_at'],packet['price']),('close','2026-11-17',11))
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT complete FROM coverage').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM bars WHERE kind='stock'").fetchone()[0],1)
