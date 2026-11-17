"""Suppress secondary observations only inside an actually covered primary window."""
from datetime import datetime
from .calendar import shanghai_now

def instant(value):
    try:return shanghai_now(datetime.fromisoformat(value)) if value else None
    except (ValueError,TypeError):return None

def apply_primary_news_policy(dashboard,now=None):
    now=shanghai_now(now);state=dashboard.get('stock_api_state',{}).get('news',{})
    start,end=instant(state.get('coverage_start')),instant(state.get('coverage_end'))
    fetched=instant(state.get('fetched_at'))
    covered=bool(start and end and fetched and 0<=(now-fetched).total_seconds()<=3600 and state.get('records'))
    hidden=0
    for n in dashboard.get('news',[]):
        at=instant(n.get('published_at'))
        primary=n.get('provider')=='stock_api' or str(n.get('source_id','')).startswith('stock_api:')
        n['primary_shadowed']=bool(covered and not primary and at and start<=at<=end)
        hidden+=n['primary_shadowed']
    dashboard['news_source_policy']={'primary':'Tushare-compatible','mode':'primary_with_gap_fallback',
        'coverage_start':state.get('coverage_start'),'coverage_end':state.get('coverage_end'),
        'primary_window_available':covered,'secondary_hidden':hidden}
    return dashboard
