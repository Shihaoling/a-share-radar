"""Fixed user-authorized data destination, token channels and shared rate gate."""
import hashlib,hmac,json,os,time
from pathlib import Path

ENDPOINT='http://tus.sxdata.top/tu_share/'
REGULAR_APIS=frozenset({'trade_cal','daily','daily_basic','index_daily','index_classify','sw_daily','index_member_all','index_basic','income','income_vip','balancesheet','balancesheet_vip','cashflow','cashflow_vip','fina_indicator','forecast','express','moneyflow','moneyflow_ind_ths','fund_basic','fund_nav','fund_daily','etf_basic','etf_share_size','cn_cpi','cn_gdp','cyq_perf'})

def base_token(token):return token.removeprefix('backup15000_')
def credential_matches(token):
    expected=os.environ.get('RADAR_EXPECTED_STOCK_KEY_SHA256')
    return not expected or hmac.compare_digest(hashlib.sha256(base_token(token).encode()).hexdigest(),expected)
def token_for_api(token,api):
    token=base_token(token)
    return 'backup15000_'+token if api in REGULAR_APIS else token

def _gate_path():
    value=os.environ.get('RADAR_SERVICE_GATE_FILE')
    return Path(value) if value else None

def _read():
    path=_gate_path()
    if path:
        try:return json.loads(path.read_text())
        except (ValueError,OSError):pass
    return {}
def _save(state):
    path=_gate_path()
    if path:
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(state));temporary.replace(path)
def gate_delay(api):
    state=_read();now=time.time()
    return max(0,state.get('host_until',0)-now,state.get('apis',{}).get(api,0)-now),max(0,6-(now-state.get('last',0)))
def sent():
    state=_read();state['last']=time.time();_save(state)
def failed(api,seconds=60,host=False):
    state=_read();until=time.time()+max(60,seconds)
    state.setdefault('apis',{})[api]=until
    if host:state['host_until']=until
    _save(state)
