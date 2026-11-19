"""Fold verified board constituents into an existing scheduled stock request."""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import NamedTuple


BOARD = re.compile(r"[0-9]{6}R?\.TI")
STOCK = re.compile(r"[0-9]{6}\.(?:SH|SZ|BJ)")


class BoardBatch(NamedTuple):
    extra: dict
    cursors: dict
    next_board: str | None


class BoardQuoteWatch:
    max_boards = 8
    max_extra = 96
    max_symbols = 480
    ttl = 150

    def __init__(self, watch_file: Path, members_dir: Path):
        self.watch_file = watch_file
        self.members_dir = members_dir
        self.watches = {}
        self.rosters = {}
        self.versions = {}
        self.cursors = {}
        self.tracked_codes = set()
        self.limit = self.max_extra
        self.retry_at = 0
        self.healthy_batches = 0
        self.next_board = None

    def refresh(self, stamp):
        try:
            rows = json.loads(self.watch_file.read_bytes())["sectors"]
            if isinstance(rows, list):
                watches = {}
                for row in rows:
                    if not isinstance(row, list) or len(row) != 2:
                        continue
                    board, touched = row
                    if (isinstance(board, str) and BOARD.fullmatch(board) and type(touched) in (int, float)
                            and stamp - self.ttl <= touched <= stamp):
                        watches[board] = max(watches.get(board, touched), touched)
                self.watches = dict(sorted(watches.items(), key=lambda item: -item[1])[:self.max_boards])
        except (OSError, KeyError, TypeError, ValueError):
            pass
        self.watches = {b: t for b, t in self.watches.items() if stamp - self.ttl <= t <= stamp}
        for board in self.rosters.keys() - self.watches.keys():
            self.rosters.pop(board)
            self.versions.pop(board, None)
            self.cursors.pop(board, None)
        for board in self.watches:
            path = self.members_dir / (board + ".json")
            try:
                stat = path.stat()
                version = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
                if self.versions.get(board) == version:
                    continue
                data = json.loads(path.read_bytes())
                if (not isinstance(data, dict) or data.get("code") != board or
                        data.get("source") != "sxdata_sector_members" or
                        data.get("status") not in ("partial", "ready") or not isinstance(data.get("members"), list)):
                    continue
                codes = dict.fromkeys(row["code"] for row in data["members"] if isinstance(row, dict)
                                      and isinstance(row.get("code"), str) and STOCK.fullmatch(row["code"]))
                self.rosters[board] = tuple(codes)
                self.versions[board] = version
            except (OSError, ValueError):
                continue
        self.tracked_codes = {code for codes in self.rosters.values() for code in codes}

    def choose(self, base, stamp, *, allowed=True):
        """Stage a round-robin allocation. A local gate deferral consumes no turns."""
        count = min(self.limit, self.max_symbols - len(base))
        if not allowed or count <= 0 or stamp < self.retry_at:
            return BoardBatch({}, {}, None)
        boards = sorted(board for board, codes in self.rosters.items() if codes)
        if self.next_board in boards:
            start = boards.index(self.next_board)
            boards = boards[start:] + boards[:start]
        cursors = {b: self.cursors.get(b, 0) % len(self.rosters[b]) for b in boards}
        remaining = {b: len(self.rosters[b]) for b in boards}
        extra = {}
        next_board = None
        while len(extra) < count and any(remaining.values()):
            for i, board in enumerate(boards):
                while remaining[board]:
                    codes = self.rosters[board]
                    code = codes[cursors[board]]
                    cursors[board] = (cursors[board] + 1) % len(codes)
                    remaining[board] -= 1
                    if code not in base and code not in extra:
                        extra[code] = code
                        next_board = boards[(i + 1) % len(boards)]
                        break
                if len(extra) == count:
                    break
        return BoardBatch(extra, cursors, next_board)

    def finish(self, stamp, plan, core, observed):
        extra, cursors, next_board = plan
        if not extra:
            return
        self.cursors.update(cursors)
        self.next_board = next_board
        included = len(extra.keys() & observed)
        if not included or len(core.keys() & observed) < .9 * len(core):
            self.limit = max(1, len(extra) // 2)
            self.retry_at = stamp + 60
            self.healthy_batches = 0
        elif included < len(extra):
            # A partial result can be an upstream row limit or a suspended
            # stock. Smaller rotating slices give unseen symbols their turn.
            self.limit = included
            self.healthy_batches = 0
        else:
            self.healthy_batches += 1
            if self.healthy_batches >= 8:
                self.limit = min(self.max_extra, self.limit + 8)
                self.healthy_batches = 0
