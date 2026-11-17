from copy import deepcopy
from datetime import datetime,date,timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from pipeline.calendar import is_trading_day
from pipeline.daily_forecast import digest
from pipeline.direction_probability import (SCHEMA,NEWS_NAMES,news_features,probability_table,predict_probability,load_probability_bundle,update_direction_probabilities)
from pipeline.train_direction_probability import fit,evaluate,paired_brier_interval

NOW=datetime.fromisoformat('2026-11-17T14:00:00+08:00')


def fixture():
    days=[];day=date(2026,6,1)
    while day<=date(2026,11,16):
        if is_trading_day(day):days.append(day.isoformat())
        day+=timedelta(days=1)
    bars=[]
    for i,day in enumerate(days):
        opening=100+i*.05;close=opening+(1 if i%3 else -1)*.6
        bars.append(dict(date=day,open=opening,close=close,high=max(opening,close)+.3,low=min(opening,close)-.3,complete=True))
    return {'market':[{'id':'sh000300','name':'沪深300','history':bars}], 'sectors':[{'id':'801780.SI','name':'银行','history':deepcopy(bars)}]}


def make_bundle():
    prior={'family':'prior','value':.57,'by_id':{}}
    return dict(schema_version=SCHEMA,model_id='test-v1',trained_at='2026-11-16T19:00:00+08:00',training_cutoff='2026-11-16',target='next_close_direction',
        entity_ids=['sh000300','801780.SI'],models={'market':prior,'sector':prior},priors={'market':prior,'sector':prior},flat_rates={},selection={},evaluation={},sentiment_training={'enabled':False})


class ProbabilityTests(unittest.TestCase):
    def save(self,path,b):
        b=deepcopy(b);b['sha256']=digest(b);Path(path).write_text(json.dumps(b));return b

    def test_news_requires_actual_availability_and_verified_window(self):
        item={'score':.7,'scored_article_count':3,'source_count':2,'available_at':'2026-11-17T14:01:00+08:00','window_policy_version':'published-and-observed-v1'}
        e={'sentiment_history':[item]};self.assertEqual(news_features(e,NOW)[-1],1)
        item['available_at']='2026-11-17T13:00:00+08:00';self.assertEqual(news_features(e,NOW)[0],.7)
        item.pop('window_policy_version');self.assertEqual(news_features(e,NOW)[-1],1)

    def test_current_news_is_never_repeated_into_past_training_dates(self):
        d=fixture();d['sectors'][0]['sentiment_history']=[dict(score=.5,scored_article_count=2,available_at='2026-11-17T12:00:00+08:00',window_policy_version='published-and-observed-v1')]
        table=probability_table(d)
        self.assertTrue(all(r['news'][-1]==1 for r in table['sectors']))
        self.assertTrue(all(len(r['x'])==len(table['feature_names']) for r in table['sectors']))

    def test_sigmoid_inference_and_blend_are_real_probabilities(self):
        base={'family':'logistic','columns':[0],'coef':[2.],'intercept':0.,'mean':[0.],'scale':[1.]}
        p=predict_probability(base,[{'x':[-1.]},{'x':[0.]},{'x':[1.]}]);self.assertLess(p[0],p[1]);self.assertLess(p[1],p[2]);self.assertAlmostEqual(p[1],.5)
        with self.assertRaises(ValueError):predict_probability(base,[{'x':[float('nan')]}])

    def test_classifier_fit_separates_calibration_and_exports_identical_results(self):
        table=probability_table(fixture());rows=[r for r in table['sectors'] if 'direction' in r]
        asset,boundary=fit({'kind':'logistic','C':.1,'calibration':'sigmoid','blend':.5},rows,table['feature_names'],1)
        self.assertLess(boundary['base_fit_through'],boundary['calibration_start'])
        p=predict_probability(asset,rows[-10:]);self.assertTrue(all(0<p0<1 for p0 in p));self.assertGreater(max(p)-min(p),0)

    def test_checksum_tamper_and_future_model_are_rejected(self):
        with TemporaryDirectory() as folder:
            path=Path(folder)/'b.json';b=self.save(path,make_bundle());b['models']['market']['value']=.99;path.write_text(json.dumps(b))
            with self.assertRaisesRegex(ValueError,'integrity'):load_probability_bundle(path)
            b=make_bundle();b['trained_at']='2026-11-18T00:00:00+08:00';self.save(path,b)
            r=update_direction_probabilities(fixture(),path,NOW);self.assertEqual(r['direction_probability']['status'],'error')

    def test_frozen_issue_is_not_rewritten_by_later_inference(self):
        with TemporaryDirectory() as folder:
            path=Path(folder)/'b.json';b=make_bundle();self.save(path,b)
            first=update_direction_probabilities(fixture(),path,NOW);p=first['direction_probability']['sectors'][0]
            self.assertAlmostEqual(p['up_probability']+p['down_probability']+p['flat_probability'],1)
            b['models']['sector']['value']=.9;self.save(path,b)
            second=update_direction_probabilities(first,path,NOW,previous=first)
            self.assertEqual(second['direction_probability']['sectors'][0],p)

    def test_after_close_stale_daily_input_cannot_predict_a_past_target(self):
        with TemporaryDirectory() as folder:
            path=Path(folder)/'b.json';self.save(path,make_bundle())
            result=update_direction_probabilities(fixture(),path,datetime.fromisoformat('2026-11-17T16:00:00+08:00'))['direction_probability']
            self.assertEqual(result['sectors'],[]);self.assertEqual(len(result['stale_entities']),2);self.assertEqual(result['today_close_status'],'not_fitted')

    def test_date_block_metrics_do_not_treat_sectors_as_independent_days(self):
        rows=[{'id':'a','label_date':'2026-11-12','direction':1},{'id':'b','label_date':'2026-11-12','direction':-1},{'id':'a','label_date':'2026-11-13','direction':1}]
        p=[.8,.8,.8];m=evaluate(rows,p)
        self.assertAlmostEqual(m['brier'],(.34+.04)/2);self.assertEqual(m['dates'],2)
        ci=paired_brier_interval(rows,p,[.5]*3);self.assertEqual(ci['independent_dates'],2)

if __name__=='__main__':unittest.main()
