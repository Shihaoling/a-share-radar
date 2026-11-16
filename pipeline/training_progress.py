"""Atomic local progress snapshots and an explicitly loopback-only monitor.

Only the configured HTML and progress JSON are served. This is not a general
file server and it has no upload or remote-network capability. ``updated_at``
changes only when the trainer reports an event, never on a server heartbeat.
"""
from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from urllib.parse import urlsplit


_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _reject_constant(value):
    raise ValueError("Non-finite JSON number")


class ProgressWriter:
    """Merge progress fields without losing other stages' history.

    Writers to the same resolved path share an in-process lock. Independent
    trainer processes should use separate run directories, as the launcher does.
    """

    def __init__(self, path):
        self.path = Path(path).resolve()
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(os.path.normcase(str(self.path)), threading.RLock())

    def _read(self):
        if not self.path.exists():
            return {"status": "running", "started_at": _now(), "history": []}
        value = json.loads(self.path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
        if not isinstance(value, dict) or not isinstance(value.get("history", []), list):
            raise ValueError("Progress snapshot must be an object with list history")
        value.setdefault("status", "running")
        value.setdefault("started_at", _now())
        value.setdefault("history", [])
        return value

    def _save(self, value):
        value["updated_at"] = _now()
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            # Close the temporary handle before replacement for Windows support.
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                    prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent,
                    delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            for attempt in range(6):
                try:
                    os.replace(temporary, self.path)
                    break
                except PermissionError:
                    # A short-lived Windows reader can prevent replacement.
                    if attempt == 5:
                        raise
                    time.sleep(.02 * (attempt + 1))
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return json.loads(payload)

    def write(self, **updates):
        with self._lock:
            value = self._read()
            value.update(updates)
            if not isinstance(value.get("history"), list):
                raise ValueError("Progress history must be a list")
            return self._save(value)

    def append_history(self, entry):
        if not isinstance(entry, dict):
            raise TypeError("History entry must be an object")
        with self._lock:
            value = self._read()
            value["history"].append(entry)
            return self._save(value)


class MonitorServer:
    """Serve a local monitor on 127.0.0.1; call close() to release its port."""

    def __init__(self, progress_path, html_path, port=8768):
        if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
            raise ValueError("Port must be an integer from 0 to 65535")
        self.progress_path = Path(progress_path).resolve()
        self.html_path = Path(html_path).resolve()
        self.port = port
        self._server = None
        self._thread = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/"

    def start(self):
        if self._server is not None:
            return self
        monitor = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                # Do not echo request paths, headers, or local filenames.
                pass

            def _reply(self, status, data, content_type="application/json; charset=utf-8"):
                if not isinstance(data, bytes):
                    data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                hosts = self.headers.get_all("Host", [])
                allowed = {f"127.0.0.1:{monitor.port}", f"localhost:{monitor.port}"}
                if len(hosts) != 1 or hosts[0].lower() not in allowed:
                    self._reply(403, {"error": "Local host required"})
                    return
                # Exact route matching prevents traversal and directory listing.
                try:
                    parts = urlsplit(self.path)
                except ValueError:
                    self._reply(400, {"error": "Invalid request target"})
                    return
                if parts.scheme or parts.netloc:
                    self._reply(400, {"error": "Origin-form request required"})
                    return
                route = parts.path
                if route in {"/", "/training-monitor.html"}:
                    try:
                        data = monitor.html_path.read_bytes()
                    except OSError:
                        self._reply(503, {"error": "Monitor page unavailable"})
                    else:
                        self._reply(200, data, "text/html; charset=utf-8")
                elif route == "/progress.json":
                    try:
                        payload = monitor.progress_path.read_text(encoding="utf-8")
                        data = json.loads(payload, parse_constant=_reject_constant)
                        if not isinstance(data, dict):
                            raise ValueError("Invalid progress object")
                    except FileNotFoundError:
                        data = {"status": "waiting", "stage": "waiting", "updated_at": None,
                                "message": "Training has not started.", "history": []}
                    except (OSError, UnicodeError, ValueError):
                        self._reply(503, {"error": "Progress temporarily unavailable"})
                        return
                    self._reply(200, data)
                else:
                    self._reply(404, {"error": "Not found"})

            def do_POST(self):
                self._reply(405, {"error": "Read-only monitor"})

            do_PUT = do_DELETE = do_OPTIONS = do_POST

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="training-monitor", daemon=True)
        self._thread.start()
        return self

    def close(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=2)
            self._server = self._thread = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()
