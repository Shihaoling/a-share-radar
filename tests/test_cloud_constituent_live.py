import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'deploy/tencent-preview')]
import constituent_live as live
from asset_research import ResearchStore

NOW = datetime.fromisoformat('2026-11-19T13:05:30+08:00')
BOARD = '885864.TI'
CODES = [f'{i:06d}.SZ' for i in range(1, 6)]
ROSTER = dict(status='ready', members=[{'code':c} for c in CODES])


def quotes(now):
    return [dict(id=c, provider='stock_api', timestamp_quality='source_reported', price=10.2,
                 change_pct=i, quote_timestamp=(now-timedelta(seconds=i)).isoformat(),
                 quote_date=now.date().isoformat()) for i,c in enumerate(CODES)]


class BoardQuoteTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute(live.SCHEMA)

    def snapshot(self, rows, now=NOW, roster=ROSTER):
        return live.summarize(self.db, BOARD, roster, rows, now)

    def test_equal_weights_threshold_source_date_and_unique_members(self):
        rows = quotes(NOW)
        for new in (dict(quote_timestamp=(NOW+timedelta(seconds=1)).isoformat()),
                    dict(quote_timestamp=(NOW-timedelta(seconds=181)).isoformat()), dict(quote_date='2026-11-18'),
                    dict(quote_timestamp='2026-11-19T13:05:30'),dict(price=0),dict(change_pct=float('nan')),
                    dict(provider='daily'),dict(timestamp_quality='daily_close'),dict(id=['invalid'])):
            result = self.snapshot([rows[0], rows[0], rows[1], rows[2], dict(rows[3], **new)])
            self.assertEqual((result['status'],result['quoted_count'],result['change_pct']), ('partial',3,None))
            self.assertFalse(result['points'])
        output = self.snapshot(rows[:4]+[rows[3]])
        self.assertEqual((output['change_pct'],output['quoted_count'],output['required_count']), (1.5,4,4))
        self.assertEqual(output['oldest_at'], rows[3]['quote_timestamp'])
        self.assertEqual(output['quote_at'], rows[0]['quote_timestamp'])
        self.assertEqual(output['points'], [[rows[0]['quote_timestamp'],1.5]])
        self.assertEqual(len(output['members']),4)

    def test_session_boundaries_match_the_chart_axis(self):
        for clock, ok in [('09:25:00',False),('09:30:10',True),('11:30:01',False),
                          ('13:00:10',True),('15:00:00',True),('15:00:01',False)]:
            at=datetime.fromisoformat('2026-11-19T'+clock+'+08:00')
            rows = [dict(r,quote_timestamp=at.isoformat()) for r in quotes(at)]
            self.assertEqual(len(live.observed_members(ROSTER,rows,at)[1]),5 if ok else 0)

    def test_stale_recovery_and_no_fabricated_minutes(self):
        self.snapshot(quotes(NOW))
        later = NOW+timedelta(minutes=4)
        stale = self.snapshot(quotes(NOW),later)
        self.assertEqual((stale['change_pct'],stale['status'],stale['current_quoted_count']), (2,'stale',0))
        self.assertEqual(stale['quote_at'], NOW.isoformat())
        current = self.snapshot(quotes(later),later)
        self.assertEqual(current['status'],'ready')
        self.assertEqual([p[0] for p in current['points']], [NOW.isoformat(),later.isoformat()])
        next_day = self.snapshot([],NOW+timedelta(days=1))
        self.assertEqual((next_day['status'],next_day['trade_date']),('stale','2026-11-19'))

    def test_partial_roster_and_unsupported_members_do_not_overstate_coverage(self):
        roster = dict(ROSTER,status='partial')
        self.assertIsNone(self.snapshot(quotes(NOW),roster=roster)['change_pct'])
        roster = dict(ROSTER,members=ROSTER['members']+[{'code':'12345.HK'},{'code':'12346.HK'}])
        result = self.snapshot(quotes(NOW),roster=roster)
        self.assertEqual((result['status'],result['total_count'],result['required_count']),('partial',7,6))

    def test_opening_a_board_registers_demand_and_background_keeps_real_samples(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            public = state/'public'
            public.mkdir()
            (public/'ths-sector-catalog.json').write_text(json.dumps({'items':[dict(code=BOARD,name='光刻胶')]}))
            store = ResearchStore(state,public,state/'runtime')
            members = store.out/'sector-members'
            members.mkdir(parents=True)
            (members/(BOARD+'.json')).write_text(json.dumps(ROSTER))
            self.assertIsNone(store.instrument('sector','999999.TI',NOW))
            first = store.instrument('sector', BOARD,NOW)
            self.assertEqual(first['constituent_live']['status'],'waiting')
            self.assertEqual(json.loads((store.private/'board-watch.json').read_bytes())['sectors'][0][0],BOARD)
            (store.out/'stock-quotes.json').write_text(json.dumps({'stocks':quotes(NOW)}))
            store.capture_members(NOW)
            with closing(store.db()) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM constituent_minutes').fetchone()[0],1)
            actual = store.instrument('sector',BOARD,NOW)['constituent_live']
            self.assertEqual((actual['status'],actual['quoted_count'],actual['change_pct']),('ready',5,2))
