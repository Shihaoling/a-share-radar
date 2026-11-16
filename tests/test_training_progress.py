"""Local progress persistence and exact-route HTTP contracts; no market API."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pipeline.training_progress import MonitorServer, ProgressWriter


class ProgressWriterTests(unittest.TestCase):
    def test_merge_preserves_start_and_history_and_rejects_nan(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "run" / "progress.json"
            writer = ProgressWriter(path)
            initial = writer.write(stage="loading", message="正在读取真实历史")
            writer.append_history({"model": "gru", "epoch": 1, "train_huber": .5})
            updated = writer.write(stage="training", epoch=2)
            self.assertEqual(initial["started_at"], updated["started_at"])
            self.assertEqual(updated["status"], "running")
            self.assertEqual(updated["history"][0]["epoch"], 1)
            self.assertIn("正在读取真实历史", path.read_text(encoding="utf-8"))
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                writer.write(loss=float("nan"))
            self.assertEqual(before, path.read_bytes())
            self.assertEqual([], list(path.parent.glob("*.tmp")))

    def test_threaded_writers_do_not_lose_history(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "progress.json"
            writers = [ProgressWriter(path), ProgressWriter(path)]
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda i: writers[i % 2].append_history({"epoch": i}), range(40)))
            saved = json.loads(path.read_text())
            self.assertEqual(sorted(row["epoch"] for row in saved["history"]), list(range(40)))

    def test_brief_windows_reader_lock_retries_atomic_replacement(self):
        import os
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "progress.json"
            ProgressWriter(path).write(epoch=1)
            replace = os.replace
            attempts = []

            def briefly_locked(source, target):
                attempts.append(1)
                if len(attempts) == 1:
                    raise PermissionError("Simulated Windows open reader")
                replace(source, target)

            with patch("pipeline.training_progress.os.replace", side_effect=briefly_locked), \
                    patch("pipeline.training_progress.time.sleep"):
                ProgressWriter(path).write(epoch=2)
            self.assertEqual(len(attempts), 2)
            self.assertEqual(json.loads(path.read_text())["epoch"], 2)
            self.assertEqual([], list(path.parent.glob("*.tmp")))


class MonitorServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.html = self.root / "page.html"
        self.html.write_text("<!doctype html><p>Local training</p>", encoding="utf-8")
        self.progress = self.root / "progress.json"
        self.server = MonitorServer(self.progress, self.html, port=0)
        try:
            self.server.start()
        except PermissionError as exc:
            self.skipTest("Runtime does not permit a loopback listener: " + str(exc))
        self.addCleanup(self.server.close)

    def request(self, path="/", host=None, method="GET"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=2)
        try:
            headers = {} if host is None else {"Host": host}
            connection.request(method, path, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_only_configured_routes_are_readable(self):
        self.assertEqual(self.server._server.server_address[0], "127.0.0.1")
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.html.read_bytes())
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.request("/training-monitor.html")[0], 200)
        status, _, body = self.request("/progress.json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "waiting")
        ProgressWriter(self.progress).write(stage="training", epoch=2)
        self.assertEqual(json.loads(self.request("/progress.json?fresh=1")[2])["epoch"], 2)
        for path in ("/../page.html", "/%2e%2e/page.html", "/page.html", "/data/", "/.env"):
            self.assertEqual(self.request(path)[0], 404, path)

    def test_host_guard_and_read_only_methods(self):
        self.assertEqual(self.request(host="attacker.invalid")[0], 403)
        self.assertEqual(self.request(host="127.0.0.1:1")[0], 403)
        self.assertEqual(self.request(host=f"localhost:{self.server.port}")[0], 200)
        self.assertEqual(self.request(method="POST")[0], 405)
        self.assertEqual(self.request("http://attacker.invalid/progress.json")[0], 403)

    def test_invalid_snapshot_is_not_published_as_valid_json(self):
        self.progress.write_text('{"loss":NaN}', encoding="utf-8")
        self.assertEqual(self.request("/progress.json")[0], 503)


if __name__ == "__main__":
    unittest.main()
