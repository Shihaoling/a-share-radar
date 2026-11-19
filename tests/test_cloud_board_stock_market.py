from contextlib import ExitStack, nullcontext
from datetime import datetime, timedelta
import gzip
from hashlib import sha256
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'deploy/tencent-preview')]
import relay_quotes as relay
from board_stock_market import StockMarketBatches, StockUniverse, StockBatchArchive

AT = datetime.fromisoformat('2026-11-19T13:21:11.123456+08:00')


def stocks(n):
    return {f'{i:06d}.SZ':f'{i:06d}.SZ' for i in range(1,n+1)}


def quote(code, now):
    return dict(id=code, price=9.25, previous_close=9, change_pct=(9.25/9-1)*100, provider='stock_api',
                quote_date=now.date().isoformat(), quote_timestamp=now.isoformat(timespec='seconds'),
                timestamp_quality='source_reported')


class MarketTests(unittest.TestCase):
    def test_source_dedup_unopened_board_and_bad_roster(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'names.json').write_text(json.dumps({'600000.SH':'a','999999.HK':'other market'}))
            roster = root/'members/885864.TI.json'
            roster.parent.mkdir()
            packet = dict(source='sxdata_sector_members',status='ready',code='885864.TI',
                          members=[{'code':c} for c in ['600000.SH','920002.BJ','invalid']])
            roster.write_text(json.dumps(packet))
            universe = StockUniverse(root/'names.json',roster.parent)
            universe.refresh(100)
            self.assertEqual(set(universe.codes),{'600000.SH','920002.BJ'})
            roster.write_text('{')
            universe.refresh(200)
            self.assertEqual(set(universe.codes),{'600000.SH','920002.BJ'})
            roster.write_text(json.dumps(dict(packet,members=[{'code':'600001.SH'}])))
            universe.refresh(300)
            self.assertEqual(set(universe.codes),{'600000.SH','600001.SH'})

    def test_wildcard_reuses_single_stock_slot_and_sweeps_missing_lower_frequency(self):
        worker = StockMarketBatches()
        pool = stocks(620)
        core = dict(list(pool.items())[:354])
        missing = set(list(pool)[-60:])
        primary = worker.choose(pool,core,set(),100)
        self.assertEqual(primary.selector,'3*.SZ,6*.SH,0*.SZ,9*.BJ')
        worker.unavailable(primary,'rate_limited',100)
        self.assertEqual(primary,worker.choose(pool,core,set(),160))
        worker.finish(primary,pool.keys()-missing,core,160)
        sweep = worker.choose(pool,core,set(),190)
        self.assertEqual(sweep.kind,'supplement')
        self.assertEqual(set(sweep.symbols),core.keys()|missing)
        worker.finish(sweep,core.keys(),core,190)
        self.assertEqual(worker.choose(pool,core,set(),220),primary)

    def test_only_verified_full_market_gets_longer_timeout_with_one_request(self):
        for api,selector,limit in [('rt_k',StockMarketBatches.full_selector,9),
                                   ('rt_k','6*.SH',5),('rt_k',None,5),('rt_idx_k','*.SH',5)]:
            client = Mock()
            client.request.return_value = []
            with patch.object(relay,'relay_client',return_value=nullcontext(client)) as session:
                with self.assertRaises(relay._Failure):
                    relay.collect('fixture',api,{'600000.SH':'600000.SH'},6,selector=selector)
            session.assert_called_once_with('fixture',timeout=limit,min_gap_seconds=6)
            client.request.assert_called_once_with(api,{'ts_code':selector or '600000.SH'})

    def test_truncated_venue_falls_back_to_bounded_exact_pages_without_starving_core(self):
        worker = StockMarketBatches()
        pool = stocks(900)
        core = dict(list(pool.items())[:50])
        primary = worker.choose(pool,core,set(),10)
        worker.finish(primary, set(core), core, 10)
        self.assertIsNone(worker.choose(pool,core,set(),20))
        worker.finish(None,set(core),core,20)
        shard = worker.choose(pool,core,set(),30)
        self.assertEqual(shard.selector,'0*.SZ')
        worker.finish(shard,set(list(pool)[:250]),core,30)
        seen = set()
        for stamp in range(40,200,20):
            self.assertIsNone(worker.choose(pool,core,set(),stamp))
            worker.finish(None,set(core),core,stamp)
            page = worker.choose(pool,core,set(),stamp+10)
            self.assertIsNone(page.selector)
            self.assertLessEqual(len(page.symbols),400)
            returned = set(list(page.symbols)[:250])
            worker.finish(page,returned,core,stamp+10)
            seen.update(returned)
        self.assertEqual(seen,set(pool))

    def test_archive_replays_distinct_source_times_and_collision_without_overwrite(self):
        with TemporaryDirectory() as folder:
            archive = StockBatchArchive(Path(folder))
            for at in [AT-timedelta(days=3), AT-timedelta(days=1), AT, AT]:
                archive.record([quote('600000.SH',at-timedelta(seconds=21))],at)
            self.assertFalse((Path(folder)/'2026-11-16').exists())
            self.assertTrue((Path(folder)/'2026-11-18').exists())
            files = sorted((Path(folder)/'2026-11-19').glob('*.json.gz'))
            self.assertEqual(len(files),2)
            obj = json.loads(gzip.decompress(files[0].read_bytes()))
            self.assertEqual(obj['received_at'],AT.isoformat())
            self.assertEqual(obj['stocks'][0]['previous_close'],9)
            self.assertEqual(obj['stocks'][0]['quote_timestamp'],(AT-timedelta(seconds=21)).isoformat(timespec='seconds'))

    def test_unopened_roster_updates_without_extra_api_or_expanded_public_feed(self):
        with TemporaryDirectory() as folder, ExitStack() as mocks:
            state = Path(folder)
            src = state/'view.json'
            public = state/'quote-output'
            pool = stocks(520)
            core = next(iter(pool))
            relay.publish(src,{'market':[],'sectors':[{'id':'801780.SI','stocks':[{'provider':'stock_api','ts_code':core}]}]})
            relay.publish(public/'sector-members/885864.TI.json', {
                'code':'885864.TI','source':'sxdata_sector_members','status':'ready','members':[{'code':c} for c in pool]})
            (state/'sxdata-token').write_text('fixture')
            mocks.enter_context(patch.dict('os.environ', {'CREDENTIALS_DIRECTORY':str(state),
                   'RADAR_EXPECTED_STOCK_KEY_SHA256':sha256(b'fixture').hexdigest()}))
            mocks.enter_context(patch.multiple(relay,STATE=state,SOURCE=src))
            mocks.enter_context(patch('market_daily.read_daily',return_value={}))
            mocks.enter_context(patch('market_daily.merge_daily'))
            mocks.enter_context(patch.object(relay,'shanghai_now',return_value=AT))
            mocks.enter_context(patch.object(relay,'restore_gate_cooldowns'))
            mocks.enter_context(patch.object(relay.QuoteSchedule,'admit',side_effect=['rt_k']))
            collect = mocks.enter_context(patch.object(relay,'collect',return_value=[quote(c,AT) for c in pool]))
            mocks.enter_context(patch.object(relay.time,'sleep',side_effect=StopIteration))
            with self.assertRaises(StopIteration):
                relay.start(SimpleNamespace(diagnostic=False,close_snapshot=False))
            collect.assert_called_once()
            self.assertEqual(collect.call_args.kwargs['selector'],'3*.SZ,6*.SH,0*.SZ,9*.BJ')
            self.assertEqual(len(json.loads((public/'board-stock-quotes.json').read_bytes())['stocks']),520)
            self.assertEqual([r['id'] for r in json.loads((public/'stock-quotes.json').read_bytes())['stocks']],[core])
            self.assertEqual(len(list((state/'asset-research/stock-batches').glob('*/*.json.gz'))),1)
