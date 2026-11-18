"""Fixed user-authorized data destination, token channels and shared rate gate."""
import hashlib,hmac,json,os,time
from contextlib import contextmanager
from pathlib import Path

ENDPOINT='http://tus.sxdata.top/tu_share/'
REGULAR_APIS=frozenset({'trade_cal','daily','daily_basic','index_daily','index_classify','sw_daily','index_member_all','index_basic','income','income_vip','balancesheet','balancesheet_vip','cashflow','cashflow_vip','fina_indicator','forecast','express','moneyflow','moneyflow_ind_ths','fund_basic','fund_nav','fund_daily','etf_basic','etf_share_size','cn_cpi','cn_gdp','cyq_perf'})

def base_token(token):return token.strip().removeprefix('backup15000_').strip()
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
@contextmanager
def _edit_gate():
    path = _gate_path()
    if path is None:
        yield {}
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # The worker's network lock is separate. Only serialize the brief file update.
    with path.with_suffix('.update.lock').open('a+b') as lock:
        if os.name == 'nt':
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        state = _read()
        yield state
        _save(state)


REALTIME_BATCHES = frozenset({"rt_idx_k", "rt_k"})


def gate_state(api, min_interval=6):
    state = _read()
    now = time.time()
    host_until = state.get('host_until', 0)
    api_until = state.get('apis', {}).get(api, 0)
    reason = (state.get('host_code') if host_until >= api_until
              else state.get('api_codes', {}).get(api))
    # A news, history or legacy request on either side keeps its original spacing.
    gap = 5 if (min_interval <= 5 and api in REALTIME_BATCHES
                and state.get('last_api') in REALTIME_BATCHES) else 6
    return max(0, max(host_until, api_until)-now), max(0, gap-(now-state.get('last', 0))), reason or 'rate_limited'


def gate_delay(api):
    return gate_state(api)[:2]


def sent(api=None):
    with _edit_gate() as state:
        state['last'] = time.time()
        state['last_api'] = api


def failed(api, seconds=60, host=False, *, code='rate_limited'):
    until = time.time() + max(60, seconds)
    with _edit_gate() as state:
        deadlines = state.setdefault('apis', {})
        if until >= deadlines.get(api, 0):
            deadlines[api] = until
            state.setdefault('api_codes', {})[api] = code
        if host and until >= state.get('host_until', 0):
            state.update(host_until=until, host_code=code)
