import gzip,json
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from pipeline.news_archive import archive_news
from pipeline.sentiment import aggregate_entity_sentiment,ENGLISH_MODEL_ID,ENGLISH_MODEL_REVISION
from unittest.mock import patch

NOW=datetime.fromisoformat('2026-11-17T10:00:00+08:00')

class NewsArchiveTests(unittest.TestCase):
    def test_same_article_is_deduplicated_but_model_revision_is_preserved(self):
        n={'id':'n1','title':'Bank profits rise','first_seen_at':'2026-11-17T09:00:00+08:00','sentiment':'positive','sentiment_score':.6,'sentiment_input_hash':'x','sentiment_model':{'version':'a'}}
        d={'news':[n]}
        with TemporaryDirectory() as folder:
            one=archive_news(d,NOW,folder);two=archive_news(d,datetime.fromisoformat('2026-11-17T10:30:00+08:00'),folder)
            self.assertEqual(one['new_versions'],1);self.assertEqual(two['new_versions'],0);self.assertEqual(two['collection_runs'],2)
            n['sentiment_model']['version']='b';n['sentiment_score']=.4
            archive_news(d,datetime.fromisoformat('2026-11-17T11:00:00+08:00'),folder)
            raw=json.loads(gzip.decompress((Path(folder)/'20261117.json.gz').read_bytes()))
            self.assertEqual(len(raw['articles']),2);self.assertEqual({r['first_seen_at'] for r in raw['articles'].values()},{'2026-11-17T09:00:00+08:00'})
            self.assertEqual(sorted(r['sentiment_score'] for r in raw['articles'].values()),[.4,.6])

    def test_english_finbert_results_are_used_and_reused_without_reinference(self):
        n={'id':'n1','title':'Bank profits rise sharply','summary':'Revenue growth exceeded expectations.','first_seen_at':'2026-11-17T09:00:00+08:00','published_at':'2026-11-17T09:00:00+08:00'}
        fake={'n1':{'label':'positive','score':.7,'confidence':.8,'confidence_kind':'model_softmax_uncalibrated','method':'finbert_english_cpu','model_version':ENGLISH_MODEL_ID+'@'+ENGLISH_MODEL_REVISION,'evidence':{'model_probabilities':{'positive':.8,'negative':.1,'neutral':.1}},'note':'text only'}}
        d={'news':[n],'market':[],'sectors':[{'id':'801780.SI','name':'银行'}]}
        with patch('pipeline.sentiment._english_finbert_scores',return_value=fake) as model:
            result=aggregate_entity_sentiment(d,NOW,{'sentiment':{'model_enabled':True}})
        self.assertEqual(model.call_count,1);self.assertEqual(result['news'][0]['sentiment'],'positive');self.assertEqual(result['sectors'][0]['sentiment']['label'],'positive')
        self.assertEqual(result['sentiment_summary']['model_language_counts']['english'],1)
        with patch('pipeline.sentiment._english_finbert_scores',side_effect=AssertionError('must reuse')):
            result=aggregate_entity_sentiment(result,NOW,{'sentiment':{'reuse_only':True}})
        self.assertEqual(result['news'][0]['sentiment_model']['model_id'],ENGLISH_MODEL_ID)

    def test_current_day_policy_excludes_yesterday_but_keeps_article_browsable(self):
        d={'news':[{'id':'old','title':'银行利润增长','first_seen_at':'2026-11-16T23:00:00+08:00','published_at':'2026-11-16T23:00:00+08:00'}],'sectors':[{'id':'801780.SI','name':'银行'}]}
        r=aggregate_entity_sentiment(d,NOW,{'sentiment':{'current_day_only':True}})
        self.assertEqual(len(r['news']),1);self.assertEqual(r['sectors'][0]['sentiment']['article_count'],0)

    def test_exact_daily_half_hour_schedule_and_quote_dispatch_match(self):
        s=(Path(__file__).resolve().parents[1]/'.github/workflows/update-and-publish.yml').read_text()
        self.assertIn("cron: '0,30 1-7 * * *'",s);self.assertIn("cron: '0 8 * * *'",s)
        self.assertGreaterEqual(s.count('5,10,15,20,25,35,40,45,50,55 1-7 * * 1-5'),4)

if __name__=='__main__':unittest.main()
