"""Supplement primary news with one cached CCTV bulletin request per day."""
from copy import deepcopy
import hashlib
from datetime import timedelta
from .calendar import shanghai_now
from .stock_api import _Client,_Failure,FIELDS,ENDPOINT,credential_matches,_clean_text
import os

def enrich_primary_news(dashboard,config=None,now=None,client=None):
    config=config or {};now=shanghai_now(now);result=deepcopy(dashboard)
    token=os.environ.get('STOCK_API_TOKEN','')
    if not config.get('network_enabled') or not token and client is None or not credential_matches(token) and client is None:return result
    state=result.setdefault('primary_news_state',{})
    if state.get('service_endpoint')!=ENDPOINT:state.clear();state['service_endpoint']=ENDPOINT
    day=now.strftime('%Y%m%d')
    if now.hour<20 or state.get('completed_date')==day or state.get('retry_after','')>now.isoformat():return result
    FIELDS['cctv_news']='date,title,content'
    client=client or _Client(token,{'max_requests':1,'budget_seconds':40,'min_interval_seconds':6,'timeout_seconds':25},now)
    try:
        rows=client.request('cctv_news',{'date':day})
        incoming=[]
        for row in rows:
            if str(row.get('date'))!=day:continue
            text=_clean_text(row.get('content'),token,4000000);title=_clean_text(row.get('title'),token,500)
            if not text:continue
            incoming.append({'id':'cctv-'+hashlib.sha256((day+title+text).encode()).hexdigest()[:24],
                'title':title,'content':text,'summary':text[:800],'published_at':None,'published_date':now.date().isoformat(),
                'first_seen_at':now.isoformat(),'fetched_at':now.isoformat(),'provider':'stock_api','source_id':'stock_api:cctv_news',
                'source_name':'新闻联播（接口文本）','source':'cctv_news','category':'国内政策','credibility':'media','url':None,
                'full_text':text,'full_text_hash':hashlib.sha256(text.encode()).hexdigest(),'full_text_status':'complete',
                'full_text_method':'provider_news_text','full_text_fetched_at':now.isoformat(),'full_text_scope':'服务返回的完整资讯文本'})
        result.setdefault('stock_api_news',[]).extend(incoming)
        state.update(status='success' if incoming else 'empty',last_success=now.isoformat(),article_count=len(incoming))
        if incoming:state['completed_date']=day
        else:state['retry_after']=(now+timedelta(minutes=30)).isoformat()
    except _Failure as e:
        state.update(status='unavailable',error_code=e.code,provider_code=e.provider_code,provider_reason=e.provider_reason,retry_after=(now+timedelta(seconds=max(60,e.retry_seconds))).isoformat())
    return result
