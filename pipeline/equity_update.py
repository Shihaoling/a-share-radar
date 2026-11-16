"""One bounded equity bootstrap, or a cached research/fund refresh.

This command only contacts the configured stock service. Credentials are read
from the environment and are never written to the published data or reports.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

from pipeline.equity_data import collect_equity_history
from pipeline.equity_model import update_equity_predictions
from pipeline.funds import enrich_funds
from pipeline.publish import publish_view, health_summary
from pipeline.run import atomic_json, load

ROOT = Path(__file__).resolve().parents[1]


def refresh(days=180, max_requests=180, offline=False, train_pilot=False,
            bootstrap_if_needed=False):
    root = ROOT / 'data/equity'
    marker = root / 'bootstrap-complete.json'
    if bootstrap_if_needed and marker.exists():
        print(json.dumps({'status': 'bootstrap_already_complete'}))
        return
    dashboard_path = ROOT / 'web/data/dashboard.json'
    dashboard = load(dashboard_path, {})
    if dashboard.get('data_mode') == 'demo':
        raise ValueError('Demonstration data cannot enter equity training.')
    settings = load(ROOT / 'config/settings.json', {})
    settings['network_enabled'] = not offline
    settings['equity'] = {**settings.get('equity', {}), 'history_days': days, 'progress': True}
    dashboard['equity_collection'] = collect_equity_history(
        dashboard, settings, max_requests=max_requests, output_dir=root)
    dashboard = enrich_funds(dashboard, settings, datetime.now(ZoneInfo('Asia/Shanghai')))
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    dashboard = update_equity_predictions(dashboard, root, root / 'models', now, settings)
    # A pilot exercises both neural candidates on real observations. Its report
    # remains distinct from the full-universe portable baseline used by the site.
    collection = dashboard['equity_collection']
    if train_pilot and collection.get('missing_days') == 0:
        command = [sys.executable, '-m', 'pipeline.train_equity',
                   '--history-dir', str(root / 'daily'), '--model-dir', str(root / 'models'),
                   '--models', 'ridge,gru,tcn', '--device', 'cpu', '--max-stocks', '256',
                   '--epochs', '4', '--cpu-threads', '4']
        completed = subprocess.run(command, check=False)
        dashboard['equity_pilot_run'] = {'status': 'completed' if completed.returncode == 0 else 'failed',
            'device': 'CPU', 'max_stocks': 256, 'max_epochs': 4,
            'note': '云端初步试训；并未使用用户本机GPU，结果不代表完成充分调优。'}
        now = datetime.now(ZoneInfo('Asia/Shanghai'))
        dashboard = update_equity_predictions(dashboard, root, root / 'models', now, settings)
    if collection.get('missing_days') == 0 and collection.get('requested_days') == days:
        atomic_json(marker, {'completed_at': now.isoformat(), 'requested_days': days,
            'available_days': collection.get('cached_days', 0) + collection.get('collected_days', 0),
            'note': '完成首次批量采集；历史成员身份及退市样本未完整核实。'})
    dashboard['generated_at'] = now.isoformat()
    atomic_json(dashboard_path, dashboard)
    publish_view(dashboard, ROOT / 'web/data/view.json')
    atomic_json(ROOT / 'web/data/health.json', health_summary(dashboard))
    print(json.dumps({'collection': collection,
        'funds': dashboard.get('fund_summary'),
        'equity_status': dashboard.get('equity_forecast', {}).get('status'),
        'coverage': dashboard.get('equity_forecast', {}).get('coverage')}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days', type=int, default=180)
    parser.add_argument('--max-requests', type=int, default=180)
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--train-pilot', action='store_true')
    parser.add_argument('--bootstrap-if-needed', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.days <= 1500 or not 0 <= args.max_requests <= 180:
        parser.error('days must be 1..1500 and each run may make at most 180 history requests')
    refresh(args.days, args.max_requests, args.offline, args.train_pilot, args.bootstrap_if_needed)


if __name__ == '__main__':
    main()
