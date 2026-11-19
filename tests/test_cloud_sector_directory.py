"""Exchange directory admission, publication isolation and timestamp recovery."""
from contextlib import ExitStack
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

if sys.platform == "win32":
    raise unittest.SkipTest("The cloud directory uses POSIX file locking")

DEPLOY = Path(__file__).resolve().parents[1] / "deploy/tencent-preview"
with patch.object(sys, "path", [str(DEPLOY), *sys.path]):
    import relay_quotes as relay
    from sector_quotes import IndexBatches, SectorFeed, catalog_codes

AI = "399284.SZ"
CHIP = "000685.SH"
CORE = "000032.SH"
DAY = "2026-11-19"


def instant(hhmmss, day=DAY):
    return datetime.fromisoformat(day+"T"+hhmmss+"+08:00")


def quote(code, hhmmss, price):
    return {"id": code, "name": code, "quote_date": DAY, "price": price, "change_pct": 1,
            "provider": "stock_api", "timestamp_quality": "source_reported",
            "quote_timestamp": instant(hhmmss).isoformat(), "amount": 12300}


class SectorDirectoryTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.output = self.base / "quote-output"
        self.catalog = self.base / "sector-catalog.json"
        relay.publish(self.catalog, {"schema_version": 1, "items": [{"code": c} for c in (AI, CHIP, CORE)]})
        self.codes = catalog_codes(self.catalog)
        self.history = sqlite3.connect(":memory:")
        self.addCleanup(self.history.close)
        self.history.execute("CREATE TABLE samples(id,minute,source_stamp,price,change_pct,PRIMARY KEY(id,minute))")

    def feed(self):
        return SectorFeed(self.base, self.output, self.codes, relay.publish)

    def read(self, name):
        return json.loads((self.output / (name + ".json")).read_bytes())

    def test_catalog_fails_closed_on_duplicates_invalid_code_or_missing_config(self):
        self.assertEqual(list(self.codes), [AI, CHIP, CORE])
        for items in [[{"code":AI},{"code":AI}], [{"code":"../../unsafe"}], [{"code":"885728.TI"}], []]:
            relay.publish(self.catalog, {"schema_version":1, "items":items})
            self.assertEqual(catalog_codes(self.catalog), {})
        self.assertEqual(catalog_codes(self.base / "missing.json"), {})

    def test_combined_batch_includes_core_and_rotates_venues_after_partial_responses(self):
        core = {"000001.SH": "sh000001", CORE: CORE}
        selector = IndexBatches(core, self.codes)
        self.assertEqual(selector.choose(1000), (core, None))
        selector.finish(1000, 2, 2)
        all_codes, query = selector.choose(1005)
        self.assertIsNone(query)
        self.assertEqual(all_codes, {**core, **self.codes})
        selector.finish(1005, 1, 4)
        self.assertEqual(selector.choose(1010), (core, None))
        selector.finish(1010, 2, 2)
        shanghai, query = selector.choose(1015)
        self.assertEqual((set(shanghai),query), ({"000001.SH",CORE,CHIP},"*.SH"))
        selector.finish(1015, 3, 3)
        selector.finish(1020, 2, 2)
        self.assertEqual(selector.choose(1025), ({AI: AI}, "*.SZ"))
        selector.finish(1025, 1, 1)
        selector.finish(1610, 2, 2)
        self.assertEqual(selector.choose(1615), (all_codes, None))

    def test_private_samples_core_history_and_late_ticks_survive_restart_and_rollover(self):
        core_stamp = instant("09:30:12").isoformat()
        self.history.execute("INSERT INTO samples VALUES (?,?,?,?,?)", (CORE, core_stamp[:16],core_stamp,55,0))
        f=self.feed()
        f.update(self.history, {CORE}, [quote(CORE,"09:30:40",60),quote(AI,"10:02:32",102),
            quote(CHIP,"09:25:15",50),quote("000001.SH","10:02:36",99)], instant("10:02:40"))
        f.update(self.history, {CORE}, [quote(AI,"10:01:57",101),quote(AI,"10:02:55",103)], instant("10:03:00"))
        live={x["id"]:x for x in self.read("sector-quotes")["sectors"]}
        self.assertEqual(set(live),set(self.codes))
        self.assertEqual(live[AI]["price"],103)
        self.assertEqual(self.read("sector-intraday/"+AI)["points"],
                         [[instant("10:01:57").isoformat(),101],[instant("10:02:32").isoformat(),102]])
        self.assertEqual(self.read("sector-intraday/"+CORE)["points"],[[core_stamp,55]])
        self.assertEqual(self.read("sector-intraday/"+CHIP)["points"],[])
        self.assertEqual(self.history.execute("SELECT count(*) FROM samples").fetchone()[0],1)
        (self.output/"sector-intraday"/(AI+".json")).unlink()
        self.feed().update(self.history, {CORE}, [], instant("10:04:00"))
        self.assertEqual(len(self.read("sector-intraday/"+AI)["points"]),2)
        f.update(self.history, {CORE}, [], instant("09:00:00","2026-11-20"))
        self.assertEqual(self.read("sector-intraday/"+AI)["trade_date"],"2026-11-20")
        self.assertEqual(self.read("sector-intraday/"+AI)["points"],[])
        self.assertTrue(all(x["stale"] and x["quote_date"]==DAY for x in self.read("sector-quotes")["sectors"]))

    def test_split_sections_share_existing_slots_and_manual_bootstrap_never_writes_core(self):
        day_now=instant("10:04:10")
        source=self.base/"view.json"
        relay.publish(source, {"market":[{"id":"sh000001"}],"sectors":[]})
        (self.base/"sxdata-token").write_text("fixture")
        def snapshot(_token, api, symbols, _gap, **_kwargs):
            return [quote(ident,"10:04:03",88) for ident in symbols.values()]
        with ExitStack() as ctx:
            ctx.enter_context(patch.object(sys,"path",[str(DEPLOY),*sys.path]))
            ctx.enter_context(patch("market_daily.read_daily",return_value={}))
            ctx.enter_context(patch("market_daily.merge_daily"))
            ctx.enter_context(patch.dict("os.environ", {"CREDENTIALS_DIRECTORY":str(self.base),
                "RADAR_EXPECTED_STOCK_KEY_SHA256":sha256(b"fixture").hexdigest()}))
            ctx.enter_context(patch.multiple(relay, STATE=self.base,SOURCE=source))
            ctx.enter_context(patch.object(relay,"shanghai_now",return_value=day_now))
            ctx.enter_context(patch.object(relay,"restore_gate_cooldowns"))
            ctx.enter_context(patch.object(relay.QuoteSchedule,"admit",side_effect=["rt_idx_k","rt_idx_k"]))
            send=ctx.enter_context(patch.object(relay,"collect",side_effect=snapshot))
            ctx.enter_context(patch.object(relay.time,"sleep",side_effect=[None,StopIteration]))
            with self.assertRaises(StopIteration):
                relay.start(SimpleNamespace(diagnostic=False,close_snapshot=False))
            self.assertEqual(send.call_count,2)
            self.assertNotIn(AI,send.call_args_list[0].args[2])
            self.assertIn(AI,send.call_args_list[1].args[2])
            self.assertEqual([x["id"] for x in self.read("live-quotes")["market"]],["sh000001"])
            self.assertNotIn(AI,self.read("today-intraday")["series"])
            self.assertIn(AI,{q["id"] for q in self.read("sector-quotes")["sectors"]})
            files=[self.output/(n+".json") for n in ("live-quotes","today-intraday","stock-quotes")]
            before=[p.stat().st_mtime_ns for p in files]
            ctx.enter_context(patch.object(relay,"market_session",return_value={"phase":"midday_break"}))
            relay.start(SimpleNamespace(diagnostic=False,close_snapshot=False,catalog_snapshot=True))
            self.assertEqual(send.call_count,3)
            self.assertEqual(before,[p.stat().st_mtime_ns for p in files])
