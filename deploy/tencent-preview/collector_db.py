"""Shared SQLite coordination for cloud collectors and backfill workers.

The live relay and the recovery timers write to the same two databases. WAL keeps
readers from blocking writers; a long busy timeout plus bounded retry keeps a
short-lived recovery job from failing on a transient write lock.
"""
import os
import random
import sqlite3
import time

BUSY_TIMEOUT_SECONDS = 30
WAL_RETRY = 3


def locked(error):
    if not isinstance(error, sqlite3.OperationalError):
        return False
    code = getattr(error, "sqlite_errorcode", 0) & 255
    text = str(error).lower()
    return code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or "locked" in text or "busy" in text


def connect(path, *, timeout=BUSY_TIMEOUT_SECONDS, wal=True):
    db = sqlite3.connect(path, timeout=timeout)
    db.execute("PRAGMA busy_timeout=%d" % int(timeout * 1000))
    if wal and os.name != "nt":
        for _ in range(WAL_RETRY):
            try:
                db.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as error:
                if not locked(error):
                    break
                time.sleep(0.2)
    return db


def retry_locked(task, *, attempts=4, base=0.4):
    """Retry a short DB write when another collector holds the write lock."""
    for index in range(attempts):
        try:
            return task()
        except sqlite3.OperationalError as error:
            if not locked(error) or index == attempts - 1:
                raise
            time.sleep(base * (2 ** index) + random.uniform(0, 0.2))


def commit(db):
    """Commit with the same bounded retry used by the one-shot workers."""
    retry_locked(db.commit, attempts=3, base=0.3)
