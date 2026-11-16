"""Explicit, sequential capability checks; no credentials or provider text in output.

This command is an operator-requested diagnostic, not an automatic retry loop.
Each group is bounded to seven HTTP requests, with the production response limit.
"""
import argparse
import json
import os
import time
from datetime import timedelta
from pathlib import Path

from pipeline.calendar import shanghai_now, previous_trading_day
from pipeline.stock_api import _Client, _Failure, FIELDS


AUDIT_GROUPS = ('financials', 'fund_macro', 'live_news', 'news_extended', 'premium')
GROUPS = ('core', 'live_news', 'financials', 'fund_macro', 'news_extended', 'premium', 'audit_15000')
NEWS_APIS = {'news', 'major_news', 'cctv_news', 'anns_d'}
NEWS_SAMPLE_FIELDS = {'datetime', 'pub_time', 'date', 'ann_date', 'ts_code'}
STOP_CODES = {'rate_limited', 'authorization_denied', 'budget_exhausted'}


def specifications(now):
    day = previous_trading_day(now.date()).strftime('%Y%m%d')
    period = '20241231'  # Completed historical annual period: capability control.
    stock = '600519.SH'
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).strftime('%Y-%m-%d %H:%M:%S')
    end = now.strftime('%Y-%m-%d %H:%M:%S')
    return {
        'core': [
            ('index_classify', {'src': 'SW2021', 'level': 'L1'}, 'index_code,industry_name,level,industry_code,is_pub,parent_code,src'),
            ('sw_daily', {'trade_date': day}, 'ts_code,trade_date,name,open,low,high,close,pct_change,vol,amount,pe,pb,float_mv,total_mv'),
            ('index_member_all', {'l1_code': '801780.SI'}, 'l1_code,l1_name,ts_code,name,in_date,out_date,is_new'),
            ('daily_basic', {'ts_code': '000001.SZ', 'trade_date': day}, 'ts_code,trade_date,close,turnover_rate,volume_ratio,pe_ttm,pb,total_mv,circ_mv'),
        ],
        'live_news': [
            ('rt_idx_k', {'ts_code': '000001.SH,399001.SZ'}, 'ts_code,name,trade_time,close,pre_close,high,open,low,vol,amount'),
            ('rt_sw_k', {'ts_code': '801780.SI'}, 'ts_code,name,trade_time,close,pre_close,high,open,low,vol,amount'),
            ('news', {'src': 'cls', 'start_date': start, 'end_date': end}, 'datetime'),
            ('index_daily', {'ts_code': '000001.SH', 'trade_date': now.strftime('%Y%m%d')}, 'ts_code,trade_date,close,open,high,low,pre_close,change,pct_chg,vol,amount'),
        ],
        'financials': [
            ('income', {'ts_code': stock, 'period': period, 'report_type': '1'}, 'ts_code,ann_date,f_ann_date,end_date,revenue,n_income_attr_p'),
            ('income_vip', {'ts_code': stock, 'period': period, 'report_type': '1'}, 'ts_code,ann_date,f_ann_date,end_date,revenue,n_income_attr_p'),
            ('balancesheet', {'ts_code': stock, 'period': period, 'report_type': '1'}, 'ts_code,ann_date,f_ann_date,end_date,total_assets,total_liab,total_hldr_eqy_exc_min_int'),
            ('cashflow', {'ts_code': stock, 'period': period, 'report_type': '1'}, 'ts_code,ann_date,f_ann_date,end_date,n_cashflow_act,n_cashflow_inv_act,n_cash_flows_fnc_act'),
            ('fina_indicator', {'ts_code': stock, 'period': period}, 'ts_code,ann_date,end_date,eps,roe,netprofit_yoy,debt_to_assets'),
            ('forecast', {'ts_code': stock, 'period': period}, 'ts_code,ann_date,end_date,type,p_change_min,p_change_max,net_profit_min,net_profit_max'),
            ('express', {'ts_code': '000001.SZ', 'period': period}, 'ts_code,ann_date,end_date,revenue,n_income,total_assets,total_hldr_eqy_exc_min_int'),
        ],
        'fund_macro': [
            ('fund_basic', {'market': 'E', 'status': 'L'}, 'ts_code,name,fund_type,found_date,market'),
            ('fund_nav', {'ts_code': '510300.SH', 'nav_date': day}, 'ts_code,ann_date,nav_date,unit_nav,accum_nav'),
            ('cn_cpi', {'m': '202412'}, 'month,nt_val,nt_yoy,nt_mom'),
            ('cn_gdp', {'q': '2024Q4'}, 'quarter,gdp,gdp_yoy'),
        ],
        'news_extended': [
            ('news', {'src': 'eastmoney', 'start_date': start, 'end_date': end}, 'datetime'),
            ('major_news', {'start_date': start, 'end_date': end}, 'pub_time'),
            ('cctv_news', {'date': (now-timedelta(days=1)).strftime('%Y%m%d')}, 'date'),
            ('anns_d', {'ts_code': stock, 'start_date': '20250101', 'end_date': '20250630'}, 'ts_code,ann_date'),
        ],
        'premium': [
            ('cyq_perf', {'ts_code': stock, 'trade_date': day}, 'ts_code,trade_date,weight_avg,winner_rate'),
        ],
    }


def _sample(records, api):
    samples = []
    for record in records[:3]:
        samples.append({key: (value[:160] if isinstance(value, str) else value)
                        for key, value in record.items()
                        if api not in NEWS_APIS or key in NEWS_SAMPLE_FIELDS})
    return samples


def _matches(records, params):
    """Verify observed rows honor identity/report-date filters, not just code=0."""
    mapping = {'period': 'end_date', 'm': 'month', 'q': 'quarter'}
    for record in records:
        for parameter in ('ts_code', 'period', 'trade_date', 'nav_date', 'date', 'm', 'q'):
            if parameter not in params:
                continue
            field = mapping.get(parameter, parameter)
            allowed = str(params[parameter]).split(',')
            if str(record.get(field)) not in allowed:
                return False
    return True


def _group(name, token, now):
    specs = specifications(now)[name]
    client = _Client(token, {'max_requests': min(7, len(specs)), 'min_interval_seconds': 3}, now)
    result = {'checked_at': now.isoformat(), 'group': name, 'requests': 0,
              'requested_limit': 3, 'results': [], 'stopped_reason': None}
    for api, original_params, fields in specs:
        params = {**original_params, 'limit': 3}
        prior_fields = FIELDS.get(api)
        FIELDS[api] = fields
        row = {'api': api, 'params': params}
        try:
            records = client.request(api, params)
            matched = _matches(records, params) if records else None
            row.update(status=('success' if matched else 'query_mismatch') if records else 'empty',
                       query_match=matched, row_count=len(records),
                       fields=fields.split(','), sample=_sample(records, api))
        except _Failure as exc:
            row.update(status='unavailable', error_code=exc.code)
            if exc.provider_code is not None:
                row['provider_code'] = exc.provider_code
            if exc.provider_reason is not None:
                row['provider_reason'] = exc.provider_reason
            # Do not label a generic business error as a proved permission denial.
            if exc.code in STOP_CODES or exc.provider_reason in STOP_CODES:
                result['stopped_reason'] = exc.provider_reason or exc.code
        finally:
            if prior_fields is None:
                FIELDS.pop(api, None)
            else:
                FIELDS[api] = prior_fields
        result['results'].append(row)
        if result['stopped_reason']:
            break
    result['requests'] = client.count
    return result


def _save(path, previous, token):
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(previous, ensure_ascii=False, indent=2, allow_nan=False).replace(token, '[REDACTED]') + '\n'
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(encoded, encoding='utf-8')
    os.replace(temporary, path)


def probe(group, path=None):
    if group not in GROUPS:
        raise ValueError('Unsupported capability group')
    token = os.environ.get('STOCK_API_TOKEN')
    if not token:
        raise SystemExit('STOCK_API_TOKEN not configured')
    path = Path(path) if path is not None else Path('web/data/tushare-capabilities.json')
    previous = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    if not isinstance(previous, dict):
        raise ValueError('Invalid capability state')
    groups = AUDIT_GROUPS if group == 'audit_15000' else (group,)
    output = {'checked_at': shanghai_now().isoformat(), 'group': group, 'requests': 0,
              'results': [], 'groups_completed': [], 'stopped_reason': None}
    for index, name in enumerate(groups):
        if index:
            # A group starts a fresh bounded client. Preserve the request gap.
            time.sleep(3)
        result = _group(name, token, shanghai_now())
        previous[name] = result
        output['requests'] += result['requests']
        output['results'].extend({**row, 'group': name} for row in result['results'])
        output['groups_completed'].append(name)
        output['stopped_reason'] = result['stopped_reason']
        if group == 'audit_15000':
            previous[group] = output
        _save(path, previous, token)
        if result['stopped_reason']:
            break
    if group != 'audit_15000':
        output = previous[group]
    print(json.dumps(output, ensure_ascii=False, allow_nan=False).replace(token, '[REDACTED]'))
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--group', choices=GROUPS, required=True)
    probe(parser.parse_args().group)
