"""The operator's SXData budget, shared by all scheduled website jobs."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile

MAX_REQUESTS = 100
BEIJING = timezone(timedelta(hours=8))
RELAY_EXPIRES_AT = "2026-12-20T22:06:00+08:00"


def policy_path():
    configured = os.getenv("RADAR_REQUEST_POLICY_FILE")
    return Path(configured) if configured else Path("/var/lib/ashare-radar-preview/account/api-policy.json")


def read_policy():
    try:
        data = json.loads(policy_path().read_bytes())
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("invalid request policy")
    limit = data.get("limit_60s", MAX_REQUESTS)
    if type(limit) is not int or not 1 <= limit <= MAX_REQUESTS:
        raise ValueError("limit_60s must be an integer between 1 and 100")
    return {"limit_60s": limit, "maximum": MAX_REQUESTS,
            "updated_at": data.get("updated_at"), "expires_at": RELAY_EXPIRES_AT}


def set_limit(limit):
    if type(limit) is not int or not 1 <= limit <= MAX_REQUESTS:
        raise ValueError("limit_60s must be an integer between 1 and 100")
    path = policy_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    policy = {"limit_60s": limit, "updated_at": datetime.now(BEIJING).isoformat(timespec="seconds")}
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        scratch = Path(stream.name)
        try:
            json.dump(policy, stream)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(scratch, path)
        finally:
            scratch.unlink(missing_ok=True)
    return {**policy, "maximum": MAX_REQUESTS, "expires_at": RELAY_EXPIRES_AT}
