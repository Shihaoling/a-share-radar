"""Run the mounted sector details page against the published file contracts."""
from pathlib import Path
import shutil
import subprocess
import unittest


class SectorDetailTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node required for sector details DOM checks")
    def test_publications_and_member_recovery(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([shutil.which("node"), "tests/sector_detail_dom.js"], cwd=root,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node required for asset research DOM checks")
    def test_asset_research(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([shutil.which("node"), "tests/asset_research_dom.js"], cwd=root,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
