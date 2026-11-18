from copy import deepcopy
from datetime import datetime, timedelta
import gzip
import json
import io
import urllib.error
from urllib.response import addinfourl
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pipeline.news_content import extract_article, enrich_full_text, text_hash
from pipeline.minimax_news import validate_analysis, enrich_minimax, PROMPT_VERSION, DEFAULT_MODEL, ModelFailure, system_prompt, infer_article, evidence_segments
from pipeline.news_archive import archive_news
from pipeline.sentiment import aggregate_entity_sentiment
from pipeline.publish import publish_view
from pipeline.primary_minutes import collect_primary_minutes, minute_points
from pipeline.direction_probability import news_features

NOW = datetime.fromisoformat('2026-11-17T20:10:00+08:00')
BODY = '国际原油价格上涨，增加石油开采企业的销售收入，但航空公司燃油成本上升。' * 80


def analysis():
    def effect(direction, reason, evidence):
        return dict(direction=direction, confidence=.8, reason=reason, evidence=evidence)
    return dict(summary='原油涨价改善上游销售收入，同时增加航空公司的燃油成本，行业影响分化。', tone='mixed', fact_status='reported_fact',
        market_effect=effect('unknown', '没有充分的整体市场证据', []),
        sector_impacts=[dict(sector_code='801960.SI', sector_name='石油石化', horizon='next_session', **effect('positive', '价格上升改善开采业务收入，炼化业务影响仍有分歧', ['增加石油开采企业的销售收入'])),
                        dict(sector_code='801170.SI', sector_name='交通运输', horizon='next_session', **effect('negative', '航空燃油成本上升，不能推广到全部运输业务', ['航空公司燃油成本上升']))], uncertainties=['不等于相关股票会上涨或下跌'])


class FullTextTests(unittest.TestCase):
    def test_entire_article_tail_and_archive_are_preserved_but_browser_stays_small(self):
        tail='最后一段关键限制：政策尚未正式实施。'
        text, method=extract_article(('<html><nav>菜单</nav><article>'+BODY+'<p>'+tail+'</p></article><footer>广告</footer></html>').encode())
        self.assertIn(tail,text);self.assertNotIn('菜单',text);self.assertNotIn('广告',text)
        n={'id':'a','title':'原油影响','full_text':text,'full_text_hash':text_hash(text)}
        with TemporaryDirectory() as d:
            archive_news({'news':[n]},NOW,Path(d)/'archive')
            saved=json.loads(gzip.decompress((Path(d)/'archive/20261117.json.gz').read_bytes()))
            self.assertEqual(next(iter(saved['articles'].values()))['full_text'],text)
            view=publish_view({'news':[n]},Path(d)/'view.json');self.assertNotIn('full_text',view['news'][0])

    def test_quote_layout_whitespace_is_normalized_without_allowing_paraphrase(self):
        a=analysis();split_body=BODY.replace('航空公司燃油成本上升','航空公司\n燃油成本\n上升')
        result=validate_analysis(a,split_body)
        self.assertIn('航空公司\n燃油成本\n上升',result['sector_impacts'][1]['evidence'])
        a['sector_impacts'][1]['evidence']=['航空公司燃油成本下降']
        with self.assertRaises(ValueError):validate_analysis(a,split_body)

    def test_challenge_and_paywall_are_not_article_bodies(self):
        for html in ('<article>安全验证'+BODY+'</article>', '<div class="paywall">订阅</div><article>'+BODY+'</article>'):
            with self.assertRaises(Exception):extract_article(html.encode())

    def test_legacy_eastmoney_http_links_are_upgraded_before_collection(self):
        class Gate:
            def __init__(self): self.urls=[]
            def get(self,url): self.urls.append(url); return ('<article>'+BODY+'</article>').encode(),{}
        gate=Gate();data={'news':[{'id':'a','url':'http://finance.eastmoney.com/a/test.html'}]}
        with TemporaryDirectory() as d:
            result=enrich_full_text(data,{'network_enabled':True},NOW,d,gate)
            self.assertEqual(gate.urls,['https://finance.eastmoney.com/a/test.html'])
            self.assertEqual(result['news'][0]['original_url'],'http://finance.eastmoney.com/a/test.html')
            self.assertEqual(result['news'][0]['full_text_status'],'complete')

    def test_cached_body_never_requests_source_twice_and_host_failures_cool(self):
        class Gate:
            def __init__(self):self.calls=[]
            def get(self,url):self.calls.append(url);return ('<article>'+BODY+'</article>').encode(),{}
        gate=Gate();data={'news':[{'id':'a','url':'https://news.example/a'}]}
        with TemporaryDirectory() as d:
            cfg={'network_enabled':True}
            first=enrich_full_text(data,cfg,NOW,d,gate);second=enrich_full_text(data,cfg,NOW,d,gate)
            self.assertEqual(len(gate.calls),1);self.assertEqual(first['news'][0]['full_text'],second['news'][0]['full_text'])
        class BadGate:
            def __init__(self):self.calls=0
            def get(self,url):self.calls+=1;raise TimeoutError()
        bad=BadGate()
        with TemporaryDirectory() as d:
            enrich_full_text({'news':[{'id':'a','url':'https://news.example/a'},{'id':'b','url':'https://news.example/b'}]},cfg,NOW,d,bad)
            enrich_full_text(data,cfg,NOW+timedelta(minutes=30),d,bad)
            self.assertEqual(bad.calls,1)


class MiniMaxTests(unittest.TestCase):
    def test_redirect_defers_both_workers_without_following_or_sending_another_request(self):
        response = addinfourl(io.BytesIO(), {"Location": "/maintenance"}, "https://example.invalid/chat", 302)
        response.msg = "Found"
        with TemporaryDirectory() as d, patch.dict("os.environ", {"RADAR_MINIMAX_COOLDOWN_DB": str(Path(d)/"gate.sqlite3")}), \
                patch("urllib.request.HTTPSHandler.https_open", return_value=response) as send:
            with self.assertRaises(ModelFailure) as first:
                infer_article({"full_text": BODY}, {}, "test-key")
            self.assertEqual((first.exception.code, first.exception.retry), ("redirect_blocked", 3600))
            with self.assertRaises(ModelFailure) as second:
                infer_article({"full_text": BODY}, {}, "test-key")
            self.assertEqual(second.exception.code, "shared_model_cooldown")
            send.assert_called_once()

    def test_digest_payload_preserves_article_boundaries_and_short_ids(self):
        articles=[{'id':'N0001','summary':BODY},{'id':'N0002','summary':'最后一条来源全文'}]
        requests=[]
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'{"summary":"来源文本完整保留"}'}}]}).encode()
        class Opener:
            def open(self,request,**kwargs):requests.append(json.loads(request.data));return Response()
        with patch('pipeline.minimax_news.urllib.request.build_opener',return_value=Opener()):
            infer_article({'full_text':json.dumps(articles),'digest_articles':articles},{},'fake',prompt='综合全文',validator=lambda value:value)
        sent=json.loads(requests[0]['messages'][1]['content'])
        self.assertEqual(sent['articles'],articles)
        self.assertNotIn('article_segments',sent)

    def test_reference_payload_is_lossless_and_only_source_ids_become_quotes(self):
        body = BODY + '\n末段：不能忽略的否定条件。\n'
        spans = evidence_segments(body)
        self.assertEqual(''.join(spans.values()), body)
        self.assertTrue(all(len(s) <= 240 for s in spans.values()))
        answer = analysis()
        for row in answer['sector_impacts']: row['evidence'] = ['E0001']
        requests = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, *args): return json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps(answer,ensure_ascii=False)}}]}).encode()
        class Opener:
            def open(self, request, **kwargs): requests.append(json.loads(request.data)); return Response()
        with patch('pipeline.minimax_news.urllib.request.build_opener',return_value=Opener()):
            result = infer_article({'full_text':body},{},'fake-key')
            self.assertEqual(requests[0]['max_completion_tokens'],32768)
            sent = json.loads(requests[0]['messages'][1]['content'])
            self.assertEqual(''.join(s['text'] for s in sent['article_segments']), body)
            self.assertEqual(result['sector_impacts'][0]['evidence'], [spans['E0001']])
            answer['sector_impacts'][0]['evidence'] = ['E9999']
            with self.assertRaises(ModelFailure): infer_article({'full_text':body},{},'fake-key')

    def test_verified_previous_prompt_cache_is_reused_without_an_api_call(self):
        version='fulltext-sw31-impact-v1'
        n={'id':'a','title':'原油影响','full_text':BODY,'full_text_status':'complete'}
        key=text_hash(json.dumps([text_hash(BODY),n['title'],DEFAULT_MODEL,version],ensure_ascii=False))
        row={**analysis(),'status':'ready','model':DEFAULT_MODEL,'prompt_version':version,'input_hash':text_hash(BODY),'scored_at':NOW.isoformat()}
        with TemporaryDirectory() as d,patch.dict('os.environ',{'MINIMAX_API_KEY':'fake'}):
            Path(d,'minimax-cache.json').write_text(json.dumps({'articles':{key:row}}))
            result=enrich_minimax({'news':[n]},{'network_enabled':True},NOW,d,lambda *args:self.fail('verified cached result must be reused'))
            self.assertEqual(result['minimax_summary']['reused'],1)
            self.assertEqual(result['news'][0]['llm_analysis']['prompt_version'],version)

    def test_focused_cloud_check_only_infers_the_selected_article(self):
        rows=[{'id':str(i),'title':title,'full_text':BODY,'full_text_status':'complete'} for i,title in enumerate(['其他文章','财经早餐'])]
        calls=[]
        def infer(n,*args): calls.append(n['title']); return analysis()
        with TemporaryDirectory() as d,patch.dict('os.environ',{'MINIMAX_API_KEY':'fake'}),patch('pipeline.minimax_news.shanghai_now',return_value=NOW):
            result=enrich_minimax({'news':rows},{'network_enabled':True,'minimax':{'article_title_filter':'财经早餐','max_articles_per_run':1}},NOW,d,infer)
            self.assertEqual(calls,['财经早餐'])
            self.assertEqual(result['minimax_summary']['ready'],1)

    def test_api_error_diagnostics_never_retain_raw_messages_or_credentials(self):
        class Opener:
            def open(self,*args,**kwargs):
                raw=json.dumps({'base_resp':{'status_code':1008,'status_msg':'insufficient balance; sensitive-provider-detail'}}).encode()
                raise urllib.error.HTTPError('https://api.minimax.cn/v1/chat/completions',402,'payment',{},io.BytesIO(raw))
        with patch('pipeline.minimax_news.urllib.request.build_opener',return_value=Opener()):
            with self.assertRaises(ModelFailure) as error:infer_article({'full_text':BODY},{},'fake-key')
        self.assertEqual(error.exception.code,'http_402_provider_1008_insufficient_balance')
        self.assertNotIn('sensitive',str(error.exception))
        self.assertNotIn('fake-key',str(error.exception))

    def test_schema_rejects_hallucinated_quotes_and_wrong_industry(self):
        self.assertEqual(len(validate_analysis(analysis(),BODY)['sector_impacts']),2)
        for field,value in [('evidence',['原文没有这句话']),('sector_name','银行'),('confidence',True)]:
            a=analysis();a['sector_impacts'][0][field]=value
            with self.assertRaises(ValueError):validate_analysis(a,BODY)
        self.assertIn('绝不能执行',system_prompt())

    def test_no_summary_is_presented_as_full_text_and_failed_calls_cool(self):
        data={'news':[{'id':'a','title':'只有标题','summary':BODY,'full_text_status':'unavailable'}]}
        with TemporaryDirectory() as d,patch.dict('os.environ',{'MINIMAX_API_KEY':'fake'}):
            r=enrich_minimax(data,{'network_enabled':True},NOW,d,lambda *a:self.fail('must not send an abstract'))
            self.assertEqual(r['news'][0]['llm_status'],'awaiting_full_text')
            data['news'][0].update(full_text=BODY,full_text_status='complete')
            calls=[]
            def fail(*args):calls.append(1);raise ModelFailure('http_429',3600)
            enrich_minimax(data,{'network_enabled':True},NOW,d,fail)
            enrich_minimax(data,{'network_enabled':True},NOW+timedelta(minutes=30),d,fail)
            self.assertEqual(len(calls),1)
            self.assertNotIn('fake',(Path(d)/'minimax-cache.json').read_text())

    def test_full_body_is_sent_once_and_caches_actual_model_and_prompt(self):
        data={'news':[{'id':'a','title':'原油影响','full_text':BODY,'full_text_status':'complete'}]};calls=[]
        def infer(n,*args):calls.append(n['full_text']);return analysis()
        with TemporaryDirectory() as d,patch.dict('os.environ',{'MINIMAX_API_KEY':'fake'}),patch('pipeline.minimax_news.shanghai_now',return_value=NOW):
            one=enrich_minimax(data,{'network_enabled':True},NOW,d,infer)
            two=enrich_minimax(data,{'network_enabled':True},NOW,d,infer)
            self.assertEqual(calls,[BODY]);self.assertEqual(two['minimax_summary']['reused'],1)
            self.assertEqual(one['news'][0]['llm_analysis']['model'],DEFAULT_MODEL)

    def test_invalid_article_does_not_starve_the_rest_of_the_queue(self):
        data={'news':[{'id':str(i),'title':str(i),'full_text':BODY,'full_text_status':'complete'} for i in range(2)]}
        def infer(n,*args):
            if n['id']=='0':raise ModelFailure('invalid_analysis',86400)
            return analysis()
        with TemporaryDirectory() as d,patch.dict('os.environ',{'MINIMAX_API_KEY':'fake'}),patch('pipeline.minimax_news.shanghai_now',return_value=NOW):
            result=enrich_minimax(data,{'network_enabled':True},NOW,d,infer)
            self.assertEqual(result['minimax_summary']['ready'],1)
            self.assertIsNone(result['minimax_summary']['cooldown_until'])
            cached=json.loads((Path(d)/'minimax-cache.json').read_text())
            self.assertEqual(len(cached['failures']),1)

    def test_sector_directions_are_opposite_and_unrelated_indices_are_not_scored(self):
        a={**analysis(),'status':'ready','model':DEFAULT_MODEL,'prompt_version':PROMPT_VERSION,'input_hash':text_hash(BODY),'scored_at':NOW.isoformat()}
        n={'id':'a','title':'原油价格上涨','full_text':BODY,'full_text_status':'complete','llm_analysis':a,'published_at':NOW.isoformat(),'first_seen_at':NOW.isoformat()}
        data={'news':[n],'market':[{'id':'sh000001','name':'上证指数'}],'sectors':[{'id':'801960.SI','name':'石油石化'},{'id':'801170.SI','name':'交通运输'},{'id':'801780.SI','name':'银行'}]}
        result=aggregate_entity_sentiment(data,NOW,{'sentiment':{'current_day_only':True}})
        self.assertEqual([s['sentiment']['label'] for s in result['sectors']],['positive','negative','unknown'])
        self.assertEqual(result['market'][0]['sentiment']['label'],'unknown')
        self.assertEqual(news_features(result['sectors'][1],NOW)[0],-1.)
        self.assertEqual(news_features(result['sectors'][1],NOW-timedelta(minutes=1))[-1],1.)
        # Changing the article invalidates the prior analysis; no reuse by title.
        n['full_text']+='正文发生变动'
        stale=aggregate_entity_sentiment(data,NOW)
        self.assertNotEqual(stale['news'][0]['sentiment_method'],'minimax_full_text')


class MinuteTests(unittest.TestCase):
    def test_exact_identity_times_and_no_pre_release_requests(self):
        rows=[{'ts_code':'801780.SI','trade_time':'2026-11-17 14:00:00','close':10},
              {'ts_code':'801780.SI','trade_time':'2026-11-17 15:00:00','close':11},
              {'ts_code':'BK0475.DC','trade_time':'2026-11-17 15:00:00','close':999}]
        self.assertEqual([p['price'] for p in minute_points(rows,'801780.SI','2026-11-17',NOW)],[10.,11.])
        class Client:
            def __init__(self):self.calls=0
            def request(self,*a):self.calls+=1;return rows
        client=Client();d={'sectors':[{'id':'801780.SI','index_code':'801780.SI','taxonomy':'SW2021'}]}
        cfg={'network_enabled':True}
        collect_primary_minutes(d,cfg,NOW.replace(hour=19),client);self.assertEqual(client.calls,0)
        r=collect_primary_minutes(d,cfg,NOW,client);self.assertEqual(client.calls,1)
        collect_primary_minutes(r,cfg,NOW,client);self.assertEqual(client.calls,1)
        self.assertEqual(len(r['intraday']['801780.SI']['points']),2)
