from copy import deepcopy
from datetime import datetime
import unittest

from pipeline.validation import validate_prices


NOW = datetime.fromisoformat("2026-11-16T16:00:00+08:00")


def observation(provider="eastmoney", price=1000, **kwargs):
    return {"entity_id": "chip", "provider": provider, "field": "close", "trade_date": "2026-11-16",
            "price": price, "adjustment": "none", "observed_at": "2026-11-16T15:50:00+08:00", **kwargs}


def dashboard(*rows):
    return {"market": [], "sectors": [{"id": "chip", "price": 1000}], "price_observations": list(rows)}


class PriceValidationTests(unittest.TestCase):
    def check(self, data, now=NOW):
        return validate_prices(data, now=now)

    def test_agreeing_close_verified_and_input_is_not_mutated(self):
        data = dashboard(observation(), observation("tencent", 1000.3))
        original = deepcopy(data)
        result = self.check(data)
        check = result["sectors"][0]["validation"]
        self.assertEqual(check["status"], "verified")
        self.assertEqual(check["independent_source_count"], 2)
        self.assertAlmostEqual(check["max_abs_diff"], .3)
        self.assertEqual(result["validation_summary"]["verified"], 1)
        self.assertEqual(result["metadata"]["data_checks"], result["data_checks"])
        self.assertEqual(data, original)

    def test_conflicting_close_preserves_evidence_and_blocks_price(self):
        result = self.check(dashboard(observation(), observation("tencent", 1001)))
        entity = result["sectors"][0]
        self.assertEqual(entity["validation"]["status"], "conflict")
        self.assertEqual(entity["data_quality"], "conflict")
        self.assertEqual(len(entity["validation"]["evidence"]), 2)
        self.assertEqual(entity["validation"]["max_abs_diff"], 1)

    def test_minimum_tick_and_relative_tolerance_boundary(self):
        self.assertEqual(self.check(dashboard(observation(price=1), observation("sina", 1.01)))["data_checks"][0]["status"], "verified")
        self.assertEqual(self.check(dashboard(observation(price=1), observation("sina", 1.02)))["data_checks"][0]["status"], "conflict")
        self.assertEqual(self.check(dashboard(observation(), observation("sina", 1000.5)))["data_checks"][0]["status"], "verified")

    def test_unadjusted_alias_and_etf_tick(self):
        data = dashboard(observation(price=1), observation("sina", price=1.005))
        data["sectors"][0].update(basis="etf", adjustment="unadjusted")
        check = self.check(data)["data_checks"][0]
        self.assertEqual(check["status"], "conflict")
        self.assertEqual(check["minimum_tick"], .001)
        data["sectors"][0]["basis"] = "index"
        self.assertEqual(self.check(data)["data_checks"][0]["status"], "verified")

    def test_different_dates_never_corroborate(self):
        result = self.check(dashboard(observation(), observation("sina", trade_date="2026-11-13")))
        self.assertEqual(result["data_checks"][0]["status"], "single_source")
        self.assertEqual(result["data_checks"][0]["independent_source_count"], 1)

    def test_old_day_agreement_is_not_current_verification(self):
        data = dashboard(observation(trade_date="2026-11-13"), observation("sina", trade_date="2026-11-13"))
        result = self.check(data)
        self.assertEqual(result["data_checks"][0]["status"], "not_comparable")
        self.assertEqual(result["data_checks"][0]["target_date"], "2026-11-16")
        self.assertEqual(result["data_checks"][0]["actual_date"], "2026-11-13")
        self.assertEqual(result["data_checks"][0]["latest_comparable_date"], "2026-11-13")

    def test_overnight_and_holiday_use_recent_completed_trading_date(self):
        data = dashboard(observation(trade_date="2026-11-13", observed_at="2026-11-13T16:00:00+08:00"),
                         observation("sina", trade_date="2026-11-13", observed_at="2026-11-13T16:00:00+08:00"))
        for value in ("2026-11-16T08:00:00+08:00", "2026-11-15T10:00:00+08:00"):
            self.assertEqual(self.check(data, datetime.fromisoformat(value))["data_checks"][0]["status"], "verified")

    def test_same_upstream_via_different_libraries_is_single_source(self):
        data = dashboard(observation("akshare", upstream_provider="eastmoney"), observation("eastmoney-http"),
                         observation("akshare.stock_zh_a_spot_em"))
        check = self.check(data)["data_checks"][0]
        self.assertEqual(check["status"], "single_source")
        self.assertEqual(check["providers"], ["eastmoney"])
        self.assertEqual(sum(e["exclusion_reason"] == "duplicate_upstream" for e in check["evidence"]), 2)

    def test_library_without_upstream_does_not_count_as_independent(self):
        result = self.check(dashboard(observation(), observation("akshare")))
        self.assertEqual(result["data_checks"][0]["status"], "single_source")

    def test_adjusted_and_unadjusted_prices_are_not_mixed(self):
        result = self.check(dashboard(observation(), observation("sina", adjustment="qfq")))
        self.assertEqual(result["data_checks"][0]["status"], "single_source")
        self.assertEqual(result["data_checks"][0]["comparison_count"], 0)

    def test_proxy_and_index_are_separate_entities(self):
        data = dashboard(observation(), observation("sina", price=1.2, entity_id="chip:proxy:512480"))
        data["sectors"][0]["proxy"] = {"code": "512480", "price": 1.2}
        result = self.check(data)
        sector = result["sectors"][0]
        self.assertEqual(sector["validation"]["status"], "single_source")
        self.assertEqual(sector["proxy"]["validation"]["status"], "single_source")
        self.assertEqual(result["validation_summary"]["conflict"], 0)

    def test_realtime_requires_market_times_within_sixty_seconds(self):
        now = datetime.fromisoformat("2026-11-16T10:05:00+08:00")
        common = {"field": "last", "observed_at": "2026-11-16T10:05:00+08:00"}
        data = dashboard(observation(market_timestamp="2026-11-16T10:04:00+08:00", **common),
                         observation("sina", market_timestamp="2026-11-16T10:05:00+08:00", **common))
        self.assertEqual(self.check(data, now)["data_checks"][0]["status"], "verified")
        data["price_observations"][0]["market_timestamp"] = "2026-11-16T10:03:59+08:00"
        self.assertEqual(self.check(data, now)["data_checks"][0]["status"], "not_comparable")

    def test_retrieval_only_never_verifies_live_quotes(self):
        now = datetime.fromisoformat("2026-11-16T10:05:00+08:00")
        common = {"field": "last", "observed_at": now.isoformat(), "market_timestamp": now.isoformat(), "timestamp_quality": "retrieved_only"}
        result = self.check(dashboard(observation(**common), observation("sina", **common)), now)
        self.assertEqual(result["data_checks"][0]["status"], "not_comparable")

    def test_yesterday_close_cannot_verify_current_intraday_price(self):
        now = datetime.fromisoformat("2026-11-16T10:05:00+08:00")
        common = {"trade_date": "2026-11-13", "observed_at": "2026-11-16T10:00:00+08:00"}
        result = self.check(dashboard(observation(**common), observation("sina", **common)), now)
        self.assertEqual(result["data_checks"][0]["status"], "not_comparable")
        self.assertEqual(result["data_checks"][0]["field"], "last")

    def test_unknown_calendar_fails_closed(self):
        result = self.check(dashboard(observation(), observation("sina")), datetime.fromisoformat("2027-01-04T16:00:00+08:00"))
        self.assertEqual(result["data_checks"][0]["status"], "not_comparable")
        self.assertIsNone(result["validation_summary"]["target_date"])


if __name__ == "__main__":
    unittest.main()
