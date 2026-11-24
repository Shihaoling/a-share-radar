"""Admission and recovery of the active Tencent quote profile, without any network."""
from collections import Counter
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

if sys.platform == 'win32':
    raise unittest.SkipTest("Tencent Cloud workers use POSIX file locking")

DEPLOY = Path(__file__).resolve().parents[1] / "deploy/tencent-preview"
with patch.object(sys, "path", [str(DEPLOY), *sys.path]):
    from quote_schedule import QuoteSchedule
    from relay_quotes import restore_gate_cooldowns

ACTIVE = json.loads((DEPLOY / "relay-active.json").read_text())
PROFILE = dict(ACTIVE, staggered_minutes=False, collector_cap_per_minute=12)


def at(time):
    return datetime.fromisoformat("2026-11-19T" + time + "+08:00")


def collect(schedule, start, seconds, costs=()):
    events = []
    now = start
    end = start + timedelta(seconds=seconds)
    while now < end:
        batch = schedule.admit(now)
        if batch:
            duration = costs[len(events) % len(costs)] if costs else 0
            events.append((now, batch))
            now += timedelta(seconds=duration)
            schedule.finish(now)
        now += timedelta(seconds=0.25)
    return events


class CloudQuoteScheduleTests(unittest.TestCase):
    def test_active_profile_keeps_each_lane_on_its_own_half_minute(self):
        for opening in ("09:30:00", "13:00:00"):
            start = at(opening)
            events = collect(QuoteSchedule(ACTIVE),start,120)
            self.assertEqual([(int((stamp-start).total_seconds()),batch) for stamp,batch in events],
                             [(k+sec,batch) for k in (0,60) for sec,batch in
                              ((3,"rt_idx_k"),(11,"rt_k"),(33,"rt_idx_k"),(41,"rt_k"))])
            slow = QuoteSchedule(ACTIVE)
            slow.gap = 15
            self.assertEqual([batch for _,batch in collect(slow,start,120)], ["rt_idx_k","rt_k"]*2)

    def test_slower_recovery_keeps_both_batches_and_retries_unsent_stock_turn(self):
        schedule = QuoteSchedule(PROFILE)
        schedule.gap = 15
        start = at("09:30:00")
        self.assertEqual([batch for _, batch in collect(schedule, start, 45)], ["rt_idx_k", "rt_k", "rt_idx_k"])
        self.assertEqual(schedule.admit(start + timedelta(seconds=45)), "rt_k")
        schedule.finish(start + timedelta(seconds=45), deferred=True)
        self.assertIsNone(schedule.admit(start + timedelta(seconds=50)))
        self.assertEqual(schedule.admit(start + timedelta(seconds=60)), "rt_k")
        schedule.finish(start + timedelta(seconds=60))
        self.assertEqual(schedule.admit(start + timedelta(seconds=75)), "rt_idx_k")

    def test_active_profile_alternates_index_and_stock_batches_every_ten_seconds(self):
        schedule = QuoteSchedule(PROFILE)
        for opening, end in [("09:30:00", "11:29:55"), ("13:00:00", "14:59:55")]:
            events = collect(schedule, at(opening), 2*3600)
            self.assertEqual(Counter(batch for _, batch in events), {"rt_idx_k": 720, "rt_k": 720})
            self.assertEqual(events[:6], [(at(opening)+timedelta(seconds=5*i), batch)
                                         for i,batch in enumerate(["rt_idx_k","rt_k"]*3)])
            self.assertEqual(events[-1], (at(end),"rt_k"))
        for stamp in ("09:29:59","11:30:00","12:59:59","15:00:00","16:00:00"):
            self.assertIsNone(schedule.admit(at(stamp)))

    def test_legacy_six_second_profile_keeps_every_fifth_slot_for_stocks(self):
        config=dict(PROFILE,slot_seconds=6,min_gap_seconds=6,collector_cap_per_minute=10)
        config.pop("stock_every_slots")
        events=collect(QuoteSchedule(config),at("09:30:00"),60)
        self.assertEqual(Counter(batch for _,batch in events),{"rt_idx_k":8,"rt_k":2})
        self.assertEqual([stamp for stamp,batch in events if batch=="rt_k"],
                         [at("09:30:24"),at("09:30:54")])

    def test_blocked_starts_give_both_batches_turns_without_catchup(self):
        start = at("09:30:00")
        slow = collect(QuoteSchedule(PROFILE), start, 120, costs=[29.75])
        self.assertEqual(slow, [(start+timedelta(seconds=30*i), batch)
                               for i,batch in enumerate(["rt_idx_k","rt_k"]*2)])
        events = collect(QuoteSchedule(PROFILE), start, 180, costs=[10.5,1,18])
        self.assertTrue(all((b[0]-a[0]).total_seconds()>=5 for a,b in zip(events,events[1:])))
        self.assertTrue(all((events[i][0]-events[i-12][0]).total_seconds()>=60 for i in range(12,len(events))))
        self.assertTrue(any(batch=="rt_k" for _,batch in events))

    def test_host_429_suppresses_both_batches_until_open_and_does_not_burst(self):
        start = at("09:30:00")
        schedule = QuoteSchedule(PROFILE)
        self.assertEqual(schedule.admit(start),"rt_idx_k")
        schedule.finish(start,120,host=True)
        self.assertEqual(collect(schedule,start+timedelta(seconds=1),119),[])
        events = collect(schedule,start+timedelta(seconds=120),60)
        self.assertEqual(len(events),12)
        self.assertEqual(events[0][0],start+timedelta(seconds=120))
        self.assertEqual(Counter(batch for _,batch in events),{"rt_idx_k":6,"rt_k":6})

    def test_restarted_worker_honors_remaining_gate_without_extending_provider_deadline(self):
        now = at("09:30:00")
        schedule = QuoteSchedule(PROFILE)
        with patch("relay_quotes.gate_delay",return_value=(12,0)):
            restore_gate_cooldowns(schedule,now)
        self.assertEqual(collect(schedule,now,12),[])
        self.assertEqual(schedule.paused_until["rt_idx_k"],(now+timedelta(seconds=12)).timestamp())
        self.assertIsNone(schedule.admit(now+timedelta(seconds=12)))
        self.assertEqual(schedule.admit(now+timedelta(seconds=15)),"rt_k")

    def test_midday_endpoint_pause_does_not_stop_healthy_stocks_after_reopen(self):
        schedule = QuoteSchedule(PROFILE)
        before_lunch = at("11:29:48")
        # A long index cooldown set just before the break persists into the afternoon.
        self.assertEqual(schedule.admit(before_lunch),"rt_idx_k")
        schedule.finish(before_lunch,6000)
        self.assertIsNone(schedule.admit(at("12:59:59")))
        events=collect(schedule,at("13:00:00"),60)
        self.assertEqual([batch for _,batch in events],["rt_k"]*6)
        self.assertEqual(events[0][0],at("13:00:05"))
