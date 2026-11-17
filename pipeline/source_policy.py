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
    covered=bool(start and end and fetched and fetched<=now and start<=end<=now and state.get('records'))
    hidden=0
    for n in dashboard.get('news',[]):
        at=instant(n.get('published_at'))
        primary=n.get('provider')=='stock_api' or str(n.get('source_id','')).startswith('stock_api:')
        n['primary_shadowed']=bool(covered and not primary and at and start<=at<=end)
        hidden+=n['primary_shadowed']
    existing_digest=dashboard.get('news_digest') or {}
    suppressed_ids={n.get('id') for n in dashboard.get('news',[]) if n.get('primary_shadowed')}
    if any(identity in suppressed_ids for identity in existing_digest.get('article_ids',[])):
        dashboard['news_digest_previous']=existing_digest
        dashboard['news_digest']={'status':'pending','current_article_count':sum(not n.get('primary_shadowed') and str(n.get('published_at') or n.get('first_seen_at') or '')[:10]==now.date().isoformat() for n in dashboard.get('news',[])), 'scope':'主源覆盖范围变化，等待重新汇总'}
    dashboard['news_source_policy']={'primary':'Tushare-compatible','mode':'primary_with_gap_fallback',
        'coverage_start':state.get('coverage_start'),'coverage_end':state.get('coverage_end'),
        'primary_window_available':covered,'secondary_hidden':hidden}
    return dashboard
