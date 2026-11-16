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


if __name__ == "__main__":
    unittest.main()
