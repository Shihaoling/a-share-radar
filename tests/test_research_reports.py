from contextlib import closing
from datetime import date
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pipeline.minimax_prompts import DEFAULT_EFFORT

if sys.platform == "win32":
    raise unittest.SkipTest("The report worker uses the cloud relay")

with patch.object(sys,"path",[str(Path(__file__).resolve().parents[1]/"deploy/tencent-preview"),*sys.path]):
    import research_report_library as library


class ReportLibraryTests(unittest.TestCase):
    def test_catalog_deduplicates_originals_and_publishes_newest_first(self):
        rows = [
            {"trade_date":"20261116","title":"较早研报","ind_name":"通信",
             "url":"https://pdf.dfcfw.com/pdf/first.pdf"},
            {"trade_date":"20261120","title":"光模块订单","ind_name":"通信","abstr":"券商提供的原始材料",
             "url":"https://pdf.dfcfw.com/pdf/second.pdf"},
            {"trade_date":"20261120","title":"光模块订单","url":"http://pdf.dfcfw.com/pdf/second.pdf?cache=1"},
        ]
        with TemporaryDirectory() as folder, closing(sqlite3.connect(":memory:")) as db:
            db.executescript(library.SCHEMA)
            library.collect(db,lambda _:rows,date(2026,11,16),date(2026,11,22))
            path=Path(folder)
            library.publish_index(db,path,include_pages=True)
            index=json.loads((path/"index.json").read_bytes())
            page=json.loads((path/"pages/1.json").read_bytes())
            item=json.loads((path/"items"/(page["reports"][0]["id"]+".json")).read_bytes())
        self.assertEqual(index["count"],2)
        self.assertEqual(index["groups"][0]["name"],"CPO")
        self.assertEqual([row["date"] for row in page["reports"]],["2026-11-20","2026-11-16"])
        self.assertNotIn("source_abstract",item["report"])
        self.assertEqual(item["report"]["url"],"https://pdf.dfcfw.com/pdf/second.pdf")

    def test_truncated_model_output_retries_as_smaller_xhigh_batches(self):
        batch = [
            {"id":"a","title":"研报甲","broker":"机构甲","date":"2026-11-20",
             "source_abstract":"可靠券商原摘要"*40,"url":"https://pdf.dfcfw.com/pdf/one.pdf"},
            {"id":"b","title":"研报乙","broker":"机构乙","date":"2026-11-21",
             "source_abstract":"","url":"https://pdf.dfcfw.com/pdf/two.pdf"},
        ]
        def infer(article, config, key, *, prompt, stage, validator):
            self.assertEqual(DEFAULT_EFFORT[stage],"xhigh")
            self.assertEqual(config["reasoning_effort"],"xhigh")
            inputs=article["digest_articles"]
            if len(inputs)==2:
                raise library.ModelFailure("incomplete_response",1800)
            return validator({"items":[{"id":inputs[0]["id"],"summary":"根据实质材料整理完整摘要。"}]})
        with patch.object(library,"infer_article",side_effect=infer) as model, \
             patch.object(library,"pdf_excerpt",return_value="已从研报正文读取材料。"):
            results,_=library.summarize_batch(batch,"test-key")
        self.assertEqual(model.call_count,3)
        self.assertEqual(results["a"]["basis"],"source_abstract")
        self.assertEqual(results["b"]["basis"],"pdf_excerpt")


if __name__ == "__main__":
    unittest.main()
