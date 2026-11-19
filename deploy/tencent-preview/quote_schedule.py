"""Admission rules and an offline replay for serial batched quote collection."""

import argparse
from collections import Counter, deque
from datetime import datetime, timedelta
import json
from pathlib import Path

from pipeline.calendar import market_session, shanghai_now


class QuoteSchedule:
    def __init__(self, settings):
        self.slot = settings["slot_seconds"]
        self.gap = settings["min_gap_seconds"]
        self.cap = min(settings["project_cap_per_minute"], settings["collector_cap_per_minute"])
        self.batches = tuple(settings["batches"])
        self.stock_every_slots = settings.get("stock_every_slots", 5)
        if type(self.stock_every_slots) is not int or self.stock_every_slots < 2:
            raise ValueError("expected at least two slots per stock batch")
        if not (1 <= self.slot <= self.gap <= 60 and 1 <= self.cap <= 100):
            raise ValueError("invalid quote request limits")
        if self.batches not in (("rt_idx_k",), ("rt_idx_k", "rt_k")):
            raise ValueError("expected the SXData index batch and optional stock batch")
        self.starts = deque()
        self.last_slot = None
        self.next_stock_slot = self.stock_every_slots - 1
        self.last_batch = None
        self.paused_until = {}
        self.in_flight = None

    def admit(self, now):
        """Reserve at most one start per slot. A missed slot is never replayed."""
        now = shanghai_now(now)
        phase = market_session(now)["phase"]
        if phase not in {"morning", "afternoon", "closing_auction"} or self.in_flight:
            return None
        stamp = now.timestamp()
        while self.starts and self.starts[0] <= stamp - 60:
            self.starts.popleft()
        if len(self.starts) >= self.cap:
            return None
        if self.starts and stamp - self.starts[-1] < self.gap:
            return None
        start_hour, start_minute = (9, 30) if phase == "morning" else (13, 0)
        opening = now.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
        slot_index = int((now - opening).total_seconds()) // self.slot
        key = (opening, slot_index)
        if self.last_slot is not None and key <= self.last_slot:
            return None
        if self.last_slot is None or self.last_slot[0] != opening:
            self.next_stock_slot = self.stock_every_slots - 1
            self.last_batch = None
        self.last_slot = key
        # A delayed index call may skip the nominal stock slot. Give the next
        # available slot to stocks, keeping an index turn between stock turns.
        batch = "rt_idx_k"
        if "rt_k" in self.batches and slot_index >= self.next_stock_slot and self.last_batch == "rt_idx_k":
            batch = "rt_k"
            self.next_stock_slot = slot_index + self.stock_every_slots
        self.last_batch = batch
        if stamp < self.paused_until.get(batch, 0):
            return None
        self.starts.append(stamp)
        self.in_flight = batch
        return batch

    def finish(self, now, retry_after=0, *, host=False, deferred=False):
        # A stock slot blocked before sending still owes the market a stock call.
        if deferred and self.in_flight == "rt_k" and self.last_slot is not None:
            self.next_stock_slot = self.last_slot[1] + 1
            self.last_batch = "rt_idx_k"
        if self.in_flight and retry_after:
            until = shanghai_now(now).timestamp() + max(60, retry_after)
            for batch in self.batches if host else (self.in_flight,):
                self.paused_until[batch] = max(self.paused_until.get(batch, 0), until)
        self.in_flight = None


def replay(settings, start, seconds):
    schedule = QuoteSchedule(settings)
    requests = []
    peak = 0
    for offset in range(seconds):
        now = start + timedelta(seconds=offset)
        batch = schedule.admit(now)
        if batch:
            requests.append({"time": now.isoformat(timespec="seconds"), "batch": batch})
            peak = max(peak, len(schedule.starts))
            schedule.finish(now)
    return {"mode": "offline_schedule_replay", "peak_starts_in_60s": peak,
            "counts": dict(Counter(event["batch"] for event in requests)), "slots": requests}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--at", required=True, type=datetime.fromisoformat)
    parser.add_argument("--seconds", type=int, default=60, choices=range(1, 301), metavar="1..300")
    args = parser.parse_args()
    profile = Path(__file__).with_name("relay-active.json")
    settings = json.loads(profile.read_text(encoding="utf-8"))
    print(json.dumps(replay(settings, shanghai_now(args.at), args.seconds), ensure_ascii=False))


if __name__ == "__main__":
    main()
