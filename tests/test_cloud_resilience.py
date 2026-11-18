"""Offline regressions for the cloud workers that share upstream inputs."""
from contextlib import nullcontext
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from pipeline import minimax_news

with patch.object(sys, "path", [str(Path(__file__).resolve().parents[1] / "deploy/tencent-preview"), *sys.path]):
    import market_daily
    import cloud_models


class CloudResilienceTests(unittest.TestCase):
    def test_minimax_quota_is_shared_until_retry_after(self):
        with TemporaryDirectory() as root, patch.dict(os.environ, {
            "RADAR_MINIMAX_COOLDOWN_DB": str(Path(root) / "new/guard.sqlite3")
        }), patch.object(minimax_news, "_infer_article") as upstream, patch.object(minimax_news.time, "time") as now:
            now.return_value = 1000
            upstream.side_effect = minimax_news.ModelFailure("http_429_plan_or_quota", 900)
            with self.assertRaisesRegex(minimax_news.ModelFailure, "http_429"):
                minimax_news.infer_article({}, {}, "fixture")
            now.return_value = 1005
            with self.assertRaisesRegex(minimax_news.ModelFailure, "shared_model_cooldown") as blocked:
                minimax_news.infer_article({}, {}, "fixture")
            self.assertEqual(blocked.exception.retry, 895)
            self.assertEqual(upstream.call_count, 1)
            now.return_value = 1900
            upstream.side_effect = None
            upstream.return_value = {"ready": True}
            self.assertEqual(minimax_news.infer_article({}, {}, "fixture"), {"ready": True})

    def test_daily_partial_stock_batch_gets_one_bounded_followup(self):
        now = datetime.fromisoformat("2026-11-18T20:00:00+08:00")
        codes = ["000001.SZ", "000002.SZ"]
        def row(code):
            return dict(ts_code=code, trade_date="20261118", close=10, total_mv=100, pe_ttm=5, pb=2)
        with TemporaryDirectory() as root, patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": root}):
            base = Path(root)
            (base / "sxdata-token").write_text("fixture")
            source = base / "view.json"
            source.write_text(json.dumps({"market": [], "sectors": [{"id":"example", "stocks":[{"ts_code":c} for c in codes]}]}))
            client = Mock()
            client.request.side_effect = [[row(codes[0])], [row(codes[1])]]
            with patch.object(market_daily, "SOURCE", source), patch.object(market_daily, "OUTPUT", base/"daily.json"), \
                 patch.object(market_daily, "shanghai_now", return_value=now), \
                 patch.object(market_daily, "relay_client", return_value=nullcontext(client)):
                for _ in range(3):
                    market_daily.refresh()
                self.assertEqual(set(market_daily.read_daily()["stocks"]), set(codes))
                self.assertEqual(client.request.call_count, 2)

    def test_older_partial_cap_overlay_preserves_newer_quote_and_history(self):
        entity = {"id":"m", "market_cap_date":"2026-11-17", "market_cap":20,
                  "market_cap_history":[{"date":"2026-11-17","value":20}]}
        market_daily.merge_daily({"market":[entity]}, {"bars":{}, "stocks":{}, "market_caps":{"m":{"2026-11-16":10}}})
        self.assertEqual(entity["market_cap"], 20)
        self.assertEqual(len(entity["market_cap_history"]), 2)

    def test_model_factors_only_use_dated_current_nonrumor_articles(self):
        now = datetime.fromisoformat("2026-11-18T14:00:00+08:00")
        article = {"llm_analysis":{"status":"ready","sector_impacts":[{"sector_code":"example","direction":"positive"}]}}
        with TemporaryDirectory() as root, patch.object(cloud_models, "STATE", Path(root)):
            out = Path(root) / "news-output"
            out.mkdir()
            (out / "news.json").write_text(json.dumps({"trade_date":"2026-11-18", "model_revision":"r1",
                "articles":[article, {**article, "credibility":"rumor"}], "recent_articles":[article]}))
            self.assertEqual(cloud_models.latest_news_factors(now), ("r1", {"example":{"mean":1, "count":1}}))
