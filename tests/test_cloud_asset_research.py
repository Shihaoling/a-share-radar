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

    def test_reported_index_previous_close_fills_only_prior_session_and_yields_to_daily(self):
        day = '2026-11-19'
        row = {**self.q, 'id':'sh000300', 'price':3984.487, 'previous_close':3983.7667,
               'provider':'stock_api', 'timestamp_quality':'source_reported',
               'quote_timestamp':day+'T14:57:03+08:00'}
        available = day+'T15:03:00+08:00'
        packet = {'schema_version':1, 'source':'sxdata_relay', 'trade_date':day,
                  'generated_at':available, 'market':[None, row]}
        self.save(self.store.out/'live-quotes.json', packet)
        now = datetime.fromisoformat(available)
        self.store.sync_prices(now)
        def bars():
            with self.store.db() as db:
                return [tuple(r) for r in db.execute('SELECT day,close,source FROM bars ORDER BY day')]
        self.assertEqual(bars(), [('2026-11-17',4000,'saved_daily'),
                                  ('2026-11-18',3983.7667,'sxdata_rt_idx_k.pre_close')])
        with self.store.db() as db:
            evidence = db.execute('SELECT * FROM index_close_evidence').fetchone()
            self.assertEqual(tuple(evidence), ('000300.SH','2026-11-18',3983.7667,'sxdata_rt_idx_k.pre_close',
                                               row['quote_timestamp'],available))

        self.save(self.store.out/'daily-market.json', {'bars':{'sh000300':{'2026-11-18':
            {'date':'2026-11-18','close':3983.77}}}})
        self.store.sync_prices(now)
        self.save(self.store.out/'live-quotes.json', {**packet,'market':[{**row,'previous_close':3983.78}]})
        self.store.sync_prices(now)
        self.assertEqual(bars()[-1], ('2026-11-18',3983.77,'sxdata_daily'))

    def test_index_previous_close_uses_verified_calendar_and_market_source(self):
        # First session following the National Day closure.
        day = '2026-10-08'
        at = day+'T09:35:00+08:00'
        now = datetime.fromisoformat(at)
        row = {**self.q, 'id':'sh000300', 'provider':'stock_api', 'timestamp_quality':'source_reported',
               'quote_date':day, 'quote_timestamp':at}
        packet = {'schema_version':1,'source':'sxdata_relay','trade_date':day,'generated_at':at,'market':[row]}
        invalid = [
            {**packet,'source':'estimate'}, {**packet,'trade_date':'2027-10-08'}, {**packet,'trade_date':'2026-10-07'},
            {**packet,'generated_at':day+'T09:40:00+08:00'}, {**packet,'generated_at':day+'T09:30:00+08:00'},
            *({**packet,'market':[{**row,**changed}]} for changed in (
                {'id':'000300.SH'}, {'id':'sh000001'}, {'kind':'etf'}, {'provider':'other'},
                {'timestamp_quality':'estimated'}, {'quote_date':'2026-09-30'},
                {'quote_timestamp':'2026-09-30T15:00:00+08:00'}, {'quote_timestamp':day+'T09:30:00'},
                {'previous_close':None}, {'previous_close':float('inf')}, {'previous_close':0}))]
        path = self.store.out/'live-quotes.json'
        for bad in invalid:
            with self.subTest(packet=bad):
                self.save(path, bad)
                self.store.sync_prices(now)
                with self.store.db() as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM index_close_evidence').fetchone()[0], 0)
        self.save(path, packet)
        self.store.sync_prices(now)
        with self.store.db() as db:
            self.assertEqual(tuple(db.execute('SELECT day,close FROM index_close_evidence').fetchone()), ('2026-09-30',10))
