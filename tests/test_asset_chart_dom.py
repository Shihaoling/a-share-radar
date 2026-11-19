"""Run offline DOM, keyboard, pointer and SVG rendering checks for the shared asset chart."""
from pathlib import Path
import shutil
import subprocess
import unittest


class AssetChartFrontendTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is required for chart DOM checks")
    def test_chart_interaction_and_discontinuities(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([shutil.which("node"), "tests/asset_chart_dom.js"], cwd=root,
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
