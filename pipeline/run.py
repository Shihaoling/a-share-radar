"""python -m pipeline.run [--offline] [--quotes-only|--stock-api-only] [--enable-mlp] [--output PATH]."""
import argparse
from copy import deepcopy
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from pipeline.calendar import market_session
from pipeline.model import update_predictions
from pipeline.sources import fetch_data
from pipeline.fallback import enrich_with_fallback
from pipeline.validation import validate_prices
from pipeline.review import daily_review
from pipeline.intraday import collect_intraday
from pipeline.metrics import compute_metrics
from pipeline.stock_api import enrich_with_stock_api
from pipeline.sentiment import aggregate_entity_sentiment

ROOT = Path(__file__).resolve().parents[1]

def load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    os.replace(temp, path)

def run(offline=False, enable_mlp=False, output=None, quotes_only=False, stock_api_only=False, bootstrap=False):
    if quotes_only and stock_api_only:
        raise ValueError('两种单源更新模式不能同时启用。')
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    path = Path(output) if output else ROOT / 'web/data/dashboard.json'
    previous = load(path, {})
    if previous.get('data_mode') == 'demo':
        raise ValueError('演示数据不能进入生产采集与训练。')
    config = load(ROOT/'config/settings.json', {})
    config['network_enabled'] = not offline and config.get('network_enabled', True)
    config['enable_mlp'] = enable_mlp or config.get('enable_mlp', False)
    config['market_session'] = market_session(now)
    if bootstrap:
        config['stock_api'] = {**config.get('stock_api', {}), 'max_index_calls': 2,
                               'max_member_calls': 6, 'max_sw_history_calls': 8}
    if quotes_only:
        phase = config['market_session']['phase']
        capture_end = (phase == 'midday_break' and now.hour == 11 and now.minute < 35) or (phase == 'post_close' and now.hour == 15 and now.minute < 5)
        if phase not in {'morning', 'afternoon', 'opening_auction', 'auction_complete', 'closing_auction'} and not capture_end:
            print(json.dumps({'status':'skipped', 'reason':'非盘中快照时段，不请求行情源'}, ensure_ascii=False))
            return previous
    # Actual runtime snapshots are regenerated; uncollected fields remain null.
    if (quotes_only or stock_api_only) and previous:
        data = deepcopy(previous)
    else:
        daily_config = {**config, 'network_enabled': False} if quotes_only or stock_api_only else config
        data = fetch_data(previous, daily_config, now)
        data['price_observations'] = previous.get('price_observations', [])
        for key in ('intraday', 'intraday_state'):
            data[key] = deepcopy(previous.get(key, {}))
        # Preserve provider cooldowns and latest quotes across full refreshes.
        data['sources'].extend(deepcopy([s for s in previous.get('sources', []) if s.get('type') == 'intraday']))
        data['stock_api_state'] = deepcopy(previous.get('stock_api_state', {}))
        data['sources'].extend(deepcopy([s for s in previous.get('sources', []) if s.get('provider') == 'stock_api' or s.get('id') == 'stock_api']))
    # The primary service owns daily history, taxonomy, valuation and optional RT.
    # Quote-only runs ask only the two bounded RT endpoints, never daily/news APIs.
    if not quotes_only or config.get('primary_provider') == 'stock_api':
        api_config = {**config, 'stock_api': {**config.get('stock_api', {}), 'quotes_only': quotes_only}}
        data = enrich_with_stock_api(data, api_config, now)
    if not quotes_only and not stock_api_only:
        data = enrich_with_fallback(data, config, now)
    started_at = now
    quote_clock = datetime.now(ZoneInfo('Asia/Shanghai'))
    # Recompute cached quote freshness even when this mode forbids quote HTTP.
    quote_config = {**config, 'network_enabled': False} if stock_api_only else config
    data = collect_intraday(data, quote_config, quote_clock)
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    # Use batch completion as a conservative upper bound for newly available data.
    starts = {started_at.isoformat(), started_at.isoformat(timespec='seconds')}
    time_fields = {'as_of','first_seen_at','fetched_at','available_at','inference_completed_at','last_success','history_updated_at','constituents_updated_at','observed_at'}
    def fix_availability(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in time_fields and isinstance(item, str) and item in starts:
                    value[key] = now.isoformat()
                else:
                    fix_availability(item)
        elif isinstance(value, list):
            for item in value:
                fix_availability(item)
    fix_availability(data)
    dashboard = {**previous, **data, 'schema_version':1, 'data_mode':'live',
        'generated_at':now.isoformat(), 'market_session':market_session(now),
        'notices':['定时任务可能延迟，请以各条数据的时间戳为准。',
                   '免费源覆盖不完整；研究输出不构成交易指令。',
                   '板块指数与总市值分开展示；主题重叠，市值不可相加。']}
    dashboard = validate_prices(dashboard, now=now)
    dashboard = compute_metrics(dashboard, config)
    sentiment_config = {**config, 'sentiment': {**config.get('sentiment', {}), 'model_enabled': False, 'reuse_only': True}} if quotes_only or offline else config
    dashboard = aggregate_entity_sentiment(dashboard, now=now, config=sentiment_config)
    dashboard.update(update_predictions(dashboard, previous=previous, config=config, now=now))
    dashboard['daily_review'] = daily_review(dashboard, now)
    dashboard['status'] = 'ready' if any(m.get('price') is not None for m in dashboard.get('market',[])) or any(s.get('price') is not None or s.get('proxy',{}).get('price') is not None for s in dashboard.get('sectors',[])) else ('partial' if dashboard.get('news') else 'pending')
    # Entity histories are canonical; avoid publishing the same bars twice.
    dashboard.pop('history', None)
    atomic_json(path, dashboard)
    # Daily review history is compact. Prediction ledger lives in dashboard and
    # git history, preserving original forecasts and later settlement fields.
    review_path = path.parent/'reviews.json'
    reviews = load(review_path, {})
    # Reconcile late-arriving labels against their original target date.
    changed_dates = {r.get('target_date') for r in dashboard.get('ledger', [])
                     if r.get('settled_at', '').startswith(now.date().isoformat())}
    for day in sorted(d for d in changed_dates if d and d != now.date().isoformat()):
        revised = daily_review(dashboard, now=now, target_date=day)
        if day in reviews:
            revised['sentiment'] = reviews[day].get('sentiment', revised['sentiment'])
        reviews[day] = revised
    reviews[dashboard['daily_review']['date']] = dashboard['daily_review']
    atomic_json(review_path, reviews)
    print(json.dumps({'status':dashboard['status'], 'sectors':len(dashboard.get('sectors',[])),
          'news':len(dashboard.get('news',[])), 'forecasts':len(dashboard.get('predictions',[])),
          'output':str(path)},ensure_ascii=False))
    return dashboard

def bootstrap_coverage(data):
    sectors = [s for s in data.get('sectors', []) if s.get('taxonomy') == 'SW2021' and s.get('level') == 'L1']
    return {'industries': len(sectors),
            'members_complete': sum(bool(s.get('membership_complete')) for s in sectors),
            'history_ready': sum(len(s.get('source_history', {}).get('stock_api', [])) >= 180 for s in sectors),
            'indices_ready': sum(len(s.get('source_history', {}).get('stock_api', [])) >= 180 for s in data.get('market', []))}

def bootstrap_data(rounds=8, output=None, enable_mlp=False):
    """One bounded initial fill; subsequent rounds reuse every non-primary source.

    At most eight rounds and twelve primary requests each; provider-wide cooldown
    or no coverage progress stops this operation instead of retrying indefinitely.
    """
    if not 1 <= rounds <= 8:
        raise ValueError('首次补齐轮次必须为 1—8。')
    previous_coverage = None
    data = {}
    for index in range(rounds):
        data = run(output=output, enable_mlp=enable_mlp, stock_api_only=index > 0, bootstrap=True)
        coverage = bootstrap_coverage(data)
        print(json.dumps({'bootstrap_round': index + 1, **coverage}, ensure_ascii=False), flush=True)
        if (coverage['industries'] == coverage['members_complete'] == coverage['history_ready'] == 31
                and coverage['indices_ready'] == len(data.get('market', []))):
            break
        retry = data.get('stock_api_state', {}).get('cooldown_until')
        if retry:
            try:
                if datetime.fromisoformat(retry) > datetime.now(ZoneInfo('Asia/Shanghai')):
                    break
            except (ValueError, TypeError):
                break
        if coverage == previous_coverage:
            break
        previous_coverage = coverage
    return data

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--offline', action='store_true', help='仅使用已有缓存；首次运行为空状态')
    p.add_argument('--enable-mlp', action='store_true', help='启用小型神经网络挑战模型')
    p.add_argument('--quotes-only', action='store_true', help='仅批量更新盘中快照；复用新闻与日线缓存')
    p.add_argument('--stock-api-only', action='store_true', help='只验证/更新用户行情接口，复用其他来源缓存')
    p.add_argument('--output')
    p.add_argument('--bootstrap-rounds', type=int, choices=range(1, 9), help='首次分批补齐31行业；最多8轮，每轮最多12次主源请求')
    args = p.parse_args()
    if args.bootstrap_rounds:
        if args.offline or args.quotes_only or args.stock_api_only:
            p.error('--bootstrap-rounds 不能与离线或单源模式同时使用')
        bootstrap_data(args.bootstrap_rounds, args.output, args.enable_mlp)
    else:
        run(args.offline, args.enable_mlp, args.output, args.quotes_only, args.stock_api_only)
