"""Read-only telemetry for the private scheduler console."""

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time

from pipeline.data_service import request_usage
from schedule_control import PAUSABLE


BEIJING = timezone(timedelta(hours=8))
CACHE_SECONDS = 15
ROWS = (
    # A missing third field uses the timer with the same basename.
    ("preview-relay", "实时行情", None),
    ("preview-news@collect", "快讯采集"),
    ("preview-news@analyze", "快讯分析", "ashare-preview-news.path"),
    ("minute-recovery", "指数分钟补采"),
    ("market-gap-recovery@session", "盘后缺口审计与补采", "ashare-market-gap-recovery.timer"),
    ("stock-minute-recovery@session", "盘后个股及板块分钟补采", "ashare-market-gap-recovery.timer"),
    ("stock-minute-recovery@midday", "午间个股及板块分钟补采", "ashare-stock-minute-midday.timer"),
    ("company-announcements", "上市公司公告"),
    ("announcement-analysis", "公告分析"),
    ("announcement-digest", "公告摘要"),
    ("research-report-collect", "研报目录采集"),
    ("research-report-summary", "研报摘要"),
    ("preview-daily", "盘后日线"),
    ("preview-model", "行情观察样本"),
    ("preview-quotes", "备用行情快照"),
    ("preview-reference", "行业权重及基础数据"),
    ("cloud-research", "盘后研究与结算"),
    ("market-insights", "市场热点"),
    ("market-reading", "市场简评"),
    ("trend-observations", "趋势观察"),
    ("trend-brief", "趋势简报"),
    ("economic-calendar", "经济日历"),
    ("global-indices", "海外指数日线"),
    ("global-intraday", "海外指数分时"),
    ("weekend-brief", "休市简报"),
    ("training-news@collect", "历史快讯采集", "ashare-training-collect.timer"),
    ("training-news@label", "历史快讯标注", "ashare-training-label.timer"),
    ("training-news@export", "训练数据导出", "ashare-training-export.timer"),
)
PROPERTIES = ",".join((
    "Id", "LoadState", "ActiveState", "SubState", "Result", "ExecMainStatus",
    "ExecMainStartTimestamp", "ExecMainExitTimestamp", "ExecMainStartTimestampMonotonic",
    "ExecMainExitTimestampMonotonic", "ConditionResult", "ConditionTimestamp",
    "InactiveExitTimestamp", "InactiveEnterTimestamp", "LastTriggerUSec",
    "TimersCalendar", "TimersMonotonic", "RandomizedDelayUSec",
))
STARTING = "7d4958e842da4a758f6c1cdc7b36dcc5"
FINISHED = "7ad2d189f7e94e70a38c781354912448"
FAILED = "be02cf6855d2428ba40df7e9d022f03d"
UNIT_FAILED = "d9b373ed55a64feb8242e02dbe79a49c"
CACHE_LOCK = threading.Lock()
_latest = None
_sampled = None
_refreshing = False


def iso(stamp):
    if not stamp or not 0 < stamp < 32_503_680_000:
        return None
    return datetime.fromtimestamp(stamp, BEIJING).isoformat(timespec="seconds")


def system_time(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%a %Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def read_command(args, partial=False):
    run = subprocess.run(args, capture_output=True, text=True, timeout=4,
                         env={**os.environ, "TZ": "UTC", "LC_ALL": "C", "SYSTEMD_COLORS": "0"})
    if run.returncode and not (partial and run.stdout):
        raise OSError(f"{args[0]} exited {run.returncode}")
    return run.stdout


def configurations():
    tasks = {}
    for name, label, *custom in ROWS:
        unit = f"ashare-{name}.service"
        trigger = custom[0] if custom else f"ashare-{name}.timer"
        tasks[unit] = (PAUSABLE.get(unit, label), trigger)
    return tasks


def discover_timers(tasks):
    data = json.loads(read_command(["systemctl", "list-timers", "--all", "--no-pager",
                                    "--output=json", "ashare-*.timer"]))
    if not isinstance(data, list):
        raise ValueError("invalid timer inventory")
    times = {}
    for row in data:
        unit, timer = row.get("activates"), row.get("unit")
        if not all(isinstance(v, str) for v in (unit, timer)):
            continue
        if not re.fullmatch(r"ashare-[a-zA-Z0-9_@.-]{1,100}\.service", unit):
            continue
        if not re.fullmatch(r"ashare-[a-zA-Z0-9_@.-]{1,100}\.timer", timer):
            continue
        if unit not in tasks:
            tasks[unit] = (unit, timer)
        stamp = row.get("next")
        if isinstance(stamp, int):
            times[timer] = iso(stamp / 1e6)
    return times


def unit_properties(tasks):
    names = set(tasks) | {trigger for label, trigger in tasks.values() if trigger}
    raw = read_command(["systemctl", "show", "--no-pager", "-p", PROPERTIES, *sorted(names)], partial=True)
    props = {}
    for group in raw.strip().split("\n\n"):
        row = {}
        for line in group.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                if key in {"TimersCalendar", "TimersMonotonic"}:
                    row.setdefault(key, []).append(value)
                else:
                    row[key] = value
        if row.get("Id") in names:
            props[row["Id"]] = row
    if not props:
        raise ValueError("missing unit properties")
    return props


def safe_code(value):
    return value if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,72}", value) else None


def timer_schedule(trigger, props):
    if trigger == "ashare-preview-news.path" and props.get("LoadState") == "loaded":
        return "有待分析快讯时触发"
    periods = []
    for value in props.get("TimersCalendar", []):
        for expr in re.findall(r"OnCalendar=([^;}]+)", value):
            expr = expr.strip().removesuffix(" Asia/Shanghai")
            expr = expr.replace("Mon..Fri *-*-* ", "工作日 ").replace("*-*-* ", "每日 ")
            expr = re.sub(r"\*:\*:(\d\d)\b", r"每分钟第\1秒", expr)
            expr = re.sub(r"\*:00/(\d+):(\d\d)\b", r"每\1分钟第\2秒", expr)
            periods.append(expr)
    monikers = {"OnUnitInactiveUSec": "上轮结束后", "OnUnitActiveUSec": "上轮启动后",
                "OnBootUSec": "开机后", "OnActiveUSec": "定时器启用后", "OnStartupUSec": "系统管理器启动后"}
    for value in props.get("TimersMonotonic", []):
        for kind, delay in re.findall(r"\b(On\w+USec)=([^;}]+)", value):
            periods.append(f"{monikers.get(kind, kind)} {delay.strip()}")
    jitter = props.get("RandomizedDelayUSec")
    if periods and jitter and jitter not in {"0", "0s", "0us"}:
        periods.append(f"随机延后最多 {jitter}")
    return " / ".join(dict.fromkeys(periods)) or None


def quote_schedule(profile):
    from quote_schedule import replay
    settings = json.loads(profile.read_text(encoding="utf-8"))
    # Replay a known trading minute using the same admission code as the live relay.
    # This describes nominal slots. Cooldowns and shared spacing can still defer a send.
    slots = replay(settings, datetime(2026, 11, 24, 10, 0, tzinfo=BEIJING), 60)["slots"]
    if not settings.get("staggered_minutes"):
        return f"盘中每 {settings['slot_seconds']} 秒检查时隙"
    parts = []
    for batch, label in (("rt_idx_k", "指数"), ("rt_k", "个股")):
        seconds = [datetime.fromisoformat(slot["time"]).strftime("%S") for slot in slots if slot["batch"] == batch]
        if seconds:
            parts.append(f"{label}第{'、'.join(seconds)}秒")
    return ("盘中每分钟 · " + " / ".join(parts)) if parts else None


def history(tasks):
    # Limit the query to PID 1 and site units. Only fixed outcome codes ever leave this process.
    command = ["journalctl", "-q", "--no-pager", "-o", "json",
               "--output-fields=UNIT,MESSAGE_ID,MESSAGE,JOB_RESULT,UNIT_RESULT,SERVICE_RESULT",
               "_PID=1", *["UNIT=" + unit for unit in tasks]]
    recent = read_command([*command, "--since=-2h", "-n", "240"])
    # Frequent successful minute jobs would otherwise displace the last real failures.
    failures = read_command([*command, "MESSAGE_ID=" + FAILED, "MESSAGE_ID=" + UNIT_FAILED,
                             "--since=-24h", "-n", "60"])
    by_stamp = {}
    recent_keys = set()
    lines = recent.splitlines()
    n_recent = len(lines)
    for index, line in enumerate(lines + failures.splitlines()):
        if not line.strip():
            continue
        row = json.loads(line)
        unit = row.get("UNIT")
        if unit not in tasks:
            continue
        ident = row.get("MESSAGE_ID")
        message = row.get("MESSAGE") or ""
        reason = None
        if (row.get("UNIT_RESULT") == "exec-condition" or row.get("JOB_RESULT") == "skipped"
                or isinstance(message, str) and "skipped" in message.lower()):
            status, reason = "skipped", "condition"
        elif ident in {FAILED, UNIT_FAILED}:
            status = "failed"
            reason = safe_code(row.get("UNIT_RESULT") or row.get("SERVICE_RESULT"))
            if not reason and isinstance(message, str):
                match = re.search(r"Failed with result '([a-z-]+)'", message)
                reason = match.group(1) if match else None
        elif ident == STARTING:
            status = "running"
        elif ident == FINISHED:
            status = "success"
        else:
            continue
        micros = int(row["__REALTIME_TIMESTAMP"])
        by_stamp[(micros, unit)] = dict(at=iso(micros / 1e6), task_id=unit, status=status, reason=reason)
        if index < n_recent:
            recent_keys.add((micros, unit))
    # A condition skip can produce both a service and a job-level manager message.
    packed = []
    last = {}
    for (micros, unit), event in sorted(by_stamp.items()):
        event["recent"] = (micros, unit) in recent_keys
        prev = last.get(unit)
        if prev and micros - prev[0] < 1_000_000:
            if prev[1] == event["status"] or (prev[1], event["status"]) == ("skipped", "success"):
                if prev[1] == event["status"] and event["reason"] and not packed[prev[2]]["reason"]:
                    packed[prev[2]]["reason"] = event["reason"]
                continue
            if (prev[1], event["status"]) == ("success", "skipped"):
                packed[prev[2]] = None
        last[unit] = (micros, event["status"], len(packed))
        packed.append(event)
    return [event for event in reversed(packed) if event]


def task_state(unit, meta, props, next_times, last_event=None):
    label, trigger = meta
    service = props.get(unit, {})
    source = props.get(trigger, {})
    active, result = service.get("ActiveState"), service.get("Result")
    start = system_time(service.get("ExecMainStartTimestamp"))
    end = system_time(service.get("ExecMainExitTimestamp"))
    condition_time = system_time(service.get("ConditionTimestamp"))
    condition_skipped = service.get("ConditionResult") == "no" and condition_time is not None
    duration = None
    if service.get("LoadState") != "loaded":
        status = "unknown"
    elif active in {"activating", "active", "reloading", "deactivating"}:
        status, end = "running", None
    elif result == "exec-condition" or condition_skipped:
        status = "skipped"
        result = "condition" if condition_skipped else result
        start = condition_time if condition_skipped else system_time(service.get("InactiveExitTimestamp"))
        end = start if condition_skipped else system_time(service.get("InactiveEnterTimestamp"))
    elif active == "failed" or result and result not in {"success", "exec-condition"}:
        status = "failed"
    elif start and end and service.get("ExecMainStatus") not in {None, "", "0"}:
        status, result = "failed", "exit-code"
    elif start and end and end >= start and result == "success":
        status = "success"
    else:
        status = "unknown"
    # systemctl can unload completed oneshots or lose their Result after reset-failed.
    if last_event and last_event["status"] in {"failed", "skipped", "success"} and status != "running":
        observed = datetime.fromisoformat(last_event["at"]).timestamp()
        if (not start and not end) or observed >= max(start or 0, end or 0):
            if status == "skipped" and last_event["status"] == "success" and observed == end:
                last_event = dict(last_event, status="skipped", reason=result)
            if not end or observed > end:
                if start is not None and observed > (end or start) + 1:
                    start = None
                end = observed
            status = last_event["status"]
            if status != "success":
                result = last_event.get("reason") or status
    if status in {"success", "failed"} and start and end and end >= start:
        a = int(service.get("ExecMainStartTimestampMonotonic") or 0)
        b = int(service.get("ExecMainExitTimestampMonotonic") or 0)
        duration = round((b - a) / 1e6, 3) if a and b >= a else end - start
    if status in {"unknown", "running"}:
        result = None
    if status == "success":
        result = "success"
    return dict(id=unit, unit=unit, label=label, timer=trigger, controllable=unit in PAUSABLE,
                active_state=active, timer_state=source.get("ActiveState"), status=status,
                last_start_at=iso(start), last_end_at=iso(end), last_result=result,
                duration_seconds=duration, next_at=next_times.get(trigger), schedule=timer_schedule(trigger, source),
                last_trigger_at=iso(system_time(source.get("LastTriggerUSec"))))


def traffic():
    gate = request_usage()
    if not gate.get("since"):
        raise ValueError("gate has no observations")
    reasons = Counter()
    for api in gate["apis"].values():
        reasons.update({key: count for key, count in api.get("deferred", {}).items()
                        if safe_code(key) and isinstance(count, int)})
    recent = [dict(at=iso(row.get("started_at")), api=safe_code(row.get("api")),
                   worker=safe_code(row.get("worker")), outcome=safe_code(row.get("outcome")),
                   duration_ms=row.get("duration_ms")) for row in reversed(gate["recent"][-64:])]
    rt_429 = len(gate["recent_rt_429"])
    return dict(since=iso(gate["since"]), limit_60s=gate["limit_60s"], starts_last_60s=gate["starts_last_60s"],
                peak_60s=gate["peak_60s"], realtime_gap_seconds=gate["realtime_min_gap"],
                rt_429_last_10m=rt_429, rt_429_capped=rt_429 >= 3,
                deferred_total=sum(reasons.values()), deferred_by_reason=dict(reasons), recent=recent)


def blank():
    return dict(schema_version=1, generated_at=None, timezone="Asia/Shanghai", stale=True,
                unavailable=["initializing"], tasks=[task_state(u, m, {}, {}) for u, m in configurations().items()],
                events=[], traffic=None)


def collect():
    tasks = configurations()
    missing = []

    def read(name, func, fallback):
        try:
            return func()
        except (OSError, subprocess.SubprocessError, ImportError, ValueError, TypeError, KeyError):
            missing.append(name)
            return fallback

    next_times = read("timers", lambda: discover_timers(tasks), {})
    props = read("services", lambda: unit_properties(tasks), {})
    events = read("journal", lambda: history(tasks), [])
    gate = read("traffic", traffic, None)
    latest = {}
    for event in events:
        if event.pop("recent"):
            latest.setdefault(event["task_id"], event)
    rows = [task_state(unit, meta, props, next_times, latest.get(unit)) for unit, meta in tasks.items()]
    profile = Path(__file__).with_name("relay-active.json")
    relay = next(row for row in rows if row["unit"] == "ashare-preview-relay.service")
    relay["schedule"] = read("quote_schedule", lambda: quote_schedule(profile), None)
    return dict(schema_version=1, generated_at=iso(time.time()), timezone="Asia/Shanghai", stale=bool(missing),
                unavailable=missing, tasks=rows, events=events[:180], traffic=gate)


def _refresh():
    global _latest, _sampled, _refreshing
    try:
        latest = collect()
    except Exception:
        # Retain the last good timestamp if a collector fails unexpectedly.
        with CACHE_LOCK:
            latest = {**(_latest or blank()), "stale": True, "unavailable": ["collector"]}
    with CACHE_LOCK:
        _latest, _sampled, _refreshing = latest, time.monotonic(), False


def snapshot():
    """Never run systemctl or journalctl on the shared account request thread."""
    global _refreshing
    with CACHE_LOCK:
        age = time.monotonic() - _sampled if _sampled is not None else float("inf")
        if age >= CACHE_SECONDS and not _refreshing:
            _refreshing = True
            threading.Thread(target=_refresh, name="schedule-telemetry", daemon=True).start()
        if _latest is None:
            return blank()
        if age > 3 * CACHE_SECONDS:
            return {**_latest, "stale": True, "unavailable": [*_latest["unavailable"], "refreshing"]}
        return dict(_latest)
