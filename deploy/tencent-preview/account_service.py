"""Serve site accounts and the authenticated administration console over a private Unix socket."""
import argparse
from contextlib import closing
import hashlib
import hmac
from http import HTTPStatus
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import re
import secrets
from socketserver import UnixStreamServer
import sqlite3
import time
from urllib.parse import urlsplit

STATE = Path("/var/lib/ashare-radar-preview/account")
SOCKET = Path("/run/ashare-account/account.sock")
ADMIN_FILES = Path("/srv/ashare-radar-preview/private-admin")
USAGE_SNAPSHOT = STATE.parent/"request-usage/admin-dashboard.json"
COOKIE = "__Host-ashare_session"
WEEK = 7 * 86400
NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{2,31}", re.ASCII)
TOKEN = re.compile(r"[a-f0-9]{64}", re.ASCII)
ADMIN_PAGES = {"/admin/": ("index.html", "text/html; charset=utf-8"),
               "/admin/index.html": ("index.html", "text/html; charset=utf-8"),
               "/admin/admin.js": ("admin.js", "application/javascript"),
               "/admin/admin.css": ("admin.css", "text/css")}
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
 role TEXT NOT NULL CHECK(role IN ('user','admin')), salt BLOB NOT NULL, password_hash BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token BLOB PRIMARY KEY, uid INTEGER NOT NULL REFERENCES users(id),
 expires INTEGER NOT NULL) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS session_expiry ON sessions(expires);
CREATE TABLE IF NOT EXISTS attempts(action TEXT NOT NULL, peer TEXT NOT NULL, username TEXT NOT NULL, stamp INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS attempts_time ON attempts(stamp);
"""


class Rejected(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def password_hash(password, salt):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=32768, r=8, p=3, maxmem=64*1024*1024)


def session_key(raw):
    return hashlib.sha256(raw.encode("ascii")).digest()


def csrf(raw):
    return hmac.digest(raw.encode("ascii"), b"ashare-account-csrf-v1", "sha256").hex()


def username(value):
    return value.lower() if isinstance(value, str) and value.isascii() else ""


class Accounts:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(self.db()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def user(self, raw, now):
        with closing(self.db()) as db:
            row = db.execute("""SELECT username,role FROM sessions JOIN users ON uid=users.id
                WHERE token=? AND expires>?""", (session_key(raw), now)).fetchone()
        return dict(row) if row else None

    def admit(self, action, peer, name, now):
        # A forwarding proxy may share one peer across many visitors. Also limit each login name.
        window, per_name, per_peer, total = (900, 8, 120, 360) if action == "login" else (3600, 4, 16, 60)
        with closing(self.db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM attempts WHERE stamp<=?", (now-3600,))
            counts = db.execute("""SELECT count(*),count(CASE WHEN peer=? THEN 1 END),
                count(CASE WHEN username=? THEN 1 END) FROM attempts WHERE action=? AND stamp>?""",
                (peer, name, action, now-window)).fetchone()
            if counts[0] >= total or counts[1] >= per_peer or counts[2] >= per_name:
                raise Rejected(429, "rate_limited", "尝试次数过多，请稍后再试。")
            db.execute("INSERT INTO attempts VALUES(?,?,?,?)", (action, peer, name, now))
            db.execute("DELETE FROM sessions WHERE expires<=?", (now,))

    def authenticate(self, action, peer, body, current, now):
        name, password = username(body.get("username")), body.get("password")
        valid = NAME.fullmatch(name) and isinstance(password, str) and 1 <= len(password) <= 128
        if not valid:
            raise Rejected(400, "invalid_credentials", "请检查用户名和密码格式。")
        self.admit(action, peer, name, now)
        with closing(self.db()) as db, db:
            if action == "register":
                if len(password) < 12 or name in {"admin", "administrator"}:
                    raise Rejected(400, "invalid_registration", "用户名不可用，或密码长度不足12个字符。")
                salt = secrets.token_bytes(16)
                digest = password_hash(password, salt)
                try:
                    uid = db.execute("INSERT INTO users(username,role,salt,password_hash) VALUES(?,'user',?,?)",
                                     (name,salt,digest)).lastrowid
                except sqlite3.IntegrityError:
                    raise Rejected(409, "username_taken", "该用户名已被使用。") from None
            else:
                row = db.execute("SELECT id,salt,password_hash FROM users WHERE username=?", (name,)).fetchone()
                digest = password_hash(password, row["salt"] if row else b"unavailable-user")
                if not row or not hmac.compare_digest(digest, row["password_hash"]):
                    raise Rejected(401, "invalid_credentials", "用户名或密码不正确。")
                uid = row["id"]
            fresh = secrets.token_hex(32)
            db.execute("DELETE FROM sessions WHERE token=?", (session_key(current),))
            db.execute("INSERT INTO sessions VALUES(?,?,?)", (session_key(fresh), uid, now+WEEK))
        return fresh

    def logout(self, raw):
        with closing(self.db()) as db, db:
            db.execute("DELETE FROM sessions WHERE token=?", (session_key(raw),))
        return secrets.token_hex(32)

    def bootstrap(self, name, output):
        name = username(name)
        if not NAME.fullmatch(name):
            raise ValueError("管理员用户名无效")
        secret, salt = secrets.token_urlsafe(30), secrets.token_bytes(16)
        digest = password_hash(secret, salt)
        created = False
        try:
            with closing(self.db()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM users WHERE role='admin' OR username=?", (name,)).fetchone():
                    raise ValueError("管理员或同名账号已存在")
                db.execute("INSERT INTO users(username,role,salt,password_hash) VALUES(?,'admin',?,?)",
                           (name,salt,digest))
                fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                created = True
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(f"username={name}\npassword={secret}\n")
                    stream.flush()
                    os.fsync(stream.fileno())
        except BaseException:
            if created:
                output.unlink(missing_ok=True)
            raise


def session_body(user, raw):
    return {"user":user, "authenticated":user is not None, "role":user["role"] if user else None,
            "username":user["username"] if user else None, "csrf_token":csrf(raw)}


class Handler(BaseHTTPRequestHandler):
    server_version = "Ashare"
    sys_version = ""

    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(8)

    def read_post_bytes(self):
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = 0
        if not 1 <= length <= 4096:
            raise Rejected(400, "invalid_request", "请求格式有误。")
        return self.rfile.read(length)

    def send(self, status, body=b"", *, content="application/json; charset=utf-8", cookie=None, location=None):
        self.send_response(status)
        self.send_header("Content-Type", content)
        self.send_header("Cache-Control", "no-store, private")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'; form-action 'self'")
        if cookie:
            self.send_header("Set-Cookie", f"{COOKIE}={cookie}; Path=/; Max-Age={WEEK}; SameSite=Lax; HttpOnly; Secure")
        if location:
            self.send_header("Location", location)
        raw = json.dumps(body, ensure_ascii=False, allow_nan=False).encode() if isinstance(body, dict) else body
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if self.command != "HEAD" and raw:
            self.wfile.write(raw)

    def post_body(self, raw, payload):
        try:
            origin = urlsplit(self.headers.get("Origin", ""))
            host = self.headers.get("Host", "").lower()
            local = origin.hostname in {"localhost", "127.0.0.1", "::1"}
            origin_ok = (origin.scheme == "https" or origin.scheme == "http" and local) and (
                origin.netloc.lower() == host and origin.hostname and not origin.username and not origin.password
                and not origin.path and not origin.query and not origin.fragment)
        except ValueError:
            origin_ok = False
        token = self.headers.get("X-CSRF-Token", "")
        if not origin_ok or not TOKEN.fullmatch(token) or not hmac.compare_digest(token, csrf(raw)):
            raise Rejected(403, "csrf_required", "登录凭证已过期，请刷新页面再试。")
        if self.headers.get_content_type() != "application/json":
            raise Rejected(415, "json_required", "请求须为JSON。")
        try:
            obj = json.loads(payload.decode("utf-8"))
            if not isinstance(obj, dict):
                raise ValueError
        except (ValueError, RecursionError):
            raise Rejected(400, "invalid_request", "请求格式有误。") from None
        return obj

    def handle_request(self):
        path = urlsplit(self.path).path
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            candidate = cookies[COOKIE].value if COOKIE in cookies else ""
        except CookieError:
            candidate = ""
        raw = candidate if TOKEN.fullmatch(candidate) else secrets.token_hex(32)
        accounts, now = self.server.accounts, int(time.time())
        if self.command == "POST":
            # Consume the bounded, proxy-buffered request even when authorization will fail.
            # Closing a Unix socket with unread body bytes can discard its HTTP response.
            payload = self.read_post_bytes()
            if path == "/api/admin/policy":
                self.require_admin(accounts.user(raw, now))
                body = self.post_body(raw, payload)
                if set(body) != {"limit_60s"}:
                    raise Rejected(400, "invalid_policy", "只接受 limit_60s 字段。")
                from pipeline.request_policy import set_limit
                try:
                    policy = set_limit(body["limit_60s"])
                except ValueError:
                    raise Rejected(400, "invalid_policy", "请检查每分钟请求上限。") from None
                self.send(200, policy)
                return
            if path not in {"/api/account/login", "/api/account/register", "/api/account/logout"}:
                raise Rejected(404, "not_found", "页面不存在。")
            body = self.post_body(raw, payload)
            action = path.rsplit("/",1)[-1]
            fresh = accounts.logout(raw) if action == "logout" else accounts.authenticate(
                action, self.headers.get("X-Account-Peer", "local"), body, raw, now)
            self.send(200, session_body(accounts.user(fresh, now), fresh), cookie=fresh)
            return
        if path == "/api/account/session":
            self.send(200, session_body(accounts.user(raw,now),raw), cookie=raw if raw != candidate else None)
            return
        if path.startswith("/admin/") or path in {"/api/admin/usage","/api/admin/policy"}:
            user = accounts.user(raw, now)
            if not user and path in {"/admin/", "/admin/index.html"}:
                self.send(302, location="/account/?next=/admin/")
                return
            self.require_admin(user)
            if path == "/api/admin/usage":
                # The metering worker publishes a private, atomic aggregate. Requests to
                # the console never open the collectors' WAL databases.
                usage = json.loads(self.server.usage_snapshot.read_bytes())
                if not isinstance(usage, dict) or usage.get("schema_version") != 1:
                    raise ValueError("invalid usage snapshot")
                self.send(200, usage)
                return
            if path == "/api/admin/policy":
                from pipeline.request_policy import read_policy
                self.send(200, read_policy())
                return
            if path in ADMIN_PAGES:
                name, content = ADMIN_PAGES[path]
                self.send(200, (self.server.assets/name).read_bytes(), content=content)
                return
        raise Rejected(404, "not_found", "页面不存在。")

    @staticmethod
    def require_admin(user):
        if not user or user["role"] != "admin":
            raise Rejected(403 if user else 401, "admin_required", "请使用管理员账号登录。")

    def run(self):
        try:
            self.handle_request()
        except Rejected as error:
            self.send(error.status, {"error":error.code, "message":error.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as err:
            print("Account service error:", type(err).__name__, flush=True)
            self.send(HTTPStatus.SERVICE_UNAVAILABLE, {"error":"temporarily_unavailable", "message":"服务暂时不可用，请稍后再试。"})

    do_GET = do_HEAD = do_POST = run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve")
    bootstrap = commands.add_parser("bootstrap-admin")
    bootstrap.add_argument("--username", default="admin")
    bootstrap.add_argument("--credential-file", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    accounts = Accounts(STATE/"accounts.sqlite3")
    if args.command == "bootstrap-admin":
        try:
            accounts.bootstrap(args.username, args.credential_file)
        except (OSError, ValueError) as error:
            parser.exit(1, f"Bootstrap failed: {error}\n")
        return
    SOCKET.unlink(missing_ok=True)
    with UnixStreamServer(str(SOCKET), Handler) as server:
        server.accounts, server.assets, server.usage_snapshot = accounts, ADMIN_FILES, USAGE_SNAPSHOT
        os.chmod(SOCKET, 0o660)
        server.serve_forever()


if __name__ == "__main__":
    main()
