"""Fixed user-authorized data destination, token channels and shared rate gate."""
import hashlib,hmac,json,math,os,sys,time
from contextlib import contextmanager
from pathlib import Path
from .request_policy import MAX_REQUESTS, read_policy

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
    path = _gate_path()
    if path is None:
        return {}
    try:
        state = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(state, dict):
        raise ValueError("invalid shared gate")
    for key in ("last", "host_until", "last_accounted"):
        stamp = state.get(key, 0)
        if type(stamp) not in (int, float) or not math.isfinite(stamp):
            raise ValueError("invalid gate timestamp")
    for key in ("apis", "api_codes", "usage"):
        if key in state and not isinstance(state[key], dict):
            raise ValueError("invalid gate mapping")
    if any(type(t) not in (int, float) or not math.isfinite(t) for t in state.get("apis", {}).values()):
        raise ValueError("invalid API cooldown")
    for key in ("request_window", "recent_rt_429"):
        stamps = state.get(key, [])
        if not isinstance(stamps, list) or any(type(t) not in (int, float) or not math.isfinite(t) for t in stamps):
            raise ValueError("invalid gate window")
    return state


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


HOST_LIMIT = MAX_REQUESTS
EVENT_LIMIT = 64
METERED_APIS = REGULAR_APIS | REALTIME_BATCHES | {
    "rt_sw_k", "news", "major_news", "cctv_news", "anns_d", "idx_mins",
    "sw_mins", "stk_mins", "index_weight", "index_dailybasic",
    "ths_hot", "ths_daily", "ths_member", "stock_basic",
}


def _window(state, now):
    # Keep future timestamps on clock rollback. Forget a start exactly at t-60.
    starts = state.get("request_window", [])
    starts = [t for t in starts if t > now - 60]
    # A still-running old worker writes last/last_api under the same locks.
    last = state.get("last", 0)
    legacy = last > now - 60 and last > state.get("last_accounted", 0)
    if legacy:
        starts.append(last)
    return sorted(starts), legacy


def _realtime_spacing(state, now):
    hits = sum(stamp > now - 600 for stamp in state.get("recent_rt_429", []))
    return 15 if hits >= 3 else 10 if hits >= 2 else 5


def realtime_min_gap():
    """Read the current realtime pacing tier without updating the shared gate."""
    try:
        return _realtime_spacing(_read(), time.time())
    except (OSError, ValueError, TypeError):
        return 15


def _gate_state(state, api, min_interval, now, starts):
    host_until = state.get('host_until', 0)
    api_until = state.get('apis', {}).get(api, 0)
    reason = (state.get('host_code') if host_until >= api_until
              else state.get('api_codes', {}).get(api))
    deadline = max(host_until, api_until)
    limit = min(HOST_LIMIT, read_policy()["limit_60s"])
    if len(starts) >= limit:
        quota_until = starts[-limit] + 60
        if quota_until > deadline:
            deadline, reason = quota_until, "local_rate_limit"
    consecutive_quotes = api in REALTIME_BATCHES and state.get('last_api') in REALTIME_BATCHES
    gap = 5 if consecutive_quotes and min_interval <= 5 else 6
    if consecutive_quotes:
        gap = max(gap, _realtime_spacing(state, now))
    return max(0, deadline-now), max(0, gap-(now-state.get('last', 0))), reason or 'rate_limited'


def gate_state(api, min_interval=6):
    try:
        state = _read()
        now = time.time()
        return _gate_state(state, api, min_interval, now, _window(state, now)[0])
    except (OSError, ValueError, TypeError):
        return 60, 0, "gate_unavailable"


def gate_delay(api):
    return gate_state(api)[:2]


def _counter(state, api, now):
    totals = state.setdefault("usage", {"since": now, "peak_60s": 0, "apis": {}, "recent": []})
    label = api if api in METERED_APIS else "other"
    counts = totals["apis"].setdefault(label, {"starts": 0, "legacy": 0, "outcomes": {}, "deferred": {}})
    return totals, counts, label


def _increment(counts, key):
    counts[key] = counts.get(key, 0) + 1


def _worker():
    name = Path(sys.argv[0]).stem
    return name if name in {"cloud_news", "training_news", "relay_quotes", "market_daily", "market_reference",
                            "minute_recovery", "cloud_research", "cloud_models", "service_rollout",
                            "probe_stock_api", "sector_directory", "asset_research", "run"} else "other"


def _add_event(state, api, stamp, outcome, size):
    totals, counts, label = _counter(state, api, stamp)
    counts["starts"] += 1
    totals["peak_60s"] = max(totals["peak_60s"], size)
    seq = totals["sequence"] = totals.get("sequence", 0) + 1
    totals["recent"].append({"id": seq, "api": label, "started_at": stamp, "outcome": outcome,
                             "worker": "legacy" if outcome == "legacy_unobserved" else _worker()})
    del totals["recent"][:-EVENT_LIMIT]
    if outcome == "legacy_unobserved":
        counts["legacy"] += 1
    return seq


def reserve_request(api, min_interval, remaining):
    """Check cooldown, host cap and gap, and reserve the actual start under one lock."""
    if _gate_path() is None:
        return 0, 0, "budget_exhausted" if remaining <= 0 else None, None
    with _edit_gate() as state:
        now = time.time()
        starts, legacy = _window(state, now)
        state["request_window"] = starts
        if legacy:
            _add_event(state, state.get("last_api"), state["last"], "legacy_unobserved", len(starts))
            state["last_accounted"] = state["last"]
        cooldown, gap, reason = _gate_state(state, api, min_interval, now, starts)
        adaptive = (api in REALTIME_BATCHES and state.get("last_api") in REALTIME_BATCHES
                    and _realtime_spacing(state, now) > 6)
        # An early quote can yield its network lock to news and background work.
        # Allow sub-second scheduler jitter to resolve within a healthy slot.
        yield_quote = api in REALTIME_BATCHES and gap >= 1
        if cooldown or yield_quote or gap >= remaining or remaining <= 0:
            if not cooldown:
                if yield_quote or (adaptive and gap > 0):
                    reason = "adaptive_spacing" if adaptive else "shared_spacing"
                    cooldown = gap
                else:
                    reason = "budget_exhausted"
                    cooldown = 0
            _, counts, _ = _counter(state, api, now)
            _increment(counts["deferred"], reason)
            counts["last_deferred_at"] = now
            return cooldown, 0, reason, None
        if gap:
            return 0, gap, None, None
        starts.append(now)
        state.update(last=now, last_api=api, last_accounted=now)
        return 0, 0, None, _add_event(state, api, now, "in_flight", len(starts))


def sent(api=None):
    # Compatibility for tools using the old API. Production clients call reserve_request.
    with _edit_gate() as state:
        state['last'] = time.time()
        state['last_api'] = api


def finish_request(api, event, outcome, duration_ms, http_status=None, transport=None):
    if event is None:
        return
    with _edit_gate() as state:
        now = time.time()
        totals, counts, _ = _counter(state, api, now)
        _increment(counts["outcomes"], outcome)
        if transport:
            _increment(counts.setdefault("transport", {}), transport)
        if api in REALTIME_BATCHES and outcome in ("http_429", "provider_429"):
            recent = [stamp for stamp in state.get("recent_rt_429", []) if stamp > now - 600]
            state["recent_rt_429"] = (recent + [now])[-3:]
        row = next((row for row in reversed(totals["recent"]) if row["id"] == event), None)
        if row is not None:
            row.update(finished_at=now, duration_ms=max(0, round(duration_ms)), outcome=outcome)
            if type(http_status) is int and 100 <= http_status <= 599:
                row["http_status"] = http_status
            if transport:
                row["transport"] = transport


def request_usage():
    """Read-only local diagnostics. Timestamps are Unix seconds. No credentials or bodies."""
    state, now = _read(), time.time()
    starts, pending_legacy = _window(state, now)
    data = state.get("usage", {})
    return {"limit_60s": min(HOST_LIMIT, read_policy()["limit_60s"]), "starts_last_60s": len(starts), "pending_legacy": pending_legacy,
            "realtime_min_gap": _realtime_spacing(state, now),
            "recent_rt_429": [stamp for stamp in state.get("recent_rt_429", []) if stamp > now - 600],
            "since": data.get("since"), "peak_60s": max(data.get("peak_60s", 0), len(starts)),
            "apis": data.get("apis", {}), "recent": data.get("recent", [])}


def failed(api, seconds=60, host=False, *, code='rate_limited'):
    until = time.time() + max(60, seconds)
    with _edit_gate() as state:
        deadlines = state.setdefault('apis', {})
        if until >= deadlines.get(api, 0):
            deadlines[api] = until
            state.setdefault('api_codes', {})[api] = code
        if host and until >= state.get('host_until', 0):
            state.update(host_until=until, host_code=code)
