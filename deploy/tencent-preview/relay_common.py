"""Serialize SXData requests from the cloud workers."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path

from pipeline.calendar import shanghai_now
from pipeline.stock_api import _Client

STATE = Path("/var/lib/ashare-radar-preview")


@contextmanager
def relay_guard():
    gate = Path(os.environ.get("RADAR_SERVICE_GATE_FILE", STATE / "relay/rate-gate.json"))
    os.environ.setdefault("RADAR_SERVICE_GATE_FILE", str(gate))
    gate.parent.mkdir(parents=True, exist_ok=True)
    with gate.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


@contextmanager
def relay_client(token, *, timeout=8, min_gap_seconds=6):
    with relay_guard():
        yield _Client(token, {"max_requests": 1, "min_interval_seconds": min_gap_seconds,
                              "timeout_seconds": timeout, "budget_seconds": timeout + 7}, shanghai_now())
