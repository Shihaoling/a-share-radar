import json,unittest
from datetime import datetime,timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from pipeline.news_content import publication_metadata,text_hash
from pipeline.minimax_news import PROMPT_VERSION
from pipeline.news_trust import apply_source_whitelist
from pipeline.sentiment import _is_rumor
from pipeline.primary_minutes import collect_primary_minutes,minute_coverage
from pipeline.news_backfill import run

NOW=datetime.fromisoformat('2026-11-18T00:20:00+08:00')
class BackfillTests(unittest.TestCase):
 def test_publication_is_labelled_source_time_not_url_or_body_date(self):
  html='<meta property="article:published_time" content="2026-11-16T19:31:00+08:00"><article>2026年11月17日 20:30 这不是发表时间。</article>'
  result=publication_metadata(html,'https://finance.eastmoney.com/a/2026111739.html',NOW)
  self.assertEqual(result['published_at'],'2026-11-16T19:31:00+08:00')
  unknown=publication_metadata('<article>2026年11月17日 20:30 事件</article>','https://example.test/20261117',NOW)
  self.assertNotIn('published_at',unknown)
  chinese=publication_metadata('<div class="time">2026年11月17日 09:30</div>','https://example.test',NOW)
  self.assertEqual(chinese['published_at'],'2026-11-17T09:30:00+08:00')
 def test_conflicting_and_future_publication_dates_are_not_invented(self):
  html='<meta property="article:published_time" content="2026-11-16T19:31:00+08:00"><div class="time">2026年11月17日 09:30</div>'
  self.assertEqual(publication_metadata(html,'https://example.test',NOW)['publication_verification']['status'],'conflict')
  self.assertNotIn('published_at',publication_metadata('<time datetime="2026-11-19T00:00:00+08:00"></time>','https://example.test',NOW))
 def test_cls_whitelist_does_not_promote_impostors_or_confirm_rumors(self):
  a={'provider':'stock_api','source_id':'stock_api:cls','verification':'unverified','title':'据传将收购企业'}
  b={'source':'财联社','title':'不受控来源冒充名称'}
  apply_source_whitelist({'news':[a,b]})
  self.assertEqual(a['credibility'],'reliable');self.assertTrue(a['source_verified']);self.assertTrue(_is_rumor(a));self.assertNotIn('source_verified',b)
 def test_one_minute_coverage_requires_every_trading_minute(self):
  def points(step):return [{'timestamp':f'2026-11-17T{m//60:02d}:{m%60:02d}:00+08:00'} for lo,hi in ((571,691),(781,901)) for m in range(lo,hi,step)]
  self.assertTrue(minute_coverage(points(1),'2026-11-17')['complete'])
  self.assertFalse(minute_coverage(points(5),'2026-11-17')['complete'])
  self.assertFalse(minute_coverage(points(1)[:-1],'2026-11-17')['complete'])
 def test_incomplete_one_minute_response_preserves_previous_five_minute_chart(self):
  old={'timestamp':'2026-11-17T15:00:00+08:00','provider':'stock_api','price':99,'frequency':'5min'}
  d={'market':[{'id':'sh000001'}],'intraday':{'sh000001':{'points':[old]}}}
  class Client:
   count=0
   def request(self,api,params):
    self.count+=1;self.params=params
    return [{'ts_code':'000001.SH','trade_time':'2026-11-17 09:35:00','close':100},{'ts_code':'000001.SH','trade_time':'2026-11-17 15:00:00','close':101}]
  c=Client();r=collect_primary_minutes(d,{'network_enabled':True,'primary_minutes':{'target_date':'2026-11-17','frequency':'1min','require_complete_session':True}},NOW,c)
  self.assertEqual(c.params['freq'],'1min');self.assertEqual(c.params['limit'],300)
  self.assertEqual(r['intraday']['sh000001']['points'],[old]);self.assertFalse(r['primary_minutes_state']['completed'])
 def test_worker_pins_requested_cohort_persists_progress_and_does_not_backdate_model_time(self):
  with TemporaryDirectory() as t:
   root=Path(t)
   for p in ['data/news_content','config','web/data']: (root/p).mkdir(parents=True)
   req={'request_id':'test','digest_date':'2026-11-17','articles':[{'id':'p','title':'新闻','url':'https://example.test/a','first_seen_at':'2026-11-17T20:00:00+08:00'}],'batch_size':8}
   (root/'data/news_content/backfill-request.json').write_text(json.dumps(req));(root/'config/settings.json').write_text('{}');(root/'web/data/dashboard.json').write_text('{"news":[{"id":"other"}]}')
   calls=[]
   def collector(d,cfg,*args):
    calls.append('fetch');self.assertEqual([n['id'] for n in d['news']],['p']);self.assertEqual(cfg['news_content']['request_interval_seconds'],20)
    d['news'][0].update(full_text='完整内容',full_text_status='complete',published_at='2026-11-16T09:00:00+08:00',publication_verification={'status':'verified'})
    return d
   def analyzer(d,cfg,*args):
    d['news'][0]['llm_analysis']={'status':'ready','model':'MiniMax-test','scored_at':NOW.isoformat(),'prompt_version':PROMPT_VERSION,'input_hash':text_hash('完整内容'),'summary':'已阅读全文并判断影响。','tone':'neutral','fact_status':'reported_fact','sector_impacts':[],'market_effect':{'direction':'unknown','confidence':0,'reason':'没有整体影响','evidence':[]},'uncertainties':[]};return d
   def digester(d,cfg,*args):
    self.assertEqual(cfg['news_digest']['target_date'],'2026-11-17');return {**d,'news_digest':{'status':'ready','date':'2026-11-17'}}
   with patch('pipeline.news_backfill.shanghai_now',return_value=NOW):
    s=run(root,NOW,collector,analyzer,digester);run(root,NOW+timedelta(minutes=1),collector,analyzer,digester)
   self.assertEqual(calls,['fetch']);self.assertEqual(s['body_complete'],1);self.assertEqual(s['model_complete'],1)
   d=json.loads((root/'web/data/dashboard.json').read_text());n=next(n for n in d['news'] if n['id']=='p')
   self.assertEqual(n['llm_analysis']['scored_at'],NOW.isoformat());self.assertEqual(n['published_at'],'2026-11-16T09:00:00+08:00')
