from contextlib import closing
from datetime import datetime, timedelta
import gzip
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT),str(ROOT/'deploy/tencent-preview')]
from asset_research import ResearchStore

NOW = datetime.fromisoformat('2026-11-19T14:12:00+08:00')
DAY = NOW.date().isoformat()
THS, SW = '885000.TI', '801001.SI'


def save(path, data):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data))


def stock(i, at, pct, base=10):
    return dict(id=f'{i:06d}.SZ',price=base*(1+pct/100),previous_close=base,change_pct=pct,
                quote_date=at.date().isoformat(),quote_timestamp=at.isoformat(),provider='stock_api',
                timestamp_quality='source_reported')


class StandaloneBoardHistory(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state=Path(self.tmp.name)
        self.public=self.state/'public'
        save(self.public/'ths-sector-catalog.json',{'items':[dict(code=THS,name='A')], 'sw_items':[dict(code=SW,name='B')]})
        self.store=ResearchStore(self.state,self.public,self.state/'runtime')
        for code in (THS, SW):
            save(self.store.out/'sector-members'/(code+'.json'),
                 dict(code=code,status='ready',members=[{'code':f'{i:06d}.SZ'} for i in range(5)]))

    def write_source(self, rows):
        save(self.store.out/'board-stock-quotes.json',dict(source='sxdata_relay',stocks=rows))

    def archive(self, at, rows):
        path=self.store.private/'stock-batches'/DAY/(at.strftime('%H%M%S%f')+'.json.gz')
        path.parent.mkdir(parents=True,exist_ok=True)
        with gzip.open(path,'wt') as f:
            json.dump(dict(schema_version=1,source='sxdata_relay',received_at=at.isoformat(),
                           trade_date=DAY,stocks=rows),f)
        return path

    def sql(self, query):
        with closing(self.store.db()) as db:
            return [tuple(row) for row in db.execute(query)]

    def test_first_visit_after_two_real_batches_already_has_both_minutes(self):
        for offset in (0,5):
            at=NOW+timedelta(minutes=offset)
            self.write_source([stock(i,at,i+offset) for i in range(5)])
            self.store.capture_members(at)
        self.assertEqual(self.sql('SELECT count(*) FROM watched'),[(0,)])
        self.assertEqual(self.sql('SELECT code,count(*) FROM constituent_minutes GROUP BY code'),[(SW,2),(THS,2)])
        public=json.loads((self.store.out/'board-live-summary.json').read_bytes())
        self.assertEqual(public['items'][SW]['quoted_count'],5)
        self.assertEqual(public['items'][THS]['change_pct'],7)
        for board in (THS,SW):
            data=self.store.instrument('sector',board,at)['constituent_live']
            self.assertEqual(data['points'],[[NOW.isoformat(),2],[at.isoformat(),7]])

    def test_legacy_prices_only_rebuilt_with_actual_same_day_confirmed_base(self):
        old=NOW-timedelta(minutes=40)
        with closing(self.store.db()) as db, db:
            for i in range(5):
                db.execute('INSERT INTO chart_points VALUES (?,?,?,?,?,?)',
                           ('stock',f'{i:06d}.SZ',DAY,old.isoformat()[:16],old.isoformat(),11))
        rows=[stock(i,NOW,1) for i in range(5)]
        rows[4]['previous_close']=100 # Inconsistent with the source close/change, ineligible for history.
        self.write_source(rows)
        self.store.capture_members(NOW)
        history=self.sql("SELECT change,quoted,rebuilt FROM constituent_minutes WHERE code='885000.TI' ORDER BY minute")
        self.assertEqual(len(history),2)
        self.assertAlmostEqual(history[0][0],10)
        self.assertEqual(history[0][1:],(4,1))
        self.assertEqual(history[1],(1,5,0))
        store=ResearchStore(self.state,self.public,self.state/'runtime')
        store.capture_members(NOW)
        self.assertEqual(self.sql('SELECT count(*) FROM constituent_minutes'),[(4,)])

    def test_recover_missed_archive_deduplicate_and_filter_source_time(self):
        first=NOW-timedelta(minutes=30)
        last=NOW-timedelta(minutes=25)
        self.archive(first,[stock(i,first,i) for i in range(4)]+[stock(4,first+timedelta(seconds=1),100)])
        self.archive(last,[stock(i,last,-i) for i in range(5)])
        self.write_source([])
        self.store.boards.capture(NOW,replay_limit=30)
        self.assertEqual(self.sql('SELECT count(*) FROM watched'),[(0,)])
        self.assertEqual(self.sql('SELECT count(*) FROM board_source_files'),[(2,)])
        self.assertEqual(self.sql('SELECT count(*) FROM board_stock_minutes'),[(9,)])
        saved=self.store.instrument('sector',THS,NOW)['constituent_live']
        self.assertEqual(saved['points'],[[first.isoformat(),1.5],[last.isoformat(),-2]])
        self.assertEqual(saved['status'],'stale')
        self.store.boards.capture(NOW+timedelta(seconds=20))
        self.assertEqual(self.sql('SELECT count(*) FROM board_stock_minutes'),[(9,)])

    def test_archive_with_two_source_minutes_in_same_response_keeps_both(self):
        earlier = NOW-timedelta(minutes=3)
        self.archive(NOW,[stock(i,earlier,4) for i in range(5)]+[stock(i,NOW,6) for i in range(5)])
        self.store.boards.capture(NOW,replay_limit=20)
        expected = [[earlier.isoformat(),4],[NOW.isoformat(),6]]
        self.assertEqual(self.store.instrument('sector',THS,NOW)['constituent_live']['points'],expected)

    def test_strict_window_and_partial_rosters_or_missing_real_minute(self):
        far=NOW-timedelta(seconds=181)
        self.archive(NOW,[stock(0,NOW,1),stock(1,NOW,1),stock(2,NOW,1),stock(3,far,1),stock(4,far,1)])
        self.store.boards.capture(NOW,replay_limit=20)
        self.assertFalse(self.sql('SELECT * FROM constituent_minutes'))
        start=NOW+timedelta(minutes=2)
        self.write_source([stock(i,start,-i) for i in range(5)])
        save(self.store.out/'sector-members'/(SW+'.json'),dict(code=SW,status='partial',members=[{'code':'000001.SZ'}]))
        self.store.boards.capture(start)
        self.assertEqual(self.sql('SELECT DISTINCT code FROM constituent_minutes'),[(THS,)])
        tomorrow=NOW+timedelta(days=1)
        self.store.capture_members(tomorrow)
        self.assertEqual(json.loads((self.store.out/'board-live-summary.json').read_bytes())['items'],{})
        self.assertFalse(self.sql('SELECT * FROM board_stock_minutes'))

    def test_partial_series_with_coverage_and_full_series_stay_separate(self):
        save(self.store.out/'sector-members'/(SW+'.json'),dict(code=SW,status='partial',expected_count=8,
             members=[{'code':'000000.SZ'},{'code':'000001.SZ'}]))
        self.write_source([stock(0,NOW,3)])
        self.store.capture_members(NOW)
        self.assertEqual(self.sql('SELECT count(*) FROM watched'),[(0,)])
        self.assertEqual(json.loads((self.store.out/'board-live-summary.json').read_bytes())['items'],{})
        for code,total in ((THS,5),(SW,8)):
            first=self.store.instrument('sector',code,NOW)['constituent_live']
            self.assertEqual(first['points'],[])
            self.assertEqual(first['sample_points'],[[NOW.isoformat(),3,1,total]])
            self.assertIsNone(first['change_pct'])
        at=NOW+timedelta(minutes=5)
        self.write_source([stock(i,at,-i) for i in range(5)])
        self.store.capture_members(at)
        self.assertEqual(self.sql('SELECT DISTINCT code FROM board_partial_minutes'),[(SW,)])
        good=self.store.instrument('sector',THS,at)['constituent_live']
        self.assertEqual(good['points'],[[at.isoformat(),-2]])
        sample=self.store.instrument('sector',SW,at)['constituent_live']
        self.assertEqual(sample['sample_points'][-1],[at.isoformat(),-.5,2,8])
        self.assertEqual(sample['total_count'],8)
        self.assertEqual(set(json.loads((self.store.out/'board-live-summary.json').read_bytes())['items']),{THS})

    def test_migrate_existing_contribution_rows_without_touching_their_values(self):
        state=self.state/'legacy'
        (state/'asset-research').mkdir(parents=True)
        old_db=state/'asset-research'/'research.sqlite3'
        with closing(sqlite3.connect(old_db)) as db, db:
            db.execute('CREATE TABLE constituent_minutes(code TEXT,day TEXT,minute TEXT,stamp TEXT,oldest TEXT,change REAL,quoted INTEGER,total INTEGER,PRIMARY KEY(code,day,minute)) WITHOUT ROWID')
            db.execute('INSERT INTO constituent_minutes VALUES(?,?,?,?,?,?,?,?)',(THS,DAY,NOW.isoformat()[:16],NOW.isoformat(),NOW.isoformat(),1.23,42,42))
        new=ResearchStore(state,self.public,state/'runtime')
        new.capture_members(NOW)
        self.assertEqual(json.loads((new.out/'board-live-summary.json').read_bytes())['items'][THS]['change_pct'],1.23)
