"""python -m pipeline.run [--offline] [--enable-mlp] [--output PATH]."""
import argparse
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

def run(offline=False, enable_mlp=False, output=None):
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    path = Path(output) if output else ROOT / 'web/data/dashboard.json'
    previous = load(path, {})
    if previous.get('data_mode') == 'demo':
        raise ValueError('演示数据不能进入生产采集与训练。')
    config = load(ROOT/'config/settings.json', {})
    config['network_enabled'] = not offline and config.get('network_enabled', True)
    config['enable_mlp'] = enable_mlp or config.get('enable_mlp', False)
    config['market_session'] = market_session(now)
    # Actual runtime snapshots are regenerated; uncollected fields remain null.
    data = fetch_data(previous, config, now)
    data['price_observations'] = previous.get('price_observations', [])
    data = enrich_with_fallback(data, config, now)
    started_at = now
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    # Use batch completion as a conservative upper bound for newly available data.
    starts = {started_at.isoformat(), started_at.isoformat(timespec='seconds')}
    time_fields = {'as_of','first_seen_at','fetched_at','last_success','history_updated_at','constituents_updated_at','observed_at'}
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

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--offline', action='store_true', help='仅使用已有缓存；首次运行为空状态')
    p.add_argument('--enable-mlp', action='store_true', help='启用小型神经网络挑战模型')
    p.add_argument('--output')
    args = p.parse_args()
    run(args.offline, args.enable_mlp, args.output)
