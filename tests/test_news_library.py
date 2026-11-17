from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json,unittest
from pipeline.news_discovery import parse_news_links
from pipeline.news_digest import digest_inputs,validate_digest,enrich_digest
from pipeline.publish import publish_view
from pipeline.sources import _news_item,_parse_rss

NOW=datetime.fromisoformat('2026-11-17T21:00:00+08:00')
class NewsLibraryTests(unittest.TestCase):
 def test_all_list_entries_are_retained_without_topic_filter(self):
  rss='<rss><channel>'+''.join(f'<item><title>资讯标题{i}</title><link>https://example.test/{i}</link></item>' for i in range(60))+'</channel></rss>'
  rows=_parse_rss(rss.encode());self.assertEqual(len(rows),60)
  item=_news_item(rows[-1],{'id':'test','name':'test'},[],{'retain_all_discovered_news':True},NOW,{})
  self.assertIsNotNone(item);self.assertFalse(item['relevance_verified']);self.assertEqual(item['market_ids'],[])
 def test_discovery_deduplicates_public_article_links(self):
  html='<a href="//finance.eastmoney.com/a/202611173932144347.html">一条真实形式新闻</a>'*2+'<a href="https://evil.test/a/202611173932144347.html">外部注入内容</a>'
  self.assertEqual(len(parse_news_links(html.encode(),'https://finance.eastmoney.com/')),1)
 def test_full_text_is_published_separately_without_truncation(self):
  text='完整正文\n'*5000+'最后一段'
  data={'news':[{'id':'n','full_text_status':'complete','full_text':text}]}
  with TemporaryDirectory() as d:
   view=publish_view(data,Path(d)/'view.json');n=view['news'][0]
   self.assertNotIn('full_text',n)
   body=json.loads((Path(d)/'articles'/Path(n['article_text_url']).name).read_text())
   self.assertEqual(body['full_text'],text);self.assertEqual(body['id'],'n')
 def test_digest_covers_every_current_day_item_and_caches_model_output(self):
  news=[{'id':str(i),'title':str(i),'published_at':NOW.isoformat(),'summary':'来源摘要'} for i in range(70)]
  news.append({'id':'old','published_at':'2026-11-16T09:00:00+08:00'})
  rows=digest_inputs(news,'2026-11-17');self.assertEqual(len(rows),70)
  value={'summary':'当日资讯综合解读，利好与风险需要区分。','positives':[{'text':'待落地的政策支持','article_ids':['0']}],'risks':[],'watch':[]}
  bad={**value,'risks':[{'text':'虚构依据','article_ids':['absent']}]}
  with self.assertRaises(ValueError):validate_digest(bad,{r['id'] for r in rows})
  calls=[]
  def infer(article,cfg,key,**kwargs):
   calls.append(article);return kwargs['validator'](value)
  with TemporaryDirectory() as d,patch.dict('os.environ',{'MINIMAX_API_KEY':'fake'}),patch('pipeline.news_digest.shanghai_now',return_value=NOW):
   one=enrich_digest({'news':news},{'network_enabled':True},NOW,d,infer)
   two=enrich_digest({'news':news},{'network_enabled':True},NOW,d,infer)
   self.assertEqual(len(calls),1);self.assertEqual(two['news_digest']['article_count'],70)
   self.assertNotIn('fake',Path(d,'daily-digest-cache.json').read_text())
