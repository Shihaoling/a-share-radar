"""Settlement must accept both daily-bar timestamp conventions."""
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy" / "tencent-preview"))
sys.path.insert(0, str(ROOT))
if sys.platform == "win32" and "fcntl" not in sys.modules:
    stub = ModuleType("fcntl")
    stub.LOCK_EX = 2
    stub.LOCK_UN = 8
    stub.LOCK_NB = 4
    stub.flock = lambda *args, **kwargs: None
    sys.modules["fcntl"] = stub

from pipeline.calendar import SHANGHAI  # noqa: E402
from cloud_research import attach_quotes  # noqa: E402

NOW = datetime(2026, 11, 24, 20, 0, tzinfo=SHANGHAI)
DAY = "2026-11-24"
AS_OF = DAY + "T15:00:00+08:00"


def dashboard():
    return {"market": [{"id": "000001.SH"}], "sectors": []}


def daily(**extra):
    bar = {"close": 3000.0, "as_of": AS_OF, "source": "stock_api"}
    bar.update(extra)
    return {"bars": {"000001.SH": {DAY: bar}}}


class AttachQuotesTests(unittest.TestCase):
    def test_evening_gap_recovery_bar_uses_available_at(self):
        board = dashboard()
        attach_quotes(board, daily(available_at=NOW.isoformat()), NOW)
        self.assertEqual(len(board["price_observations"]), 1)
        row = board["price_observations"][0]
        self.assertEqual(row["observed_at"], NOW.isoformat())
        self.assertEqual(row["price"], 3000.0)
        self.assertEqual(row["trade_date"], DAY)

    def test_legacy_fetched_at_still_wins(self):
        board = dashboard()
        attach_quotes(board, daily(fetched_at="2026-11-24T15:05:00+08:00",
                                  available_at=NOW.isoformat()), NOW)
        self.assertEqual(board["price_observations"][0]["observed_at"],
                         "2026-11-24T15:05:00+08:00")

    def test_missing_current_bar_is_skipped(self):
        board = dashboard()
        attach_quotes(board, {"bars": {"000001.SH": {"2026-11-23": {"close": 1.0}}}}, NOW)
        self.assertEqual(board["price_observations"], [])

    def test_falls_back_to_as_of_when_no_receipt_stamp(self):
        board = dashboard()
        attach_quotes(board, daily(), NOW)
        self.assertEqual(board["price_observations"][0]["observed_at"], AS_OF)


if __name__ == "__main__":
    unittest.main()
