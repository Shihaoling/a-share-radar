from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pipeline.publish import health_summary, publish_view


def fixture():
    history = [{"date": str(i), "close": i + 1, "open": i + .5} for i in range(300)]
    entity = {"id": "801010.SI", "name": "农业", "price": 300, "quote_date": "2026-11-16",
              "history": history, "market_cap_history": [{"date": str(i), "value": i * 100} for i in range(300)],
              "source_history": {"stock_api": history}, "feature_history": [{"private": True}],
              "sentiment_history": [{"private": True}], "constituents": [{"code": "000001"}] * 1000,
              "membership_count": 1000, "membership_complete": True,
              "sentiment": {"label": "unknown", "score": None, "article_count": 0},
              "stocks": [{"code": "000001", "price": 12.5, "daily_basic_history": [{"private": True}]}],
              "validation": {"status": "single_source"}, "quant_metrics": {"pe_ttm": {"value": None}}}
    return {"schema_version": 1, "generated_at": "2026-11-16T20:00:00+08:00", "market": [deepcopy(entity)], "sectors": [entity],
            "history": {"801010.SI": history}, "archived_sectors": [entity], "sentiment_state": {"cache": "private"},
            "intraday": {"801010.SI": {"points": [{"price": i, "timestamp": str(i)} for i in range(2000)], "latest": {"price": 1999}}},
            "stock_api_state": {"requests_this_run": 12, "members": {"private": True}, "daily_basic": {"records": [1] * 6000},
                                "financial_status": "vendor_ratios_only", "classification": {"complete": True, "records": [1] * 31},
                                "endpoints": {"news": {"status": "cooldown", "error_code": "api_denied", "cooldown_until": "later"}}},
            "models": {"status": "active", "by_target": {"a": {"champion": "ridge", "sample_count": 188, "cached_outcome": {"private": True}}}},
            "news": [{"id": "n", "url": None, "title": "真实标题"}], "predictions": [{"status": "pending", "predicted_price": None}],
            "ledger": [{"id": "old", "actual_price": 10}], "metrics": {"settled_count": 1},
            "daily_review": {"direction_accuracy": None}, "sentiment_summary": {"model_used": True, "model_cache_used_count": 50, "model_inference_count": 0},
            "sources": [{"id": "stock_api", "status": "success"}], "future_ui_field": {"keep": True}}


class PublishTests(unittest.TestCase):
    def test_preserves_values_and_input_while_omitting_backend_duplicates(self):
        source = fixture()
        before = deepcopy(source)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "view.json"
            view = publish_view(source, path)
            self.assertEqual(json.loads(path.read_text()), view)
            self.assertNotIn('\n  ', path.read_text())
        self.assertEqual(source, before)
        for key in ("news", "predictions", "ledger", "metrics", "daily_review", "sources", "future_ui_field"):
            self.assertEqual(view[key], source[key])
        self.assertEqual(view["sectors"][0]["history"], source["sectors"][0]["history"][-252:])
        self.assertEqual(view["sectors"][0]["market_cap_history"], source["sectors"][0]["market_cap_history"][-252:])
        self.assertEqual(view["sectors"][0]["stocks"][0]["price"], 12.5)
        for key in ("source_history", "feature_history", "sentiment_history", "constituents"):
            self.assertNotIn(key, view["sectors"][0])
        for key in ("archived_sectors", "history", "sentiment_state"):
            self.assertNotIn(key, view)
        self.assertNotIn("cached_outcome", view["models"]["by_target"]["a"])
        self.assertNotIn("daily_basic", view["stock_api_state"])
        self.assertNotIn("members", view["stock_api_state"])
        self.assertEqual(view["stock_api_state"]["classification"]["cached_count"], 31)
        self.assertEqual(view["stock_api_state"]["endpoints"], source["stock_api_state"]["endpoints"])

    def test_daily_close_keeps_its_own_evaluation_and_ledger(self):
        source = fixture()
        daily = {"schema_version": 1, "status": "ready", "model_id": "daily-local",
                 "target": "next_close_return_pct", "target_date": "2026-11-17",
                 "unit": "percentage_points", "mae_target": .1,
                 "market": [{"id": "sh000300", "predicted_return_pct": .2}],
                 "sectors": [{"id": "801010.SI", "relative_return_pct": -.1}],
                 "evaluation": {"historical_research": True,
                                "market": {"mae_pct": .5, "baseline_mae_pct": .6, "target_met": False,
                                           "by_id": {"sh000300": {"mae_pct": .5}}}},
                 "ledger": [{"id": "new-close", "predicted_return_pct": .2}]}
        source["daily_forecast"] = deepcopy(daily)
        before = deepcopy(source)
        with tempfile.TemporaryDirectory() as tmp:
            view = publish_view(source, Path(tmp) / "view.json")
        self.assertEqual(view["daily_forecast"], daily)
        self.assertEqual(view["ledger"], source["ledger"])
        self.assertEqual(view["predictions"], source["predictions"])
        self.assertEqual(source, before)
        health = health_summary(source)["daily_forecast_summary"]
        self.assertEqual(health["target"], "next_close_return_pct")
        self.assertEqual(health["market_count"], 1)
        self.assertEqual(health["evaluation"]["market"]["mae_pct"], .5)
        self.assertFalse(health["evaluation"]["market"]["target_met"])
        self.assertNotIn("ledger", health)
        self.assertNotIn("by_id", health["evaluation"]["market"])

    def test_all_intraday_points_are_preserved(self):
        source = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            view = publish_view(source, Path(tmp) / "view.json")
        self.assertEqual(view["intraday"], source["intraday"])
        self.assertEqual(len(view["intraday"]["801010.SI"]["points"]), 2000)

    def test_chart_rows_drop_redundant_payload_without_changing_quotes_or_formulas(self):
        source = fixture()
        entity = source["sectors"][0]
        entity["history"][-1].update(provider="stock_api", source="stock_api", as_of="2026-11-16T15:00:00+08:00",
            volume=12000, volume_unit="万股", amount=12345.67, amount_unit="CNY_10000", adjustment="unadjusted",
            complete=True, change_pct=1.25, previous_close=296.3, vendor_pe=12, vendor_pb=2,
            ts_code="801010.SI", name="重复名称", valuation_available_at="later", source_url="https://example.test")
        entity["market_cap_history"][-1].update(market_cap=29900, source="stock_api", currency="CNY", fetched_at="later")
        entity["vendor_pe"] = 12
        metric = {"value": 1.2, "sample_count": 200, "reason": "有效样本", "coverage": {"used_count": 200},
                  "rf_annual": .02, "ddof": 1, "mean_daily_excess_return": .0001,
                  "sample_daily_standard_deviation": .002, "exclusions": [{"constituent": "a", "reason": "missing"}]}
        entity["quant_metrics"] = {"sharpe": deepcopy(metric), "help": {"sharpe": {"formula": "sqrt(252)*mean/std", "caveats": "历史不保证未来"}}}
        source["data_checks"] = [{"status": "single_source"}]
        source["metadata"] = {"data_checks": deepcopy(source["data_checks"]), "validation_summary": {"count": 1}}
        before = deepcopy(source)
        with tempfile.TemporaryDirectory() as tmp:
            view = publish_view(source, Path(tmp) / "view.json")
        published = view["sectors"][0]
        for key in ("date", "open", "close", "volume", "volume_unit", "amount", "amount_unit", "as_of", "provider",
                    "source", "adjustment", "complete", "change_pct", "previous_close"):
            self.assertEqual(published["history"][-1][key], entity["history"][-1][key])
        for key in ("vendor_pe", "vendor_pb", "ts_code", "name", "valuation_available_at", "source_url"):
            self.assertNotIn(key, published["history"][-1])
        self.assertEqual(published["market_cap_history"][-1], {"date": "299", "value": 29900})
        self.assertEqual(published["vendor_pe"], 12)
        for key, value in metric.items():
            if key != "exclusions":
                self.assertEqual(published["quant_metrics"]["sharpe"][key], value)
        self.assertEqual(published["quant_metrics"]["sharpe"]["exclusion_count"], 1)
        self.assertEqual(published["quant_metrics"]["help"], entity["quant_metrics"]["help"])
        self.assertNotIn("data_checks", view["metadata"])
        self.assertEqual(view["data_checks"], source["data_checks"])
        self.assertEqual(source, before)

    def test_failed_atomic_replace_keeps_previous_file_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "view.json"
            path.write_text('{"previous":true}')
            with patch("pipeline.publish.os.replace", side_effect=OSError("test")):
                with self.assertRaises(OSError):
                    publish_view(fixture(), path)
            self.assertEqual(json.loads(path.read_text()), {"previous": True})
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["view.json"])
            with self.assertRaises(ValueError):
                publish_view({"price": float("nan")}, path)
            self.assertEqual(json.loads(path.read_text()), {"previous": True})

    def test_health_reports_actual_status_and_sentiment_cache_fields(self):
        source = fixture()
        summary = health_summary(source)
        self.assertEqual(summary["requests_this_run"], 12)
        self.assertEqual(summary["endpoints"]["news"]["next_retry"], "later")
        self.assertEqual(summary["sectors"][0]["bars"], 300)
        self.assertEqual(summary["sectors"][0]["constituents"], 1000)
        self.assertEqual(summary["prediction_status_counts"], {"pending": 1})
        self.assertEqual(summary["sentiment_summary"], source["sentiment_summary"])
        self.assertNotIn("private", json.dumps(summary))
        json.dumps(summary, allow_nan=False)

    def test_fund_catalogs_and_model_private_paths_are_not_published(self):
        source = fixture()
        marker = "RAW_FUND_CATALOG_MUST_STAY_PRIVATE"
        source["fund_state"] = {"version": 1, "status": "partial", "requests_this_run": 3,
            "last_run_at": "2026-11-16T20:00:00+08:00", "future_private_state": [marker],
            "cache": {"etf_basic": {"fetched_at": "2026-11-16T19:00:00+08:00", "target_date": None,
                                    "rows": [{"name": marker}] * 6000}},
            "endpoints": {"etf_share_size": {"status": "cooldown", "error_code": "api_denied",
                                            "cooldown_until": "2026-11-17T20:00:00+08:00", "raw": marker}}}
        source["fund_summary"] = {"status": "partial", "covered_sectors": 20, "candidate_count": 99}
        fund = {"code": "510001.SH", "price": 1.2, "price_date": "2026-11-16", "nav": 1.1,
                "nav_date": "2026-11-13", "aum_cny": 2e9, "match_status": "index_name_candidate"}
        source["sectors"][0]["funds"] = [fund]
        source["sectors"][0]["funds_note"] = "主题关联，持仓待核验。"
        source["equity_collection"] = {"status": "partial", "cached_days": 5, "missing_days": 10,
            "requests_this_run": 2, "data_dir": "/private/workspace/training", "raw": marker}
        before = deepcopy(source)
        with tempfile.TemporaryDirectory() as tmp:
            view = publish_view(source, Path(tmp) / "view.json")
        public_state = view["fund_state"]
        self.assertNotIn("cache", public_state)
        self.assertNotIn("future_private_state", public_state)
        self.assertEqual(public_state["cache_summary"]["etf_basic"]["cached_count"], 6000)
        self.assertEqual(public_state["endpoints"]["etf_share_size"]["next_retry"], "2026-11-17T20:00:00+08:00")
        self.assertEqual(view["sectors"][0]["funds"], [fund])
        self.assertEqual(view["fund_summary"], source["fund_summary"])
        self.assertEqual(view["equity_collection"]["cached_days"], 5)
        self.assertNotIn("data_dir", view["equity_collection"])
        self.assertNotIn(marker, json.dumps(view))
        self.assertEqual(source, before)

    def test_health_keeps_fund_and_equity_coverage_without_full_rows(self):
        source = fixture()
        source["fund_state"] = {"status": "success", "requests_this_run": 3,
            "cache": {"etf_basic": {"rows": [{"private_catalog": True}] * 200}}}
        source["fund_summary"] = {"status": "success", "covered_sectors": 19, "candidate_count": 86}
        source["equity_collection"] = {"status": "partial", "cached_days": 40, "missing_days": 140,
                                       "requests_this_run": 2, "data_dir": "/private/training"}
        source["equity_forecast"] = {"status": "partial", "target_date": "2026-11-17", "target_kind": "next_open",
            "coverage": {"universe_count": 5000, "predicted_count": 1000, "weight_coverage": .2},
            "stocks": [{"code": str(i), "status": "pending", "private_feature": 5} for i in range(5000)],
            "evaluation": {"settled_count": 900, "mae_pct": 1.5, "direction_accuracy": .51, "daily": [{"private": True}]},
            "training": {"status": "baseline", "promoted": False, "parameters": {"private": True}}}
        summary = health_summary(source)
        self.assertEqual(summary["fund_summary"]["covered_sectors"], 19)
        self.assertEqual(summary["fund_summary"]["state"]["cache_summary"]["etf_basic"]["cached_count"], 200)
        equity = summary["equity_summary"]
        self.assertEqual(equity["coverage"]["weight_coverage"], .2)
        self.assertEqual(equity["stock_status_counts"], {"pending": 5000})
        self.assertEqual(equity["collection"]["missing_days"], 140)
        self.assertEqual(equity["evaluation"]["settled_count"], 900)
        self.assertNotIn("stocks", equity)
        self.assertNotIn("private", json.dumps(summary))
        json.dumps(summary, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
