from contextlib import closing
from http.client import HTTPConnection
import json
from pathlib import Path
import select
import socket
from socketserver import UnixStreamServer
import sys
from tempfile import TemporaryDirectory
import threading
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT),str(ROOT/"deploy/tencent-preview")]
from account_service import Accounts, COOKIE, Handler


class UnixConnection(HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=4)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))


class AccountHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Accounts(self.root/"private/accounts.sqlite3")
        self.socket = self.root/"test.sock"
        server = UnixStreamServer(str(self.socket), Handler)
        server.accounts = self.store
        server.assets = self.root/"admin"
        server.usage_snapshot = self.root/"private/admin-dashboard.json"
        server.assets.mkdir()
        (server.assets/"index.html").write_text("private-console")
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval":.01})
        thread.start()
        self.addCleanup(thread.join,2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.cookie, self.csrf = "", ""
        self.host = "127.0.0.1:18769"
        self.passwd = "test password with spaces"

    def request(self, path, data=None, *, extra=None):
        headers = {"Host":self.host}
        if self.cookie:
            headers["Cookie"] = self.cookie
        body = None
        if data is not None:
            headers.update({"Content-Type":"application/json","Origin":"http://"+self.host,"X-CSRF-Token":self.csrf})
            body = json.dumps(data)
        headers.update(extra or {})
        with closing(UnixConnection(self.socket)) as conn:
            conn.request("POST" if data is not None else "GET", path, body=body, headers=headers)
            response = conn.getresponse()
            status, outgoing, raw = response.status, dict(response.getheaders()), response.read()
        if "Set-Cookie" in outgoing:
            self.cookie = outgoing["Set-Cookie"].split(";")[0]
        result = json.loads(raw) if raw and outgoing["Content-Type"].startswith("application/json") else raw
        if isinstance(result,dict) and "csrf_token" in result:
            self.csrf = result["csrf_token"]
        return status, outgoing, result

    def test_guest_signup_session_rotation_and_no_admin_escalation(self):
        self.assertEqual(self.request("/admin/")[1]["Location"], "/account/?next=/admin/")
        self.assertEqual(self.request("/api/admin/usage")[0],401)
        status, headers, guest = self.request("/api/account/session")
        self.assertIsNone(guest["user"])
        self.assertEqual(status,200)
        for value in (COOKIE+"=", "HttpOnly", "Secure", "SameSite=Lax", "Path=/"):
            self.assertIn(value, headers["Set-Cookie"])
        old_guest = self.cookie
        creds = {"username":"Reader", "password":self.passwd, "role":"admin"}
        self.assertEqual(self.request("/api/account/register", creds,extra={"Origin":"https://other.example"})[0],403)
        self.assertEqual(self.request("/api/account/register", creds,extra={"Origin":"http://127.0.0.1:8769"})[0],403)
        self.assertEqual(self.request("/api/account/register", creds,extra={"X-CSRF-Token":""})[0],403)
        self.assertEqual(self.request("/api/account/register", creds)[2]["user"], {"username":"reader","role":"user"})
        self.assertNotEqual(self.cookie, old_guest)
        self.assertEqual(self.request("/admin/")[0],403)
        self.assertEqual(self.request("/api/admin/policy", {"limit_60s":20})[0],403)
        authenticated = self.cookie
        self.assertIsNone(self.request("/api/account/logout",{})[2]["user"])
        self.cookie = authenticated
        self.assertIsNone(self.request("/api/account/session")[2]["user"])
        with self.store.db() as db:
            self.assertNotEqual(self.passwd.encode(), db.execute("SELECT password_hash FROM users").fetchone()[0])

    def test_private_admin_bootstrap_and_administrator_endpoints(self):
        output = self.root/"credentials"
        self.store.bootstrap("admin",output)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        first = output.read_text()
        with self.assertRaises(ValueError):
            self.store.bootstrap("second_admin",output)
        self.assertEqual(output.read_text(),first)
        secret = first.split("password=")[1].strip()
        self.request("/api/account/session")
        creds = {"username":"admin","password":secret}
        self.host = "preview.example:8443"
        self.assertEqual(self.request("/api/account/login",creds)[0],403)
        self.assertEqual(self.request("/api/account/login",creds,extra={"Origin":"https://"+self.host})[2]["role"],"admin")
        self.assertEqual(self.request("/admin/")[2],b"private-console")
        self.assertEqual(self.request("/admin/unlisted.txt")[0],404)
        policy = types.ModuleType("pipeline.request_policy")
        policy.read_policy = lambda: {"limit_60s":100}
        policy.set_limit = lambda n: {"limit_60s":n}
        self.assertEqual(self.request("/api/admin/usage")[0],503)
        (self.root/"private/admin-dashboard.json").write_text(json.dumps({"schema_version":1}))
        with patch.dict(sys.modules, {"pipeline.request_policy":policy}):
            self.assertEqual(self.request("/api/admin/usage")[2], {"schema_version":1})
            self.assertEqual(self.request("/api/admin/policy")[2], {"limit_60s":100})
            origin = {"Origin":"https://"+self.host}
            self.assertEqual(self.request("/api/admin/policy", {"limit_60s":12,"extra":1},extra=origin)[0],400)
            self.assertEqual(self.request("/api/admin/policy", {"limit_60s":12},extra=origin)[2],{"limit_60s":12})

    def test_attempts_persist_and_first_admin_cannot_be_reserved_publicly(self):
        self.request("/api/account/session")
        self.assertEqual(self.request("/api/account/register",{"username":"admin","password":self.passwd})[0],400)
        creds = {"username":"reader","password":self.passwd}
        for _ in range(8):
            self.assertEqual(self.request("/api/account/login", creds)[0],401)
        self.assertEqual(self.request("/api/account/login", creds)[0],429)
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM users").fetchone()[0],0)

    def test_rejected_post_consumes_the_separately_sent_body(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(self.socket))
            client.sendall(b"POST /api/account/login HTTP/1.1\r\nHost: localhost\r\n"
                           b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\n")
            self.assertFalse(select.select([client], [], [], .05)[0])
            client.sendall(b"{}")
            with client.makefile("rb") as incoming:
                response = incoming.read()
            self.assertIn(b"403 Forbidden", response)
            self.assertIn(b"csrf_required", response)
