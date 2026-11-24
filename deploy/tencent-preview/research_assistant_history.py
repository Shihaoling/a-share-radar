"""Private conversation history for authenticated research assistant users."""

from contextlib import closing
from http.client import HTTPConnection
import json
import re
import socket
import sqlite3
from uuid import uuid4

ACCOUNT_SOCKET = "/run/ashare-account/account.sock"
BATCH = 4
RECENT = 2
CONTEXT_MAX = 6
PAGE = 25
ID = re.compile(r"[a-f0-9]{32}\Z")
CONSOLE_HREF = "/admin/"
SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations(
    id TEXT PRIMARY KEY, owner INTEGER NOT NULL, title TEXT NOT NULL,
    updated_at TEXT NOT NULL, turn_count INTEGER NOT NULL,
    summary TEXT NOT NULL DEFAULT '', summarized INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS assistant_owner_updated ON conversations(owner, updated_at DESC);
CREATE TABLE IF NOT EXISTS conversation_turns(
    conversation TEXT NOT NULL REFERENCES conversations(id),
    seq INTEGER NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL, sources TEXT NOT NULL, progress TEXT,
    PRIMARY KEY(conversation, seq)
) WITHOUT ROWID;
"""


class UnknownConversation(Exception):
    pass


class AccountConnection(HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(ACCOUNT_SOCKET)


def account_id(cookie):
    if not cookie:
        return None
    with closing(AccountConnection("localhost", timeout=5)) as conn:
        conn.request("GET", "/internal/research-assistant/identity", headers={"Cookie": cookie})
        response = conn.getresponse()
        if response.status != 200:
            raise OSError("account service unavailable")
        owner = json.loads(response.read(4096)).get("id")
        if owner is not None and (type(owner) is not int or owner < 1):
            raise ValueError("invalid account identity")
        return owner


def model_turn(row):
    if any(isinstance(source,dict) and source.get("href") == CONSOLE_HREF for source in json.loads(row["sources"])):
        return {"question":"用户请求概括当前控制台。", "answer":"已概括控制台的计量和设置区。"}
    return {"question":row["question"], "answer":row["answer"]}


class History:
    def __init__(self, file):
        self.file = file
        with closing(self.db()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            with db:
                db.execute("BEGIN IMMEDIATE")
                if not any(row["name"]=="progress" for row in db.execute("PRAGMA table_info(conversation_turns)")):
                    db.execute("ALTER TABLE conversation_turns ADD COLUMN progress TEXT")

    def db(self):
        db = sqlite3.connect(self.file, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def list(self, owner):
        with closing(self.db()) as db:
            return [dict(row) for row in db.execute(
                "SELECT id, title, updated_at, turn_count FROM conversations WHERE owner=? ORDER BY updated_at DESC, id",
                (owner,))]

    def owned(self, db, owner, ident):
        row = db.execute("SELECT * FROM conversations WHERE owner=? AND id=?", (owner, ident)).fetchone()
        if row is None:
            raise UnknownConversation
        return row

    def view(self, owner, ident, before=None):
        with closing(self.db()) as db:
            thread = self.owned(db, owner, ident)
            rows = db.execute("""SELECT seq, question, answer, sources, progress FROM conversation_turns
                WHERE conversation=? AND seq<? ORDER BY seq DESC LIMIT ?""",
                              (ident, before or thread["turn_count"]+1, PAGE)).fetchall()
        return {"id": ident, "count": thread["turn_count"], "summarized": thread["summarized"],
                "turns": [{**dict(row), "sources":json.loads(row["sources"]),
                           "progress":json.loads(row["progress"]) if row["progress"] else None} for row in reversed(rows)],
                "before": rows[-1]["seq"] if rows and rows[-1]["seq"] > 1 else None}

    def append(self, owner, ident, question, answer, sources, now, *, progress=None):
        with closing(self.db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if ident is None:
                ident = uuid4().hex
                count, summarized = 1, 0
                db.execute("INSERT INTO conversations(id, owner, title, updated_at, turn_count) VALUES (?, ?, ?, ?, ?)",
                           (ident, owner, " ".join(question.split())[:48], now, count))
            else:
                thread = self.owned(db, owner, ident)
                count, summarized = thread["turn_count"]+1, thread["summarized"]
                db.execute("UPDATE conversations SET turn_count=?, updated_at=? WHERE owner=? AND id=?",
                           (count, now, owner, ident))
            db.execute("""INSERT INTO conversation_turns(conversation,seq,question,answer,sources,progress)
                        VALUES (?, ?, ?, ?, ?, ?)""",(ident,count,question,answer,json.dumps(sources,ensure_ascii=False),
                                                       json.dumps(progress,ensure_ascii=False) if progress else None))
        return {"id": ident, "count": count, "summarized": summarized}

    def context(self, owner, ident):
        if ident is None:
            return {"history": [], "conversation_summary": ""}
        with closing(self.db()) as db:
            thread = self.owned(db, owner, ident)
            count, through = thread["turn_count"], thread["summarized"]
            start = max(0, count-CONTEXT_MAX, min(through, count-RECENT))
            rows = db.execute("""SELECT question, answer, sources FROM conversation_turns
                WHERE conversation=? AND seq>? AND seq<=? ORDER BY seq""", (ident, start, count))
            history = [{"role": role, "text": row[field]}
                       for row in map(model_turn,rows) for role, field in (("user", "question"), ("assistant", "answer"))]
        return {"history": history, "conversation_summary": thread["summary"]}

    def batch(self, owner, ident):
        with closing(self.db()) as db:
            thread = self.owned(db, owner, ident)
            through = thread["summarized"]
            if thread["turn_count"] - through < BATCH:
                return through, None
            rows = db.execute("""SELECT question, answer, sources FROM conversation_turns
                WHERE conversation=? AND seq>? AND seq<=? ORDER BY seq""", (ident, through, through+BATCH))
            return through, {"previous_summary": thread["summary"], "turns": [model_turn(row) for row in rows]}

    def checkpoint(self, owner, ident, through, summary):
        with closing(self.db()) as db, db:
            return db.execute("""UPDATE conversations SET summarized=?, summary=?
                WHERE owner=? AND id=? AND summarized=? AND turn_count>=?""",
                              (through+BATCH, summary, owner, ident, through, through+BATCH)).rowcount == 1
