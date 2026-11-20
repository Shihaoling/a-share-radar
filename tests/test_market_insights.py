"""Test evidence binding, quote selection and supplemental news eligibility."""

from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

if sys.platform == "win32":
    raise unittest.SkipTest("The cloud workers use POSIX file locking")

with patch.object(sys, "path", [str(Path(__file__).resolve().parents[1] / "deploy/tencent-preview"), *sys.path]):
    import market_insights as insights

DAY = "2026-11-20"
NOW = datetime.fromisoformat(DAY + "T16:00:00+08:00")


class MarketInsightsTests(unittest.TestCase):
    def test_current_index_close_and_verified_industry_estimate(self):
        catalog = {"market":[{"id":"sh000001","name":"上证指数"}],
                   "sectors":[{"id":"801780.SI","name":"银行","taxonomy":"SW2021"}]}
        daily = {"bars":{"sh000001":{DAY:{"change_pct":-1}}, "801780.SI":{"2026-11-19":{"change_pct":12}}}}
        live = {"market":[{"id":"sh000001","quote_timestamp":DAY+"T14:50:00+08:00","change_pct":2}]}
        stocks = {"board_estimates":[{"id":"801780.SI","quote_timestamp":DAY+"T15:00:00+08:00",
                                     "estimate_phase":"close","change_pct":3}]}
        data = insights.headline_inputs(NOW, daily, live, stocks, catalog)
        self.assertEqual((data["indices"][0]["change_pct"], data["indices"][0]["basis"]), (-1,"正式收盘"))
        self.assertEqual((data["industries"][0]["change_pct"], data["industries"][0]["basis"]), (3,"盘后权重股估算"))
        self.assertEqual(insights.breadth([{"change_pct": .0001},{"change_pct": 0},{"change_pct": -.015}]),
                         {"up":1,"down":1,"flat":1})
        daily["bars"]["801780.SI"][DAY] = {"change_pct":1}
        data = insights.headline_inputs(NOW, daily, live, stocks, catalog)
        self.assertEqual((data["industries"][0]["change_pct"], data["industries"][0]["basis"]), (1,"正式收盘"))

    def test_theme_payload_binds_each_impact_to_its_own_article(self):
        inputs = {"A1":("first", {"E0001":"生猪供应下降。"}, {}), "A2":("second", {"E0001":"区域降雨增加。"}, {})}
        names = {"885573.TI":"猪肉"}
        row = {"id":"A1","tone":"neutral","impacts":[{"code":"885573.TI","direction":"positive",
                "reason":"供应变化影响猪价。","evidence":["E0001"]}]}
        value = {"items":[row, {"id":"A2","tone":"neutral","impacts":[]}]}
        validated = insights.validate_themes(value, inputs, names)["items"]
        self.assertEqual(validated["A1"]["impacts"][0]["evidence"], ["生猪供应下降。"])
        self.assertEqual(validated["A2"]["impacts"], [])
        row["impacts"][0]["evidence"] = ["E0002"]
        with self.assertRaises(ValueError):
            insights.validate_themes(value, inputs, names)
        with self.assertRaises(ValueError):
            insights.validate_themes({"items": [value["items"][1]]}, inputs, names)

    def test_supplemental_queue_accepts_provider_ids_and_excludes_prior_days(self):
        with TemporaryDirectory() as folder:
            state = Path(folder)
            (state/"news").mkdir()
            with closing(sqlite3.connect(state/"news/news.sqlite3")) as db, db:
                db.execute("CREATE TABLE articles(id TEXT,day TEXT,published TEXT,payload TEXT)")
                entries = [
                    ("stock-api-news-2feb07a48c601f5ac777b8a9", DAY, "official", "14:50"),
                    ("story_yesterday", "2026-11-19", "official", "14:50"),
                    ("story_rumor", DAY, "rumor", "14:50"),
                    ("story_future", DAY, "reliable", "16:50"),
                ]
                for ident, day, source, clock in entries:
                    stamp = day + "T" + clock + ":00+08:00"
                    db.execute("INSERT INTO articles VALUES (?,?,?,?)",
                               (ident,day,stamp,json.dumps({"published_at":stamp,"credibility":source})))
            with patch.object(insights,"STATE",state):
                self.assertEqual([ident for ident,_ in insights.eligible_news(NOW)], [entries[0][0]])
