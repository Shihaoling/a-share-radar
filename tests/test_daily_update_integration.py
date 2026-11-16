"""The existing push entry point must refresh daily inference without fitting."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pipeline import equity_update


class DailyUpdateIntegrationTests(unittest.TestCase):
    def test_offline_push_preserves_issued_records_when_bundle_is_missing(self):
        legacy_ledger = [{"id": "legacy-first-issue", "predicted_return_pct": .2}]
        legacy_predictions = [{"sector_id": "legacy", "status": "pending"}]
        daily_ledger = [{"id": "sh000300", "target_date": "2026-11-17", "predicted_return_pct": .3}]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / "web/data"
            data.mkdir(parents=True)
            (data / "dashboard.json").write_text(json.dumps({
                "data_mode": "live", "market": [], "sectors": [],
                "ledger": legacy_ledger, "predictions": legacy_predictions,
                "daily_forecast": {"ledger": daily_ledger},
            }), encoding="utf-8")
            with patch.object(equity_update, "ROOT", root), \
                    patch.dict(os.environ, {"RADAR_ALLOW_TRAINING": "false"}), \
                    patch.object(equity_update, "collect_equity_history", return_value={
                        "missing_days": 1, "available_calendar_days": 0,
                    }) as collect, \
                    patch.object(equity_update, "enrich_funds", side_effect=lambda d, *_: d), \
                    patch.object(equity_update, "update_equity_predictions", side_effect=lambda d, *_: d), \
                    patch("pipeline.train_daily_forecast.fit_asset", side_effect=AssertionError("must not fit")) as fit, \
                    redirect_stdout(io.StringIO()):
                equity_update.refresh(offline=True, max_requests=0)
            fit.assert_not_called()
            self.assertFalse(collect.call_args.args[1]["network_enabled"])
            dashboard = json.loads((data / "dashboard.json").read_text(encoding="utf-8"))
            view = json.loads((data / "view.json").read_text(encoding="utf-8"))
            self.assertEqual(dashboard["ledger"], legacy_ledger)
            self.assertEqual(dashboard["predictions"], legacy_predictions)
            self.assertEqual(dashboard["daily_forecast"]["ledger"], daily_ledger)
            self.assertEqual(dashboard["daily_forecast"]["status"], "pending")
            self.assertIn("daily_forecast", view)
            self.assertFalse((root / "data/daily_forecast/models/bundle.json").exists())


if __name__ == "__main__":
    unittest.main()
