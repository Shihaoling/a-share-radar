from datetime import datetime,timedelta
import hashlib,json,os,unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch,Mock
from pipeline.data_service import token_for_api,credential_matches,gate_delay,failed,ENDPOINT
from pipeline.stock_api import _Client,_Failure,_rows,_news_records
from pipeline.news_content import enrich_full_text
from pipeline.source_policy import apply_primary_news_policy
from pipeline.news_digest import digest_inputs
from pipeline.publish import publish_view
from pipeline.sources import fetch_data

NOW=datetime.fromisoformat('2026-11-17T13:00:00+08:00')
class DataServiceTests(unittest.TestCase):
 def test_channels_and_credential_destination_guard(self):
  key='synthetic-channel-key'
  self.assertEqual(token_for_api(key,'daily'),'backup15000_'+key)
  self.assertEqual(token_for_api(key,'idx_mins'),key)
  self.assertEqual(token_for_api('backup15000_'+key,'news'),key)
  with patch.dict(os.environ,{'RADAR_EXPECTED_STOCK_KEY_SHA256':hashlib.sha256(key.encode()).hexdigest()}):
   self.assertTrue(credential_matches(key));self.assertTrue(credential_matches(' \n'+key+'\n'));self.assertFalse(credential_matches('old-key'))
   with patch('pipeline.stock_api.urllib.request.build_opener') as opener:
    with self.assertRaises(_Failure):_Client('old-key',{},NOW)
    opener.assert_not_called()
 def test_body_429_stops_other_calls_and_records_no_secret(self):
  with TemporaryDirectory() as d,patch.dict(os.environ,{'RADAR_SERVICE_GATE_FILE':str(Path(d)/'gate.json')}):
   with self.assertRaises(_Failure) as caught:_rows({'code':429,'msg':'upstream busy'},'index_daily')
   self.assertEqual(caught.exception.code,'rate_limited');self.assertGreaterEqual(caught.exception.retry_seconds,60)
   failed('index_daily',60,host=True)
   self.assertGreater(gate_delay('news')[0],59)
   self.assertNotIn('upstream busy',Path(d,'gate.json').read_text())
 def test_primary_news_text_is_not_truncated_or_refetched_as_html(self):
  text='保留原始资讯正文'*3000+'最后一段'
  record=_news_records([{'datetime':NOW.isoformat(),'title':'快讯标题','content':text,'channels':''}],NOW,NOW.isoformat(),'fake')[0]
  record['id']='api-news'
  self.assertEqual(record['full_text'],text)
  with TemporaryDirectory() as d:
   gate=Mock();gate.get.side_effect=AssertionError('API text must not be crawled again')
   result=enrich_full_text({'news':[record]},{'network_enabled':True},NOW,Path(d)/'cache',gate)
   self.assertEqual(result['news'][0]['full_text_status'],'complete');gate.get.assert_not_called()
   view=publish_view(result,Path(d)/'view.json')
   body=json.loads((Path(d)/'articles'/Path(view['news'][0]['article_text_url']).name).read_text())
   self.assertEqual(body['full_text'],text)
 def test_secondary_is_hidden_only_in_primary_covered_window(self):
  rows=[{'id':'p','provider':'stock_api','published_at':'2026-11-17T10:00:00+08:00'},
        {'id':'covered','published_at':'2026-11-17T10:00:00+08:00'},
        {'id':'gap','published_at':'2026-11-17T12:30:00+08:00'}]
  state={'records':[{}],'coverage_start':'2026-11-17T00:00:00+08:00','coverage_end':'2026-11-17T12:00:00+08:00','fetched_at':NOW.isoformat()}
  d={'stock_api_state':{'news':state},'news':rows,'news_digest':{'summary':'旧备用来源综述','article_ids':['covered']}}
  apply_primary_news_policy(d,NOW)
  self.assertNotIn('summary',d['news_digest'])
  self.assertEqual([n['primary_shadowed'] for n in rows],[False,True,False])
  self.assertEqual({n['id'] for n in digest_inputs(rows,'2026-11-17')},{'p','gap'})
  with TemporaryDirectory() as t:
   view=publish_view(d,Path(t)/'view.json');self.assertEqual({n['id'] for n in view['news']},{'p','gap'})
  apply_primary_news_policy(d,NOW+timedelta(hours=2))
  self.assertTrue(rows[1]['primary_shadowed'])
  state['records']=[];apply_primary_news_policy(d,NOW)
  self.assertFalse(any(n['primary_shadowed'] for n in rows))
 def test_minute_cooldown_survives_normal_source_refresh(self):
  d={'primary_minutes_state':{'service_endpoint':ENDPOINT,'endpoints':{'sw_mins':{'cooldown_until':(NOW+timedelta(hours=1)).isoformat()}}},'news':[]}
  out=fetch_data(d,{'network_enabled':False,'primary_provider':'stock_api'},NOW)
  self.assertEqual(out['primary_minutes_state'],d['primary_minutes_state'])

class ServiceInventoryTests(unittest.TestCase):
 def test_capability_inventory_is_bounded_and_resumes_without_duplicate_requests(self):
  from pipeline.service_rollout import run
  class Client:
   def __init__(self):self.calls=[]
   def request(self,api,params):
    self.calls.append(api)
    return [{'ts_code':params.get('ts_code'),'trade_date':params.get('trade_date'),'end_date':params.get('period'),'date':params.get('date'),'month':params.get('m'),'quarter':params.get('q'),'nav_date':params.get('nav_date')}]
  c=Client()
  with TemporaryDirectory() as d:
   first=run(d,NOW,c);self.assertEqual(len(c.calls),6)
   second=run(d,NOW,c);self.assertEqual(len(c.calls),12)
   self.assertEqual(len(c.calls),len(set(c.calls)));self.assertEqual(second['cursor'],12)
 def test_mismatched_old_secret_is_not_sent_to_new_service(self):
  from pipeline.service_rollout import run
  with TemporaryDirectory() as d,patch.dict(os.environ,{'STOCK_API_TOKEN':'old-private-fixture','RADAR_EXPECTED_STOCK_KEY_SHA256':'0'*64}),patch('pipeline.service_rollout._Client') as client:
   result=run(d,NOW)
   self.assertEqual(result['status'],'needs_token_update');client.assert_not_called()
   self.assertNotIn('old-private-fixture',Path(d,'web/data/data-service-capabilities.json').read_text())

class MinuteCompensationTests(unittest.TestCase):
 def test_only_one_sixty_second_compensation_per_endpoint_per_day(self):
  from pipeline.primary_minutes import collect_primary_minutes
  class Client:
   def __init__(self):self.count=0
   def remaining(self):return 500
   def request(self,api,params):
    self.count+=1
    if self.count!=2:raise _Failure('rate_limited',60)
    return [{'ts_code':params['ts_code'],'trade_time':NOW.date().isoformat()+' '+t,'close':100+i} for i,t in enumerate(['13:00:00','15:00:00'])]
  d={'market':[{'id':'sh000001'}]};c=Client()
  with patch('pipeline.primary_minutes.time.sleep') as sleep:
   first=collect_primary_minutes(d,{'network_enabled':True},NOW.replace(hour=22),c)
   self.assertEqual(c.count,2);sleep.assert_called_once_with(60)
   self.assertEqual(len(first['primary_minutes_state']['completed']),1)
   first['market'].append({'id':'sh000300'})
   again=collect_primary_minutes(first,{'network_enabled':True},NOW.replace(hour=22),c)
   self.assertEqual(c.count,3);sleep.assert_called_once_with(60)
