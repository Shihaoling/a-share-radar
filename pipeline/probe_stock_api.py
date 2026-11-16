"""Small, explicit capability checks; no credentials or provider errors in output."""
import argparse
import json
import os
from datetime import timedelta
from pathlib import Path

from pipeline.calendar import shanghai_now, previous_trading_day
from pipeline.stock_api import _Client, _Failure, FIELDS


def probe(group):
    now = shanghai_now()
    day = previous_trading_day(now.date()).strftime('%Y%m%d')
    specs = {
        'core': [
            ('index_classify', {'src': 'SW2021', 'level': 'L1'}, 'index_code,industry_name,level,industry_code,is_pub,parent_code,src'),
            ('sw_daily', {'trade_date': day}, 'ts_code,trade_date,name,open,low,high,close,pct_change,vol,amount,pe,pb,float_mv,total_mv'),
            ('index_member_all', {'l1_code': '801780.SI'}, 'l1_code,l1_name,ts_code,name,in_date,out_date,is_new'),
            ('daily_basic', {'ts_code': '000001.SZ', 'trade_date': day}, 'ts_code,trade_date,close,turnover_rate,volume_ratio,pe_ttm,pb,total_mv,circ_mv'),
        ],
        'live_news': [
            ('rt_idx_k', {'ts_code': '000001.SH,399001.SZ'}, 'ts_code,name,trade_time,close,pre_close,high,open,low,vol,amount'),
            ('rt_sw_k', {'ts_code': '801780.SI'}, 'ts_code,name,trade_time,close,pre_close,high,open,low,vol,amount'),
            ('news', {'src': 'cls', 'start_date': (now-timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S'), 'end_date': now.strftime('%Y-%m-%d %H:%M:%S')}, 'datetime,title,content,channels'),
            ('index_daily', {'ts_code': '000001.SH', 'trade_date': now.strftime('%Y%m%d')}, 'ts_code,trade_date,close,open,high,low,pre_close,change,pct_chg,vol,amount'),
        ],
    }
    token = os.environ.get('STOCK_API_TOKEN')
    if not token:
        raise SystemExit('STOCK_API_TOKEN not configured')
    client = _Client(token, {'max_requests': 4, 'min_interval_seconds': 3}, now)
    output = {'checked_at': now.isoformat(), 'group': group, 'requests': 0, 'results': []}
    for api, params, fields in specs[group]:
        FIELDS[api] = fields
        row = {'api': api, 'params': params}
        try:
            records = client.request(api, params)
            row.update(status='success' if records else 'empty', row_count=len(records), fields=fields.split(','), sample=[])
            for record in records[:3]:
                row['sample'].append({key: (value[:160] if isinstance(value, str) else value) for key, value in record.items() if key != 'content'})
        except _Failure as exc:
            row.update(status='unavailable', error_code=exc.code)
            output['results'].append(row)
            if exc.code in ('rate_limited', 'authorization_denied', 'budget_exhausted'):
                break
            continue
        output['results'].append(row)
    output['requests'] = client.count
    path = Path('web/data/tushare-capabilities.json')
    previous = json.loads(path.read_text()) if path.exists() else {}
    previous[group] = output
    path.write_text(json.dumps(previous, ensure_ascii=False, indent=2, allow_nan=False).replace(token, '[REDACTED]')+'\n')
    print(json.dumps(output, ensure_ascii=False).replace(token, '[REDACTED]'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--group', choices=('core', 'live_news'), required=True)
    probe(parser.parse_args().group)
