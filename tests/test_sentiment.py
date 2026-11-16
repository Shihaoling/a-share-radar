from copy import deepcopy
from datetime import datetime
import unittest
from unittest.mock import patch

from pipeline.sentiment import aggregate_entity_sentiment, SW2021, MODEL_REVISION, MODEL_ID


NOW = datetime.fromisoformat("2026-11-17T10:00:00+08:00")


def news(title, **kwargs):
    return {"id": title, "title": title, "source": "测试来源", "url": None, "summary": "", "credibility": "trusted",
            "first_seen_at": "2026-11-17T09:00:00+08:00", "published_at": "2026-11-17T08:30:00+08:00", **kwargs}


def dashboard(*items):
    return {"market": [{"id": "000300.SH", "name": "沪深300"}],
            "sectors": [{"id": "801080.SI", "code": "801080.SI", "name": "电子", "taxonomy": "SW2021", "level": "L1",
                         "constituents": [{"code": "600000", "name": "示例芯片公司"}]},
                        {"id": "801780.SI", "name": "银行", "code": "801780.SI"}], "news": list(items)}


class SentimentTests(unittest.TestCase):
    def process(self, data, now=NOW, config=None):
        return aggregate_entity_sentiment(data, now, config)

    def test_fixed_31_industries_and_keyword_association(self):
        self.assertEqual(len(SW2021), 31)
        result = self.process(dashboard(news("半导体订单增长，芯片需求回暖")))
        self.assertEqual(result["sectors"][0]["sentiment"]["label"], "positive")
        self.assertEqual(result["sectors"][1]["sentiment"]["label"], "unknown")
        self.assertIsNone(result["sectors"][1]["sentiment"]["score"])
        self.assertEqual(result["market"][0]["sentiment"]["label"], "unknown")

    def test_actual_constituent_name_and_code_link_news(self):
        for title in ("示例芯片公司利润下降", "600000 营收增长"):
            entity = self.process(dashboard(news(title)))["sectors"][0]
            self.assertEqual(entity["sentiment"]["article_count"], 1)
            self.assertTrue(any(a["method"].startswith("constituent_") for a in entity["sentiment"]["evidence"][0]["associations"]))

    def test_macro_news_goes_to_broad_index_without_forcing_all_industries(self):
        result = self.process(dashboard(news("美联储利率维持不变")))
        self.assertEqual(result["market"][0]["sentiment"]["label"], "neutral")
        self.assertTrue(all(s["sentiment"]["label"] == "unknown" for s in result["sectors"]))

    def test_future_first_seen_and_future_publication_are_excluded(self):
        data = dashboard(news("芯片订单增长", first_seen_at="2026-11-17T11:00:00+08:00"),
                         news("半导体利润增长", published_at="2026-11-18T10:00:00+08:00"))
        result = self.process(data)
        self.assertEqual(result["sectors"][0]["sentiment"]["label"], "unknown")
        self.assertEqual(result["sentiment_summary"]["eligible_count"], 0)

    def test_future_exclusion_does_not_permanently_poison_article(self):
        first = self.process(dashboard(news("芯片订单增长", first_seen_at="2026-11-17T11:00:00+08:00")))
        later = self.process(first, datetime.fromisoformat("2026-11-17T11:30:00+08:00"))
        self.assertEqual(later["sentiment_summary"]["eligible_count"], 1)
        self.assertEqual(later["sectors"][0]["sentiment"]["label"], "positive")

    def test_missing_first_seen_is_now_never_backdated(self):
        item = news("半导体订单增长", published_at="2026-11-15T09:00:00+08:00")
        item.pop("first_seen_at")
        result = self.process(dashboard(item))
        self.assertEqual(result["news"][0]["first_seen_at"], NOW.isoformat())
        self.assertEqual(result["sectors"][0]["sentiment_history"][0]["available_at"], NOW.isoformat())
        self.assertEqual(result["sectors"][0]["sentiment_history"][0]["date"], "2026-11-17")

    def test_rumor_excluded_by_default(self):
        result = self.process(dashboard(news("网传芯片需求暴跌")))
        self.assertEqual(result["sentiment_summary"]["rumors_excluded"], 1)
        self.assertEqual(result["sectors"][0]["sentiment"]["article_count"], 0)

    def test_reposts_from_multiple_sources_are_one_evidence_item(self):
        result = self.process(dashboard(news("半导体订单增长", id="one", source="来源甲"),
                                        news("半导体订单增长！", id="two", source="来源乙")))
        self.assertEqual(result["sectors"][0]["sentiment"]["article_count"], 1)
        self.assertEqual(result["sentiment_summary"]["duplicate_count"], 1)

    def test_no_url_is_preserved_without_inventing_link(self):
        evidence = self.process(dashboard(news("半导体订单增长")))["sectors"][0]["sentiment"]["evidence"][0]
        self.assertIsNone(evidence["url"])
        self.assertEqual(evidence["source"], "测试来源")

    def test_specific_loss_phrase_not_positive_growth(self):
        result = self.process(dashboard(news("芯片公司亏损增长")))
        self.assertEqual(result["news"][0]["sentiment"], "negative")
        self.assertNotIn("增长", [m["term"] for m in result["news"][0]["sentiment_evidence"]["matches"]])

    def test_negation_does_not_assert_negative_tone(self):
        result = self.process(dashboard(news("芯片订单并未下降")))
        self.assertEqual(result["news"][0]["sentiment"], "unknown")
        self.assertTrue(result["news"][0]["sentiment_evidence"]["matches"][0]["negated"])

    def test_english_financial_cost_cut_is_positive_not_generic_negative_cut(self):
        result = self.process(dashboard(news("Semiconductor makers announce cost cuts and demand recovery")))
        self.assertEqual(result["news"][0]["sentiment"], "positive")
        self.assertIn("rules", result["news"][0]["sentiment_method"])

    def test_no_signal_is_unknown_not_neutral(self):
        result = self.process(dashboard(news("芯片行业举行技术交流会议")))
        self.assertEqual(result["news"][0]["sentiment"], "unknown")
        self.assertIsNone(result["sectors"][0]["sentiment"]["score"])

    def test_generic_comprehensive_and_ecommerce_do_not_false_match_industries(self):
        data = dashboard(news("综合考虑电子商务需求增长"))
        data["sectors"].append({"id": "801230.SI", "name": "综合"})
        result = self.process(data)
        self.assertEqual(result["sectors"][0]["sentiment"]["article_count"], 0)
        self.assertEqual(result["sectors"][-1]["sentiment"]["article_count"], 0)

    def test_commodity_direction_alone_does_not_claim_positive_industry_tone(self):
        result = self.process(dashboard(news("国际油价上涨")))
        self.assertIsNone(result["news"][0]["sentiment_score"])

    def test_snapshots_are_idempotent_immutable_and_input_untouched(self):
        data = dashboard(news("半导体订单增长"))
        original = deepcopy(data)
        first = self.process(data)
        saved = deepcopy(first["sectors"][0]["sentiment_history"])
        second = self.process(first, datetime.fromisoformat("2026-11-17T10:30:00+08:00"))
        self.assertEqual(second["sectors"][0]["sentiment_history"], saved)
        self.assertEqual(data, original)
        second["news"].append(news("芯片利润下降", id="new", first_seen_at="2026-11-17T11:00:00+08:00"))
        third = self.process(second, datetime.fromisoformat("2026-11-17T11:05:00+08:00"))
        self.assertEqual(third["sectors"][0]["sentiment_history"][0], saved[0])
        self.assertEqual(len(third["sectors"][0]["sentiment_history"]), 2)

    def test_model_failure_is_visible_rules_fallback(self):
        with patch("pipeline.sentiment._finbert_scores", side_effect=ImportError("dependency missing")):
            result = self.process(dashboard(news("半导体订单增长，芯片需求回暖")), config={"enable_model": True})
        self.assertFalse(result["sentiment_summary"]["model_loaded"])
        self.assertIsNotNone(result["sentiment_summary"]["model_error"])
        self.assertIn("rules", result["news"][0]["sentiment_method"])

    def test_pinned_model_results_integrate_as_uncalibrated_text_confidence(self):
        item = news("半导体订单增长，芯片需求回暖", id="model-test")
        fake = {"model-test": {"label": "positive", "score": .7, "confidence": .8,
                                "confidence_kind": "model_softmax_uncalibrated", "method": "finbert_chinese_cpu",
                                "model_version": MODEL_ID + "@" + MODEL_REVISION,
                                "evidence": {"model_probabilities": {"neutral": .1, "positive": .8, "negative": .1}},
                                "note": "uncalibrated text classifier"}}
        with patch("pipeline.sentiment._finbert_scores", return_value=fake):
            result = self.process(dashboard(item), config={"enable_model": True})
        self.assertEqual(result["news"][0]["sentiment_score"], .7)
        self.assertEqual(result["news"][0]["sentiment_confidence_kind"], "model_softmax_uncalibrated")
        self.assertEqual(result["sentiment_summary"]["model_revision"], MODEL_REVISION)
        self.assertNotIn("up_probability", result["sectors"][0]["sentiment"])
        with patch("pipeline.sentiment._finbert_scores", side_effect=AssertionError("must not load in quotes-only")):
            reused = self.process(result, config={"sentiment": {"model_enabled": False, "reuse_only": True}})
        self.assertEqual(reused["news"][0]["sentiment_method"], "finbert_chinese_cpu")
        self.assertEqual(reused["news"][0]["sentiment_score"], .7)
        self.assertEqual(reused["sentiment_summary"]["model_inference_count"], 0)

    def test_nested_settings_and_article_limit(self):
        first = news("半导体订单增长，芯片需求回暖", id="first")
        second = news("半导体盈利增长，行业全面复苏", id="second")
        with patch("pipeline.sentiment._finbert_scores", side_effect=ImportError("not installed")) as inference:
            result = self.process(dashboard(first, second), config={"sentiment": {"model_enabled": True, "max_model_articles": 1}})
        self.assertEqual(len(inference.call_args.args[0]), 1)
        self.assertEqual(result["sentiment_summary"]["model_deferred_count"], 1)

    def test_stock_api_news_merges_into_ui_feed_and_entity_sentiment(self):
        data = dashboard(news("银行利润增长", id="existing"))
        data["stock_api_news"] = [{"datetime": "2026-11-17T09:10:00+08:00", "title": "半导体订单增长",
                                   "content": "芯片需求回暖，企业盈利改善。", "source": "cls", "source_name": "财联社快讯",
                                   "fetched_at": "2026-11-17T09:15:00+08:00", "url": None}]
        original = deepcopy(data)
        result = self.process(data)
        self.assertEqual(len(result["news"]), 2)
        self.assertEqual(result["news"][0]["id"], "existing")
        self.assertEqual(result["news"][1]["source"], "财联社快讯")
        self.assertIsNone(result["news"][1]["url"])
        self.assertEqual(result["news"][1]["first_seen_at"], "2026-11-17T09:15:00+08:00")
        self.assertEqual(result["sectors"][0]["sentiment"]["label"], "positive")
        self.assertEqual(result["sentiment_summary"]["stock_api_news_merged_count"], 1)
        self.assertEqual(data, original)

    def test_stock_api_merge_is_idempotent_and_keeps_existing_first_seen(self):
        data = dashboard(news("半导体订单增长", id="existing", first_seen_at="2026-11-17T08:00:00+08:00"))
        data["stock_api_news"] = [{"title": "半导体订单增长", "content": "同一消息转载", "source": "cls",
                                   "datetime": "2026-11-17T09:30:00+08:00", "fetched_at": "2026-11-17T09:45:00+08:00"}]
        first = self.process(data)
        second = self.process(first)
        self.assertEqual(len(second["news"]), 1)
        self.assertEqual(second["news"][0]["id"], "existing")
        self.assertEqual(second["news"][0]["first_seen_at"], "2026-11-17T08:00:00+08:00")
        self.assertEqual(second["sentiment_summary"]["stock_api_news_duplicate_count"], 1)

    def test_later_recurring_headline_is_not_suppressed_by_old_cached_article(self):
        data = dashboard(news("半导体订单增长", id="old", published_at="2026-10-17T08:30:00+08:00",
                              first_seen_at="2026-10-17T09:00:00+08:00"))
        data["stock_api_news"] = [{"title": "半导体订单增长", "source": "cls", "datetime": "2026-11-17T09:00:00+08:00",
                                   "fetched_at": "2026-11-17T09:01:00+08:00"}]
        result = self.process(data)
        self.assertEqual(len(result["news"]), 2)
        self.assertEqual(result["sectors"][0]["sentiment"]["article_count"], 1)

    def test_stock_api_future_fetch_is_not_available_early(self):
        data = dashboard()
        data["stock_api_news"] = [{"title": "半导体订单增长", "source": "cls", "datetime": "2026-11-17T09:00:00+08:00",
                                   "fetched_at": "2026-11-17T11:00:00+08:00"}]
        result = self.process(data)
        self.assertEqual(result["news"][0]["sentiment_exclusion_reason"], "future_information")
        self.assertEqual(result["sectors"][0]["sentiment"]["label"], "unknown")


if __name__ == "__main__":
    unittest.main()
