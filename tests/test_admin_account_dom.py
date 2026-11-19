"""Exercise account and administrator pages against their HTTP contracts."""
from pathlib import Path
import shutil
import subprocess
import unittest


class AdminAccountFrontend(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node required for account DOM checks")
    def test_account_and_usage_views(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([shutil.which("node"), "tests/admin_account_dom.js"], cwd=root,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
