"""Locally score already-collected news; no news-site requests or backdating."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import argparse
from collections import Counter
from datetime import datetime,timezone
import json
import hashlib
import truststore
truststore.inject_into_ssl()
from pipeline.sentiment import (_body,_hash,_timestamp,_finbert_scores,_english_finbert_scores,_is_chinese,
    MODEL_ID,MODEL_REVISION,ENGLISH_MODEL_ID,ENGLISH_MODEL_REVISION)
from pipeline.calendar import shanghai_now
from pipeline.train_direction_probability import write

p=argparse.ArgumentParser();p.add_argument('--source',default='web/data/dashboard.json');p.add_argument('--output',default='data/news_sentiment/cache.json');a=p.parse_args()
raw=Path(a.source).read_bytes();d=json.loads(raw);now=shanghai_now();cache={'schema_version':1,'source_sha256':hashlib.sha256(raw).hexdigest(),'articles':{}}
cfg={'torch_threads':4,'model_batch_size':4};report={'started_at':now.isoformat(),'models':{},'sanity_checks':{},'source_news_count':len(d.get('news',[]))}
for language,fn,model,revision in [('chinese',_finbert_scores,MODEL_ID,MODEL_REVISION),('english',_english_finbert_scores,ENGLISH_MODEL_ID,ENGLISH_MODEL_REVISION)]:
    items=[n for n in d.get('news',[]) if _is_chinese(n)==(language=='chinese') and (_timestamp(n.get('first_seen_at')) is None or _timestamp(n.get('first_seen_at'))<=now) and (_timestamp(n.get('published_at')) is None or _timestamp(n.get('published_at'))<=now)]
    print('SCORING',language,len(items),flush=True)
    if language=='chinese':
        smoke=[{'id':'smoke-pos','title':'公司营业收入和净利润均大幅增长，业绩明显超出市场预期。'},{'id':'smoke-neg','title':'公司营业收入和净利润大幅下降，持续亏损且面临债务违约风险。'}]
    else:
        smoke=[{'id':'smoke-pos','title':'The company reported sharply higher profits and strong revenue growth, beating expectations.'},{'id':'smoke-neg','title':'The company reported a large loss, falling revenue and a debt default.'}]
    scores=fn(items+smoke,cfg);at=shanghai_now().isoformat()
    for n in items:
        cache['articles'][n['id']]={'input_hash':_hash(_body(n)[2]),'scored_at':at,'scoring':scores[n['id']]}
    sanity=scores['smoke-pos']['score']>scores['smoke-neg']['score']
    if not sanity:raise ValueError(language+' sentiment polarity sanity failed')
    report['sanity_checks'][language]={'positive_score':scores['smoke-pos']['score'],'negative_score':scores['smoke-neg']['score'],'passed':sanity,'purpose':'polarity smoke check only; not labeled-news accuracy'}
    report['models'][language]={'id':model,'revision':revision,'articles':len(items),'labels':dict(Counter(scores[n['id']]['label'] for n in items))}
    cache['generated_at']=at;write(a.output,cache);write('data/news_sentiment/inference-report.json',report)
    print('DONE',language,report['models'][language],flush=True)
report['completed_at']=shanghai_now().isoformat();report['label_accuracy']=None;report['label_accuracy_reason']='No independently labeled project news validation set';write('data/news_sentiment/inference-report.json',report)
