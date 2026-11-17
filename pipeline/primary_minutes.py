"""Bounded evening Tushare minute backfill with exact index identities."""
from copy import deepcopy
from datetime import datetime, timedelta
import os
from .calendar import shanghai_now, is_trading_day, SHANGHAI
from .stock_api import _Client, _Failure, _index_code, FIELDS, _instant, ENDPOINT,credential_matches

FIELDS.update({name: 'ts_code,trade_time,open,close,high,low,vol,amount' for name in ('sw_mins', 'idx_mins')})


def collect_primary_minutes(dashboard, config=None, now=None, client=None):
    now = shanghai_now(now); result = deepcopy(dashboard); cfg = (config or {}).get('primary_minutes', {})
    state = result.setdefault('primary_minutes_state', {'endpoints': {}, 'completed': {}})
    if state.get('service_endpoint')!=ENDPOINT:
        state['endpoints']={};state['completed']={}
    state['service_endpoint']=ENDPOINT
    state.setdefault('endpoints', {}); state.setdefault('completed', {})
    # Official SW minute data is updated 19:00–20:00. Never poll it intraday.
    if not (config or {}).get('network_enabled') or not cfg.get('enabled', True) or is_trading_day(now.date()) is not True or now.hour < 20:
        return result
    token = os.environ.get('STOCK_API_TOKEN', '')
    if (not token or not credential_matches(token)) and client is None:
        state['status']='credentials_pending';return result
    client = client or _Client(token, {'max_requests': 12, 'budget_seconds': 180, 'min_interval_seconds': 6,'timeout_seconds':30}, now)
    day = now.date().isoformat(); attempted = 0
    state['completed'] = {k:v for k,v in state['completed'].items() if k[:10] >= (now-timedelta(days=7)).date().isoformat()}
    for group, api in (('sectors', 'sw_mins'), ('market', 'idx_mins')):
        endpoint = state['endpoints'].setdefault(api, {})
        if endpoint.get('cooldown_until', '') > now.isoformat(): continue
        for entity in result.get(group, []):
            code = entity.get('index_code') if group == 'sectors' else _index_code(entity)
            if not code or group == 'sectors' and (entity.get('taxonomy') != 'SW2021' or not str(code).endswith('.SI')): continue
            key = day+':'+code
            if state['completed'].get(key): continue
            if attempted >= int(cfg.get('max_requests', 12)): break
            try:
                attempted += 1
                rows = client.request(api, {'ts_code': code, 'freq': '5min', 'start_date': day+' 09:30:00', 'end_date': day+' 15:00:00'})
                points = minute_points(rows, code, day, now)
                if len(points) < 2:
                    endpoint.update(status='pending', last_attempt=now.isoformat(), cooldown_until=(now+timedelta(minutes=30)).isoformat()); break
                bucket = result.setdefault('intraday', {}).setdefault(entity['id'], {'points': []})
                if isinstance(bucket, list): bucket = {'points': bucket}; result['intraday'][entity['id']] = bucket
                # Keep old observed snapshots for audit. Frontend selects Tushare
                # minutes only for the same code/day, and never joins taxonomies.
                prior = [p for p in bucket.get('points', []) if not (p.get('provider') == 'stock_api' and str(p.get('timestamp', '')).startswith(day))]
                bucket['points'] = prior + points
                state['completed'][key] = {'available_at': now.isoformat(), 'count': len(points)}
                endpoint.update(status='success', last_success=now.isoformat()); endpoint.pop('cooldown_until', None)
            except _Failure as e:
                endpoint.update(status='unavailable', error_code=e.code, last_attempt=now.isoformat(),
                    provider_code=e.provider_code, provider_reason=e.provider_reason,
                    cooldown_until=(now+timedelta(seconds=max(1800,e.retry_seconds))).isoformat())
                if e.code=='api_denied': endpoint['error_code']=e.provider_reason or 'provider_error'
                break
    state.update(last_run_at=now.isoformat(), requests_this_run=attempted,
        note='申万日线18:30；申万历史分钟19:00—20:00更新，20点后按实际权限分批补齐，原始观察点保留。')
    return result


def minute_points(rows, code, day, now):
    import math
    points = {}
    for r in rows:
        if r.get('ts_code') != code: continue
        stamp = _instant(str(r.get('trade_time') or '')); price = r.get('close')
        if stamp is None or stamp.date().isoformat() != day or stamp > now or type(price) not in (int,float) or not math.isfinite(price) or price <= 0: continue
        hhmm = stamp.strftime('%H:%M')
        if not ('09:30' <= hhmm <= '11:30' or '13:00' <= hhmm <= '15:00'): continue
        points[stamp.isoformat()] = {'timestamp': stamp.isoformat(), 'quote_timestamp': stamp.isoformat(), 'price': float(price),
            'provider': 'stock_api', 'timestamp_quality': 'exchange_timestamp', 'available_at': now.isoformat(),
            'source_code': code, 'frequency': '5min', 'kind': 'historical_minute'}
    return [points[k] for k in sorted(points)]
