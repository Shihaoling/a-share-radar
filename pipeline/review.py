"""Daily evaluation. Targets, issuance, sentiment labels are separate populations."""
from collections import Counter, defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
from statistics import mean

TZ = ZoneInfo('Asia/Shanghai')

def local_date(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return (dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)).date().isoformat()
    except (ValueError, TypeError):
        return str(value)[:10] if len(str(value)) >= 10 else None

def finite(value):
    from math import isfinite
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)

def summarize(rows):
    errors = [abs(r['error_pct']) for r in rows if finite(r.get('error_pct'))]
    base = [abs(r['baseline_error_pct']) for r in rows if finite(r.get('baseline_error_pct'))]
    directions = [float(r['direction_correct']) for r in rows if isinstance(r.get('direction_correct'), bool)]
    mae = mean(errors) if errors else None
    baseline = mean(base) if base else None
    return {'count': len(rows), 'direction_accuracy': mean(directions) if directions else None,
            'mae_pct': mae, 'baseline_mae_pct': baseline,
            'skill_pct': (1 - mae / baseline) * 100 if mae is not None and baseline and len(base) == len(errors) else None}

def daily_review(dashboard, now=None, target_date=None):
    now = now or datetime.now(TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    day = target_date or now.astimezone(TZ).date().isoformat()
    ledger = dashboard.get('ledger', [])
    # One fixed latest forecast per instrument/task/target date. Earlier intraday
    # forecasts stay in the ledger; they do not artificially enlarge headline n.
    groups = {}
    for row in ledger:
        if row.get('target_date') != day:
            continue
        key = (row.get('sector_id'), row.get('target_kind'), row.get('target_date'))
        if key not in groups or (row.get('issued_at') or '') > (groups[key].get('issued_at') or ''):
            groups[key] = row
    evaluated = list(groups.values())
    settled = [r for r in evaluated if finite(r.get('actual_price')) and finite(r.get('error_pct'))]
    issued_ids = {r.get('id') for r in ledger if local_date(r.get('issued_at')) == day}
    abstained = [r for r in dashboard.get('predictions', []) if r.get('status') in ('pending', 'abstained', 'unavailable')]
    by_sector = defaultdict(list)
    by_task = defaultdict(list)
    for row in settled:
        by_sector[row.get('sector_id')].append(row)
        by_task[row.get('target_kind')].append(row)
    news = [n for n in dashboard.get('news', []) if local_date(n.get('first_seen_at')) == day]
    trusted = [n for n in news if n.get('credibility') != 'rumor']
    counts = Counter(n.get('sentiment', 'uncertain') for n in trusted)
    # Alignment is available only for pre-open news and FINAL matching daily bars;
    # it is a descriptive association, never a verified sentiment label accuracy.
    alignment = []
    if now.astimezone(TZ).date().isoformat() > day or now.astimezone(TZ).strftime('%H:%M') >= '15:15':
        for sector in dashboard.get('sectors', []):
            votes = []
            for n in trusted:
                if sector['id'] not in n.get('sector_ids', []):
                    continue
                try:
                    seen = datetime.fromisoformat(n['first_seen_at'].replace('Z', '+00:00')).astimezone(TZ)
                    if seen.strftime('%H:%M') >= '09:25':
                        continue
                except (KeyError, ValueError, TypeError):
                    continue
                if n.get('sentiment') in ('positive', 'negative'):
                    votes.append(1 if n['sentiment'] == 'positive' else -1)
            from pipeline.model import _history, DEFAULTS
            from pipeline.calendar import previous_trading_day
            preceding = previous_trading_day(day)
            bars = {b['date']: b for b in _history(sector, now, DEFAULTS)}
            if not votes or not sum(votes) or preceding is None or day not in bars or preceding.isoformat() not in bars:
                continue
            close, prev = bars[day].get('close'), bars[preceding.isoformat()].get('close')
            if not finite(close) or not finite(prev) or prev <= 0 or close == prev:
                continue
            alignment.append((sum(votes) > 0) == (close > prev))
    summary = summarize(settled)
    return {'date': day, 'evaluated_at':now.isoformat(), 'timezone': 'Asia/Shanghai', 'issued_count': len(issued_ids),
            'settled_count': len(settled), 'pending_count': len(evaluated)-len(settled),
            'abstained_count': len(abstained), **{k:v for k,v in summary.items() if k != 'count'},
            'evaluation_cohort': '目标日为今天；每标的、每任务取最后一条预测。不同任务分列；不等同独立交易日样本。',
            'by_sector': [{'sector_id': k, 'name': v[0].get('sector_name', k), **summarize(v)} for k,v in by_sector.items()],
            'by_task': [{'target_kind': k, **summarize(v)} for k,v in by_task.items()],
            'sentiment': {**{k:counts[k] for k in ('positive','negative','neutral','uncertain')},
                'rumor_count': len(news)-len(trusted), 'alignment_count':len(alignment),
                'alignment_rate':mean(alignment) if alignment else None,
                'method':'lexicon_rules', 'label_accuracy':None,
                'note':'仅统计今日首次采集消息；传言排除。走势一致性仅比较盘前语气与当日最终涨跌，并非情绪标签准确率或因果检验；人工真值尚未建立。'}}
