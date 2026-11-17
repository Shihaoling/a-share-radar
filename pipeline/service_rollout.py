"""Resume a bounded capability inventory and perform one verified source migration."""
import json,os
from pathlib import Path
from datetime import timedelta
from .calendar import shanghai_now,previous_trading_day
from .data_service import REGULAR_APIS
from .stock_api import _Client,_Failure,FIELDS,ENDPOINT,credential_matches
from .run import load,atomic_json
from .probe_stock_api import specifications,_matches

VERSION='sxdata-primary-v1'

def plan(now):
    day=previous_trading_day(now.date()).isoformat()
    specs=specifications(now)
    rows=[('trade_cal',{'exchange':'SSE','start_date':day.replace('-',''),'end_date':day.replace('-','')},FIELDS['trade_cal'])]
    rows+=specs['core'][:1]+specs['core'][3:4]
    rows+=[('idx_mins',{'ts_code':'000001.SH','freq':'5min','start_date':day+' 09:00:00','end_date':day+' 19:00:00'},'ts_code,trade_time,open,close,high,low,vol,amount')]
    rows+=[('news',{'src':'cls','start_date':now.strftime('%Y-%m-%d')+' 09:00:00','end_date':now.strftime('%Y-%m-%d')+' 10:00:00'},'datetime,title,content,channels')]
    rows+=specs['financials']+specs['fund_macro']+specs['premium']
    rows+=[('moneyflow',{'ts_code':'600519.SH','trade_date':day.replace('-','')},'ts_code,trade_date,net_mf_amount')]
    rows+=[('stk_mins',{'ts_code':'600519.SH','freq':'5min','start_date':day+' 09:00:00','end_date':day+' 19:00:00'},'ts_code,trade_time,open,close,high,low,vol,amount'),
           ('sw_mins',{'ts_code':'801780.SI','freq':'5min','start_date':day+' 09:00:00','end_date':day+' 19:00:00'},'ts_code,trade_time,open,close,high,low,vol,amount')]
    rows+=specs['news_extended'][1:]
    seen=set();out=[]
    for api,params,fields in rows:
        if api not in seen:seen.add(api);out.append((api,params,fields))
    return out

def run(root=None,now=None,client=None):
    production=client is None and root is None
    root=Path(root) if root else Path(__file__).resolve().parents[1];now=shanghai_now(now)
    path=root/'web/data/data-service-capabilities.json';report=load(path,{})
    if report.get('request_id')!=VERSION:report={'request_id':VERSION,'service':ENDPOINT,'results':[],'cursor':0}
    token=os.environ.get('STOCK_API_TOKEN','')
    if client is None and (not token or not credential_matches(token)):
        report.update(status='needs_token_update',checked_at=now.isoformat(),credential_match=False)
        atomic_json(path,report);print(json.dumps({'service':ENDPOINT,'status':'needs_token_update'}));return report
    report['credential_match']=True
    if report.get('next_allowed_after','')>now.isoformat():return report
    # A source-only refresh keeps already published news/model outputs and does not invoke MiniMax.
    if not report.get('migration_completed') and production and os.environ.get('GITHUB_ACTIONS')=='true':
        from .run import run as refresh
        refresh(stock_api_only=True)
        from .primary_minutes import collect_primary_minutes
        from .publish import publish_view
        cfg=load(root/'config/settings.json',{});cfg['network_enabled']=True
        snapshot=load(root/'web/data/dashboard.json',{})
        # Complete one initial day's backfill in at most four bounded batches.
        # Each failure retains its endpoint cooldown; no-progress ends the loop.
        for _ in range(3):
            before=len(snapshot.get('primary_minutes_state',{}).get('completed',{}))
            snapshot=collect_primary_minutes(snapshot,cfg,shanghai_now())
            atomic_json(root/'web/data/dashboard.json',snapshot);publish_view(snapshot,root/'web/data/view.json')
            if len(snapshot.get('primary_minutes_state',{}).get('completed',{}))<=before:break
        report['minute_entities_completed']=len(snapshot.get('primary_minutes_state',{}).get('completed',{}))
        report['migration_completed']=shanghai_now().isoformat();atomic_json(path,report)

    rows=plan(now);start=report.get('cursor',0)
    client=client or _Client(token,{'max_requests':6,'budget_seconds':120,'min_interval_seconds':6,'timeout_seconds':25},now)
    for api,params,fields in rows[start:start+6]:
        original=FIELDS.get(api);FIELDS[api]=fields
        row={'api':api,'params':params,'channel':'backup15000' if api in REGULAR_APIS else 'main'}
        before_count=getattr(client,'count',None)
        not_attempted=False
        try:
            records=client.request(api,{**params,'limit':3})
            matched=_matches(records,params) if records else None
            row.update(status='success' if records and matched else 'query_mismatch' if records else 'empty',row_count=len(records),query_match=matched,fields=fields.split(','))
        except _Failure as e:
            not_attempted=before_count is not None and getattr(client,'count',None)==before_count
            row.update(status='unavailable',error_code=e.code,provider_code=e.provider_code,provider_reason=e.provider_reason)
            report['next_allowed_after']=(shanghai_now()+timedelta(seconds=max(60,e.retry_seconds))).isoformat()
        finally:
            if original is None:FIELDS.pop(api,None)
            else:FIELDS[api]=original
        if not_attempted:
            report['deferred_api']=api;break
        report['results'].append(row);report['cursor']+=1
        if row['status']=='unavailable':break
    report.update(checked_at=shanghai_now().isoformat(),planned_apis=len(rows),pending_apis=max(0,len(rows)-report['cursor']),status='complete' if report['cursor']>=len(rows) else 'partial')
    atomic_json(path,report)
    atomic_json(root/'web/data/tushare-capabilities.json',{'sxdata_primary':report})
    print(json.dumps({'service':ENDPOINT,'status':report['status'],'tested':len(report['results']),'pending':report['pending_apis']},ensure_ascii=False))
    return report

if __name__=='__main__':run()
