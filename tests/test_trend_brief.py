from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy/tencent-preview"))

from trend_brief import market_inputs, validate


class TrendBriefEvidenceTests(unittest.TestCase):
    def test_post_close_publication_remains_background(self):
        board = {"id": "s", "name": "板块名称", "group": "sw", "leaders": [], "as_of": "2026-11-19",
                 "windows": {"10": {"state": 2, "score": 2.0, "start_date": "2026-11-05", "return_pct": 5.0}}}
        evidence = {"early": {"available_at": "2026-11-19T14:59:00+08:00"},
                    "late": {"available_at": "2026-11-19T15:01:00+08:00"},
                    "before": {"available_at": "2026-11-05T12:00:00+08:00"}}
        _, candidates = market_inputs({"expected_date": "2026-11-19", "market": [], "boards": [board], "stocks": {}}, evidence)
        self.assertEqual(candidates["s"]["eligible_refs"], ["early"])

    def test_unsupported_figures_and_case_citations_do_not_publish_as_facts(self):
        receipts = {"S1": {"topic": "economy", "kind": "official", "title": "经济统计",
                           "available_at": "2026-11-16T10:00:00+08:00", "evidence": "工业增加值同比增长4.8%。"},
                    "S2": {"topic": "global", "kind": "official", "lead": True, "title": "官方通报",
                           "available_at": "2026-11-19T18:00:00+08:00", "evidence": "会晤通报在收盘后发布。"}}
        candidate = {"id": "s", "name": "银行", "state": 2, "eligible_refs": ["S1"]}
        response = {"sections": {"market": {"text": "市场状态来自实际行情。", "refs": ["M"]},
                                 "economy": {"text": "工业增加值同比增长7.8%。", "refs": ["S1"]}},
                    "cases": [{"id": "s", "relation": "possible", "text": "用收盘后事件解释当日走势。", "refs": ["S2"]}]}
        actual = validate(response, receipts, {"s": candidate})
        self.assertEqual(actual["sections"]["economy"]["text"], "工业增加值同比增长4.8%。")
        self.assertEqual(actual["sections"]["global"]["refs"], ["S2"])
        self.assertEqual(actual["cases"][0]["relation"], "insufficient")
        self.assertEqual(actual["cases"][0]["refs"], [])
