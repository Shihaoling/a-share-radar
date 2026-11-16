import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from pipeline.review import daily_review

NOW=datetime(2026,11,16,16,tzinfo=ZoneInfo('Asia/Shanghai'))
class ReviewTests(unittest.TestCase):
    def test_no_fake_accuracy(self):
        r=daily_review({},NOW)
        self.assertIsNone(r['direction_accuracy'])
        self.assertIsNone(r['sentiment']['label_accuracy'])
    def test_cohorts_dedupe_and_timezone(self):
        r={'id':'one','sector_id':'a','sector_name':'A','target_date':'2026-11-16','target_kind':'close','issued_at':'2026-11-16T02:00:00Z','actual_price':100,'error_pct':1,'baseline_error_pct':2,'direction_correct':True}
        later={**r,'id':'two','issued_at':'2026-11-16T06:00:00Z','error_pct':3,'direction_correct':False}
        future={**r,'id':'three','target_date':'2026-11-17'}
        review=daily_review({'ledger':[r,later,future]},NOW)
        self.assertEqual(review['issued_count'],3)
        self.assertEqual(review['settled_count'],1)
        self.assertEqual(review['mae_pct'],3)
        self.assertEqual(review['direction_accuracy'],0)
    def test_rumors_excluded(self):
        news=[{'first_seen_at':'2026-11-15T17:00:00Z','sentiment':'positive','credibility':'rumor'}, {'first_seen_at':'2026-11-16T01:00:00Z','sentiment':'negative','credibility':'official'}]
        r=daily_review({'news':news},NOW)['sentiment']
        self.assertEqual(r['rumor_count'],1)
        self.assertEqual(r['positive'],0)
        self.assertEqual(r['negative'],1)
    def test_finbert_unknown_and_mixed_remain_in_daily_summary(self):
        template={'first_seen_at':'2026-11-16T10:00:00+08:00','sentiment_method':'finbert_chinese_cpu'}
        news=[dict(template,sentiment=label) for label in ('positive','mixed','unknown')]
        news.append(dict(template,sentiment='negative',sentiment_is_rumor=True))
        news.append(dict(template,sentiment='positive',sentiment_duplicate_of='one'))
        r=daily_review({'news':news},NOW)['sentiment']
        self.assertEqual(r['positive'],1)
        self.assertEqual(r['uncertain'],2)
        self.assertEqual(r['labels']['mixed'],1)
        self.assertEqual(r['rumor_count'],1)
        self.assertEqual(r['method'],'finbert_chinese_cpu')
        self.assertIsNone(r['label_accuracy'])
if __name__=='__main__': unittest.main()
