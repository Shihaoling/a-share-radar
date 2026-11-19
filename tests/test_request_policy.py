import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pipeline import data_service, request_policy


class RequestPolicyTests(unittest.TestCase):
    def test_admin_limit_governs_all_apis_in_the_same_window(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "RADAR_SERVICE_GATE_FILE": str(Path(tmp) / "gate.json"),
            "RADAR_REQUEST_POLICY_FILE": str(Path(tmp) / "account" / "api-policy.json"),
        }):
            self.assertEqual(request_policy.read_policy()["limit_60s"], 100)
            self.assertEqual(request_policy.set_limit(1)["limit_60s"], 1)
            with patch("time.time", return_value=1000):
                self.assertIsNotNone(data_service.reserve_request("rt_idx_k", 5, 20)[3])
            with patch("time.time", return_value=1030):
                self.assertEqual(data_service.reserve_request("news", 6, 20)[:3], (30, 0, "local_rate_limit"))
            with patch("time.time", return_value=1060):
                self.assertIsNotNone(data_service.reserve_request("news", 6, 20)[3])
            for invalid in (0, 101, True, 1.1, "100", None):
                with self.assertRaises(ValueError):
                    request_policy.set_limit(invalid)
            self.assertEqual(request_policy.read_policy()["limit_60s"], 1)
            self.assertEqual(request_policy.read_policy()["expires_at"], "2026-12-20T22:06:00+08:00")
