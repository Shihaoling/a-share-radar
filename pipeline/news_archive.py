"""Immutable news/score versions and half-hour collection receipts."""
from datetime import datetime
from pathlib import Path
import gzip
import json
from .calendar import shanghai_now
from .daily_forecast import digest,canonical_bytes

ROOT=Path(__file__).resolve().parents[1]


def archive_news(dashboard,now=None,root=None):
    now=shanghai_now(now);root=Path(root) if root else ROOT/'data/news_archive';root.mkdir(parents=True,exist_ok=True)
    path=root/(now.strftime('%Y%m%d')+'.json.gz')
    try:archive=json.loads(gzip.decompress(path.read_bytes()))
    except FileNotFoundError:archive={'schema_version':1,'date':now.date().isoformat(),'articles':{},'runs':[]}
    added=0;scored=0
    for n in dashboard.get('news',[]):
        if not n.get('id'):continue
        version=digest({'id':n['id'],'published_at':n.get('published_at'),'publication_verification':n.get('publication_verification'),'source_verified':n.get('source_verified'),'input_hash':n.get('sentiment_input_hash'),'model':n.get('sentiment_model'),'score':n.get('sentiment_score'),'label':n.get('sentiment'),'full_text_hash':n.get('full_text_hash'),'llm':n.get('llm_analysis')})
        if version in archive['articles']:continue
        record={k:n.get(k) for k in ('id','title','source','source_id','url','published_at','first_seen_at','sector_ids','market_ids','sentiment','sentiment_score','sentiment_method','sentiment_model','sentiment_input_hash','sentiment_scored_at','sentiment_eligible','sentiment_exclusion_reason','full_text','full_text_hash','full_text_status','full_text_fetched_at','full_text_method','publication_verification','published_at_verified_at','source_verified','source_verification_basis','llm_analysis','llm_status')}
        record.update(summary=str(n.get('summary') or n.get('content') or '')[:1000],archived_at=now.isoformat(),version=version)
        archive['articles'][version]=record;added+=1
        scored+=record.get('sentiment') in ('positive','negative','neutral','mixed')
    receipt={'observed_at':now.isoformat(),'new_versions':added,'new_scored_versions':scored,'visible_articles':len(dashboard.get('news',[])),
        'eligible_current_articles':dashboard.get('sentiment_summary',{}).get('eligible_count',0),'source':'scheduled_collection_or_explicit_refresh'}
    if not any(r['observed_at']==receipt['observed_at'] for r in archive['runs']):archive['runs'].append(receipt)
    temporary=path.with_suffix('.tmp');temporary.write_bytes(gzip.compress(canonical_bytes(archive),mtime=0));temporary.replace(path)
    return {'date':archive['date'],'new_versions':added,'archived_versions':len(archive['articles']),'collection_runs':len(archive['runs']),'last_collection':now.isoformat(),
        'schedule':'每天北京时间09:00—16:00，每30分钟；GitHub任务可能延迟','path':'data/news_archive/'+path.name}
