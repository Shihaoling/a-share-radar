"""One explicit, bounded diagnostic of HISTORICAL minutes, with safe reason codes."""
import json,os
from pathlib import Path
from .calendar import shanghai_now,previous_trading_day,is_trading_day
from .primary_minutes import _Client,_Failure,minute_points,collect_primary_minutes
from .run import atomic_json,load
from .stock_api import ENDPOINT,credential_matches
from .publish import publish_view

VERSION='historical-minutes-doc-check-v1'

def failure_details(error):
    reason=error.provider_reason
    code=(reason or 'provider_error') if error.code=='api_denied' else error.code
    return {'error_code':code,'provider_code':error.provider_code,'provider_reason':reason}

def run(root=None,client=None,now=None):
    root=Path(root) if root else Path(__file__).resolve().parents[1];now=shanghai_now(now)
    path=root/'web/data/minute-access.json';prior=load(path,{})
    if prior.get('request_id')==VERSION:return prior
    token=os.environ.get('STOCK_API_TOKEN','')
    if (not token or not credential_matches(token)) and client is None:return {'status':'missing_key'}
    client=client or _Client(token,{'max_requests':4,'budget_seconds':50,'min_interval_seconds':4},now)
    day=previous_trading_day(now.date()).isoformat()
    results=[]
    for api,code in [('sw_mins','801003.SI'),('idx_mins','000001.SH')]:
        params={'ts_code':code,'freq':'5min','start_date':day+' 09:00:00','end_date':day+' 19:00:00'}
        try:
            rows=client.request(api,params);points=minute_points(rows,code,day,now)
            results.append({'api':api,'kind':'historical_minutes','params':params,'status':'available' if len(points)>=2 else 'empty','rows':len(rows),'valid_points':len(points)})
        except _Failure as e:results.append({'api':api,'kind':'historical_minutes','params':params,'status':'unavailable',**failure_details(e)})
    report={'request_id':VERSION,'checked_at':shanghai_now().isoformat(),'service':ENDPOINT,'historical_trade_date':day,'results':results,'automatic_retries':0}
    dashboard=load(root/'web/data/dashboard.json',{})
    state=dashboard.setdefault('primary_minutes_state',{'endpoints':{},'completed':{}})
    for row in results:
        endpoint=state.setdefault('endpoints',{}).setdefault(row['api'],{})
        endpoint['historical_check']=row
        if row['status']=='unavailable':endpoint.update({k:row.get(k) for k in ('error_code','provider_code','provider_reason')})
        elif row['status']=='available':
            endpoint.pop('cooldown_until',None);endpoint.pop('error_code',None)
    # Only successfully verified historical endpoints may issue a bounded today backfill.
    if any(r['status']=='available' for r in results) and is_trading_day(now.date()) is True:
        dashboard=collect_primary_minutes(dashboard,{'network_enabled':True,'primary_minutes':{'max_requests':2}},now,client)
    atomic_json(root/'web/data/dashboard.json',dashboard);publish_view(dashboard,root/'web/data/view.json')
    atomic_json(path,report)
    return report

if __name__=='__main__':print(json.dumps(run(),ensure_ascii=False))
