"""Select one market-wide stock request inside an already scheduled rt_k slot."""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import timedelta
import gzip
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile

from pipeline.calendar import previous_trading_day
from board_quote_watch import STOCK


class StockUniverse:
    """Read the local stock directory and verified rosters, including newly published members."""

    def __init__(self, names, rosters):
        self.names = names
        self.rosters = rosters
        self.next_scan = 0
        self.files = {}
        self.codes = {}

    def refresh(self, stamp):
        if stamp < self.next_scan:
            return
        self.next_scan = stamp + 90
        try:
            paths = {self.names, *self.rosters.glob('*.json')}
        except OSError:
            return
        changed = False
        for path in paths:
            try:
                stat = path.stat()
                version = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
                old = self.files.get(path)
                if old and old[0] == version:
                    continue
                packet = json.loads(path.read_bytes())
                if not isinstance(packet, dict):
                    continue
                if path == self.names:
                    candidates = packet.keys()
                elif (packet.get('code') == path.stem and packet.get('source') == 'sxdata_sector_members'
                      and packet.get('status') in ('ready', 'partial') and isinstance(packet.get('members'), list)):
                    candidates = (r.get('code') for r in packet['members'] if isinstance(r, dict))
                else:
                    continue
                symbols = frozenset(c for c in candidates if isinstance(c, str) and STOCK.fullmatch(c))
                self.files[path] = (version, symbols)
                changed = True
            except (OSError, ValueError):
                continue
        for path in self.files.keys() - paths:
            del self.files[path]
            changed = True
        if changed:
            all_codes = set().union(*(entry[1] for entry in self.files.values()))
            self.codes = {c: c for c in sorted(all_codes)}


@dataclass
class StockPlan:
    kind: str
    symbols: dict
    selector: str | None = None
    group: str = ''


class StockMarketBatches:
    # The documented full-market selector was verified with the relay. Exact-code
    # sweeps also cover older Beijing identifiers absent from the four prefixes.
    standard = ('3*.SZ', '6*.SH', '0*.SZ', '9*.BJ')
    full_selector = ','.join(standard)
    page_size = 400
    fallback_seconds = 600

    def __init__(self):
        self.split_until = 0
        self.priority_next = False
        self.next_group = 0
        self.pages = {}
        self.page_limits = {}
        self.market_response_failures = 0
        self.missing = set()
        self.supplement_cursor = ''
        self.next_supplement = 0

    @staticmethod
    def group_for(code):
        return code[0] + '*' + code[-3:]

    def choose(self, universe, base, watched, stamp):
        if self.priority_next or not (universe.keys() - base.keys() - watched):
            return None
        pool = {**base, **universe}
        if stamp >= self.split_until:
            if stamp >= self.next_supplement:
                missing = sorted((self.missing & universe.keys()) - base.keys())
                room = max(0, min(self.page_size, 480 - len(base)))
                if missing and room:
                    start = bisect_right(missing, self.supplement_cursor) % len(missing)
                    extras = (missing[start:] + missing[:start])[:room]
                    return StockPlan('supplement', {**base, **{c: c for c in extras}}, group=extras[-1])
            if len(pool) <= self.page_size:
                return StockPlan('whole', pool)
            return StockPlan('whole', pool, self.full_selector)

        groups = {self.group_for(c) for c in pool}
        ordered = [g for g in self.standard if g in groups] + sorted(groups - set(self.standard))
        group = ordered[self.next_group % len(ordered)]
        symbols = sorted(c for c in pool if self.group_for(c) == group)
        if group in self.pages:
            start = bisect_right(symbols, self.pages[group]) % len(symbols)
            size = self.page_limits.get(group, self.page_size)
            chosen = (symbols[start:] + symbols[:start])[:size]
            return StockPlan('page', {c: c for c in chosen}, group=group)
        return StockPlan('group', {c: c for c in symbols}, group, group)

    def finish(self, plan, observed, core, stamp):
        if plan is None:
            self.priority_next = False
            return
        self.market_response_failures = 0
        count = len(observed & plan.symbols.keys())
        if plan.kind == 'supplement':
            self.supplement_cursor = plan.group
            self.next_supplement = stamp + 180
            return
        if plan.kind == 'whole':
            self.missing = plan.symbols.keys() - observed
            if count < .85 * len(plan.symbols) or len(core.keys() & observed) < .9 * len(core):
                self.split_until = stamp + self.fallback_seconds
                self.priority_next = True
            return
        self.priority_next = True
        self.next_group += 1
        if plan.kind == 'group' and count < .85 * len(plan.symbols):
            self.pages[plan.group] = ''
        elif plan.kind == 'page':
            self.pages[plan.group] = next(reversed(plan.symbols))
            if count < .8 * len(plan.symbols):
                self.page_limits[plan.group] = max(1, count or len(plan.symbols)//2)

    def unavailable(self, plan, error, stamp):
        # A failed transport supplies no quotes. Give the known direct batch
        # the next allowed slot, retaining the group/page cursor for retry.
        if plan is not None and error in ('transport_error', 'budget_exhausted'):
            self.priority_next = True
            if plan.kind == 'supplement':
                self.next_supplement = stamp + 180
            if plan.kind == 'whole':
                self.market_response_failures += 1
                if self.market_response_failures >= 2:
                    self.split_until = stamp + self.fallback_seconds
            if plan.kind == 'group' and len(plan.symbols) > self.page_size:
                self.pages.setdefault(plan.group, '')


class StockBatchArchive:
    """Record actual replies separately so a stopped projection can replay each market observation."""

    def __init__(self, directory):
        self.directory = directory
        self.cleaned_day = None

    def record(self, rows, received_at):
        day = received_at.date()
        directory = self.directory / day.isoformat()
        directory.mkdir(parents=True, exist_ok=True)
        fields = ('id', 'price', 'previous_close', 'change_pct', 'quote_date', 'quote_timestamp',
                  'provider', 'timestamp_quality')
        packet = dict(schema_version=1, source='sxdata_relay', trade_date=day.isoformat(),
                      received_at=received_at.isoformat(timespec='microseconds'),
                      stocks=[{k:r[k] for k in fields if k in r} for r in rows])
        raw = json.dumps(packet, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
        with NamedTemporaryFile(dir=directory, prefix='.batch-', delete=False) as handle:
            temporary = Path(handle.name)
            try:
                handle.write(gzip.compress(raw, compresslevel=1, mtime=0))
                handle.close()
                # An atomic, no-clobber link avoids losing either batch if the
                # host clock steps back or two processes finish in the same microsecond.
                for offset in range(100):
                    stamp = received_at + timedelta(microseconds=offset)
                    try:
                        os.link(temporary, directory / (stamp.strftime('%H%M%S%f') + '.json.gz'))
                        break
                    except FileExistsError:
                        continue
                else:
                    raise OSError('stock batch filename collision')
            finally:
                temporary.unlink(missing_ok=True)
        if self.cleaned_day != day:
            self.clean(day)
            self.cleaned_day = day

    def clean(self, day):
        # Keep Friday's replayable input through the next Monday, including long weekends.
        prior = previous_trading_day(day)
        cutoff = min(day - timedelta(days=2), prior or day).isoformat()
        for entry in self.directory.iterdir():
            if entry.name >= cutoff or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', entry.name):
                continue
            if entry.is_symlink() or not entry.is_dir():
                continue
            for batch in entry.glob('*.json.gz'):
                if re.fullmatch(r'[0-9]{12}\.json\.gz', batch.name):
                    batch.unlink(missing_ok=True)
            if not any(entry.iterdir()):
                entry.rmdir()
