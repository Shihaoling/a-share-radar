"""Persistent, low-frequency online completion of an explicitly requested cohort."""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import json
from .calendar import shanghai_now,previous_trading_day,is_trading_day
from .run import load,atomic_json
from .news_content import enrich_full_text
from .minimax_news import enrich_minimax
from .news_digest import enrich_digest
from .news_trust import apply_source_whitelist
from .news_archive import archive_news
from .publish import publish_view
from .sentiment import aggregate_entity_sentiment


def run(root=None,now=None,collector=None,analyzer=None,digester=None):
    root=Path(root) if root else Path(__file__).resolve().parents[1];now=shanghai_now(now)
    request=load(root/'data/news_content/backfill-request.json',{})
    if not request.get('articles'):return {'status':'not_requested'}
    state_path=root/'data/news_content/backfill-state.json';state=load(state_path,{})
    if state.get('request_id')!=request['request_id']:state={'request_id':request['request_id'],'runs':0}
    if state.get('next_allowed_after','')>now.isoformat():return state
    d=load(root/'web/data/dashboard.json',{});cfg=load(root/'config/settings.json',{});cfg['network_enabled']=True
    known={n['id']:n for n in d.setdefault('news',[])}
    for item in request['articles']:
        if item['id'] not in known:d['news'].append(deepcopy(item));known[item['id']]=d['news'][-1]
    ids={n['id'] for n in request['articles']}
    selected=[deepcopy(known[i]) for i in ids]
    for n in selected:n['primary_shadowed']=False
    batch=max(1,min(8,int(request.get('batch_size',8))))
    cfg['news_content']={**cfg.get('news_content',{}),'reuse_only':False,'max_articles_per_run':batch,'max_articles_per_host':batch,'request_interval_seconds':max(20,request.get('request_interval_seconds',20)),'verify_publication_times':True}
    result=(collector or enrich_full_text)({'news':selected},cfg,now,root/'data/news_content')
    # Save fetched bodies before the slower model stage; a later run resumes them.
    for n in result['news']:known[n['id']].update(n)
    atomic_json(root/'web/data/dashboard.json',d)
    cfg['minimax']={**cfg.get('minimax',{}),'max_articles_per_run':batch,'budget_seconds':480}
    result=(analyzer or enrich_minimax)(result,cfg,now,root/'data/news_sentiment')
    for n in result['news']:known[n['id']].update(n)
    apply_source_whitelist(d)
    d=aggregate_entity_sentiment(d,shanghai_now(),{'sentiment':{'model_enabled':False,'reuse_only':True,'current_day_only':True}})
    full=sum(bool(n.get('full_text')) and n.get('full_text_status')=='complete' for n in result['news'])
    dated=sum(n.get('publication_verification',{}).get('status')=='verified' for n in result['news'])
    scored=sum(n.get('llm_analysis',{}).get('status')=='ready' for n in result['news'])
    changed=state.get('body_complete')!=full or state.get('model_complete')!=scored or state.get('publication_verified')!=dated
    cfg['news_digest']={**cfg.get('news_digest',{}),'target_date':request['digest_date']}
    if changed:
        revised=(digester or enrich_digest)(d,cfg,shanghai_now(),root/'data/news_sentiment')
        d['news_backfill_digest']=revised['news_digest']
        if d.get('news_digest',{}).get('date') in (None,request['digest_date']):d['news_digest']=revised['news_digest']
    completed=full==len(ids) and scored==len(ids)
    state.update(status='complete' if completed else 'in_progress',runs=state['runs']+1,total=len(ids),body_complete=full,publication_verified=dated,publication_unconfirmed=full-dated,model_complete=scored,last_run_at=shanghai_now().isoformat(),next_allowed_after=(shanghai_now()+timedelta(minutes=30)).isoformat(),content=result.get('news_content_summary'),model=result.get('minimax_summary'))
    d['news_backfill_summary']=state;d['generated_at']=shanghai_now().isoformat()
    # Historical labels remain historical; new analyses are archived only now.
    d['news_archive_summary']=archive_news(d,root=root/'data/news_archive')
    atomic_json(state_path,state);atomic_json(root/'web/data/dashboard.json',d);publish_view(d,root/'web/data/view.json')
    print(json.dumps(state,ensure_ascii=False));return state


if __name__=='__main__':run()
