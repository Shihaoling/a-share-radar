"""Admin-managed, expiring pauses checked before starting background jobs."""

import json
import os
from pathlib import Path
import sys
from tempfile import mkstemp
import time


CONTROL_PATH = Path("/var/lib/ashare-radar-preview/account/schedule-control.json")
PAUSE_MINUTES = (15, 60)
PAUSABLE = {
    "ashare-market-gap-recovery@session.service": "盘后缺口审计与补采",
    "ashare-stock-minute-recovery@session.service": "盘后个股分钟补采",
    "ashare-stock-minute-recovery@midday.service": "午间个股分钟补采",
    "ashare-training-news@collect.service": "历史快讯采集",
    "ashare-training-news@label.service": "历史快讯标注",
    "ashare-training-news@export.service": "历史数据集导出",
    "ashare-research-report-summary.service": "研报摘要",
    "ashare-announcement-analysis.service": "公告解读",
}


class ControlConflict(Exception):
    """Another administrator updated the policy after the caller read it."""


def read_control(path=CONTROL_PATH, *, now=None):
    try:
        data = json.loads(Path(path).read_bytes())
    except FileNotFoundError:
        return {"revision": 0, "pauses": {}}
    if not isinstance(data, dict):
        raise ValueError("invalid schedule control file")
    revision, pauses = data.get("revision"), data.get("pauses")
    if type(revision) is not int or revision < 0 or not isinstance(pauses, dict):
        raise ValueError("invalid schedule control fields")
    if any(task not in PAUSABLE or type(end) is not int or end < 0 for task, end in pauses.items()):
        raise ValueError("invalid schedule pause")
    now = time.time() if now is None else now
    return {"revision": revision, "pauses": {task: end for task, end in pauses.items() if end > now}}


def update_control(path, task_id, minutes, revision, *, now=None):
    if not isinstance(task_id, str) or task_id not in PAUSABLE:
        raise ValueError("unknown or non-pausable task")
    if type(minutes) is not int or minutes not in (0, *PAUSE_MINUTES):
        raise ValueError("invalid pause duration")
    if type(revision) is not int or revision < 0:
        raise ValueError("invalid revision")
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        now = int(time.time() if now is None else now)
        policy = read_control(path, now=now)
        if revision != policy["revision"]:
            raise ControlConflict
        if minutes:
            policy["pauses"][task_id] = now + minutes * 60
        elif policy["pauses"].pop(task_id, None) is None:
            return policy
        policy["revision"] += 1
        descriptor, filename = mkstemp(dir=path.parent)
        temporary = Path(filename)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(policy, stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return policy


def check_start(task_id, path=CONTROL_PATH, *, now=None):
    # Systemd also permits manually starting other instances of these templates.
    if task_id not in PAUSABLE:
        return 0
    try:
        end = read_control(path, now=now)["pauses"].get(task_id)
    except (OSError, ValueError) as error:
        print(f"schedule control unavailable for {task_id}: {type(error).__name__}", file=sys.stderr)
        return 255  # A broken policy is a service failure, distinct from an intentional skip.
    if end is not None:
        print(f"schedule paused: {task_id} until {end}", flush=True)
        return 1  # ExecCondition skips just this activation.
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "check":
        sys.exit(255)
    sys.exit(check_start(sys.argv[2]))
