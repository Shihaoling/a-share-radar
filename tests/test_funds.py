"""Synthetic fund transport, unit, date, selection and throttling contracts."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
import json
import os
import unittest
import urllib.error
from unittest.mock import Mock, patch

from pipeline.funds import ENDPOINT, FIELDS, _Client, _Failure, enrich_funds

NOW = datetime.fromisoformat("2026-11-16T19:00:00+08:00")
SECRET = "synthetic-fund-secret-never-persist"


def seed():
    return {"sectors": [{"id": "801780.SI", "taxonomy": "SW2021", "name": "银行"},
                        {"id": "801790.SI", "taxonomy": "SW2021", "name": "非银金融"},
                        {"id": "801230.SI", "taxonomy": "SW2021", "name": "综合"},
                        {"id": "legacy", "name": "银行"}], "sources": []}


def catalog(code="512800.SH", manager="华夏基金", index="中证银行指数", name="银行ETF"):
    return {"ts_code": code, "csname": name, "cname": f"{manager}{index}交易型开放式指数证券投资基金",
            "index_code": "399986.SZ", "index_name": index, "list_date": "20200101",
            "list_status": "L", "mgr_name": manager, "etf_type": "境内"}


def quote(code="512800.SH", day="20261116", close=1.2):
    return {"ts_code": code, "trade_date": day, "close": close, "pct_chg": 1.0, "amount": 100}


def size(code="512800.SH", day="20261113", nav=1.1, close=1.11, total_size=250000):
    return {"ts_code": code, "trade_date": day, "total_share": 200000,
            "total_size": total_size, "nav": nav, "close": close}


class Response:
    status = 200
    headers = {}

    def __init__(self, value, url=ENDPOINT):
        self.body = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, count):
        return self.body[:count]

    def geturl(self):
        return self.url


@contextmanager
def provider(overrides=None):
    calls = []
    defaults = {"etf_basic": [catalog()], "fund_daily": [quote()], "etf_share_size": [size()]}

    def request(req, **kwargs):
        body = json.loads(req.data)
        calls.append(body)
        value = (overrides or {}).get(body["api_name"], defaults[body["api_name"]])
        if isinstance(value, Exception):
            raise value
        if isinstance(value, Response):
            return value
        if isinstance(value, dict):
            return Response(value)
        fields = body["fields"].split(",")
        return Response({"code": 0, "data": {"fields": fields,
            "items": [[row.get(key) for key in fields] for row in value]}})

    opener = Mock()
    opener.open.side_effect = request
    with patch.dict(os.environ, {"STOCK_API_TOKEN": SECRET}), \
         patch("pipeline.stock_api.urllib.request.build_opener", return_value=opener), \
         patch("pipeline.funds.time.sleep") as sleep:
        yield calls, opener, sleep


class FundTests(unittest.TestCase):
    def assert_safe(self, result):
        self.assertNotIn(SECRET, json.dumps(result, ensure_ascii=False, allow_nan=False))

    def test_dated_values_and_units_do_not_mix_latest_price_with_old_nav(self):
        original = seed()
        with provider() as (calls, _, sleep):
            result = enrich_funds(original, now=NOW)
        self.assertEqual(len(calls), 3)
        self.assertEqual(result["fund_state"]["requests_this_run"], 3)
        self.assertEqual(len(sleep.call_args_list), 2)
        self.assertTrue(all(2.9 < args.args[0] <= 3 for args in sleep.call_args_list))
        fund = result["sectors"][0]["funds"][0]
        self.assertEqual(fund["price"], 1.2)
        self.assertEqual(fund["price_date"], "2026-11-16")
        self.assertEqual(fund["nav_date"], "2026-11-13")
        self.assertEqual(fund["aum_cny"], 2_500_000_000)
        self.assertEqual(fund["turnover_cny"], 100000)
        self.assertIsNone(fund["premium_pct"])
        self.assertEqual(fund["match_status"], "index_name_candidate")
        self.assertIn("未核验", fund["match_basis"])
        self.assertIn("控股", fund["broker_relation"])
        self.assertNotIn("funds", original["sectors"][0])
        self.assert_safe(result)

    def test_same_day_premium_and_price_fallback(self):
        with provider({"fund_daily": []}):
            result = enrich_funds(seed(), now=NOW)
        fund = result["sectors"][0]["funds"][0]
        self.assertEqual(fund["price"], 1.11)
        self.assertEqual(fund["price_date"], fund["nav_date"])
        self.assertAlmostEqual(fund["premium_pct"], .909091)
        self.assertTrue(fund["stale"])

    def test_offline_and_missing_credentials_never_send_request(self):
        original = seed()
        with patch.dict(os.environ, {}, clear=True), patch("pipeline.stock_api.urllib.request.build_opener") as build:
            result = enrich_funds(original, {"funds": {"token": SECRET}}, NOW)
        build.assert_not_called()
        self.assertEqual(result["sources"][-1]["error_code"], "not_configured")
        with provider() as (calls, _, _):
            offline = enrich_funds(seed(), {"network_enabled": False}, NOW)
        self.assertEqual(calls, [])
        self.assertEqual(offline["sources"][-1]["error_code"], "offline")
        self.assert_safe(result)

    def test_catalog_weekly_and_values_daily_cache(self):
        with provider() as (calls, _, _):
            first = enrich_funds(seed(), now=NOW)
            second = enrich_funds(first, now=NOW + timedelta(minutes=5))
        self.assertEqual(len(calls), 3)
        self.assertEqual(second["fund_state"]["requests_this_run"], 0)
        tomorrow = NOW + timedelta(days=1)
        with provider({"fund_daily": [quote(day="20261117")],
                       "etf_share_size": [size(day="20261116")]}) as (calls, _, _):
            third = enrich_funds(second, now=tomorrow)
        self.assertEqual([x["api_name"] for x in calls], ["fund_daily", "etf_share_size"])
        self.assertEqual(third["sectors"][0]["funds"][0]["price_date"], "2026-11-17")

    def test_no_broad_cross_border_or_nonbroker_substitution(self):
        catalogs = [catalog(), catalog("510300.SH", index="沪深300指数"),
                    catalog("159888.SZ", index="恒生银行指数"),
                    catalog("159889.SZ", manager="无股权关联基金"),
                    catalog("159890.SZ", index="中证银行证券行业指数"),
                    catalog("159891.SZ", index="中证非银行金融指数")]
        with provider({"etf_basic": catalogs}):
            result = enrich_funds(seed(), now=NOW)
        self.assertEqual([x["code"] for x in result["sectors"][0]["funds"]], ["512800.SH"])
        self.assertEqual([x["code"] for x in result["sectors"][1]["funds"]], ["159891.SZ"])
        self.assertEqual(result["sectors"][2]["funds"], [])
        self.assertEqual(result["sectors"][3]["funds"], [])

    def test_verified_provider_domestic_enum_and_cross_border_exclusion(self):
        domestic = dict(catalog(), etf_type="纯境内")
        overseas = dict(catalog("159888.SZ", index="恒生银行指数"), etf_type="纯境内")
        with provider({"etf_basic": [domestic, overseas]}):
            result = enrich_funds(seed(), now=NOW)
        self.assertEqual([x["code"] for x in result["sectors"][0]["funds"]], ["512800.SH"])

    def test_max_three_prefer_different_managers_and_larger_funds(self):
        codes = ["510001.SH", "510002.SH", "510003.SH", "510004.SH", "510005.SH"]
        managers = ["华夏基金", "华夏基金", "广发基金", "易方达基金", "南方基金"]
        catalogs = [catalog(code, manager) for code, manager in zip(codes, managers)]
        values = [size(code, total_size=1000 - i * 100) for i, code in enumerate(codes)]
        with provider({"etf_basic": catalogs, "etf_share_size": values}):
            result = enrich_funds(seed(), now=NOW)
        funds = result["sectors"][0]["funds"]
        self.assertEqual(len(funds), 3)
        self.assertEqual([x["manager"] for x in funds], ["华夏基金", "广发基金", "易方达基金"])
        self.assertIn("参股", funds[2]["broker_relation"])

    def test_rate_limit_stops_and_persists_global_cooldown(self):
        error = urllib.error.HTTPError(ENDPOINT, 429, SECRET, {"Retry-After": "7200"}, None)
        with provider({"fund_daily": error}) as (calls, _, _):
            first = enrich_funds(seed(), now=NOW)
            second = enrich_funds(first, now=NOW + timedelta(minutes=5))
        self.assertEqual(len(calls), 2)
        self.assertEqual(second["fund_state"]["requests_this_run"], 0)
        self.assertEqual(second["fund_state"]["status"], "cooldown")
        self.assert_safe(first)

    def test_api_permission_failure_cools_one_endpoint_without_echoing_message(self):
        denied = {"code": -2001, "msg": "没有访问该接口的权限 " + SECRET}
        with provider({"etf_share_size": denied}) as (calls, _, _):
            first = enrich_funds(seed(), now=NOW)
            second = enrich_funds(first, now=NOW + timedelta(minutes=30))
        self.assertEqual(len(calls), 3)
        fund = second["sectors"][0]["funds"][0]
        self.assertEqual(fund["price"], 1.2)
        self.assertIsNone(fund["nav"])
        self.assertIsNone(fund["aum_cny"])
        self.assert_safe(second)

    def test_invalid_endpoint_and_redirect_cannot_receive_token(self):
        with provider() as (calls, _, _):
            result = enrich_funds(seed(), {"funds": {"endpoint": "https://example.invalid/"}}, NOW)
        self.assertEqual(calls, [])
        self.assertEqual(result["sources"][-1]["error_code"], "invalid_endpoint")
        redirected = Response({}, url="https://example.invalid/?token=" + SECRET)
        with provider({"etf_basic": redirected}) as (calls, _, _):
            result = enrich_funds(seed(), now=NOW)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["sources"][-1]["error_code"], "redirect_blocked")
        self.assert_safe(result)

    def test_client_hard_request_limit_and_minimum_interval(self):
        with provider() as (calls, _, _):
            client = _Client(SECRET, {"max_requests": 999, "min_interval_seconds": 0}, NOW)
            self.assertEqual(client.max_requests, 4)
            self.assertEqual(client.interval, 3)
            for _ in range(4):
                client.request("etf_basic", {"list_status": "L"})
            with self.assertRaisesRegex(_Failure, "budget_exhausted"):
                client.request("etf_basic", {"list_status": "L"})
        self.assertEqual(len(calls), 4)

    def test_invalid_numeric_future_and_wrong_snapshot_rows_do_not_become_prices(self):
        quotes = [quote(close=float("nan")), quote("510001.SH", day="20261117"),
                  quote("510002.SH", day="20261113")]
        with provider({"fund_daily": quotes}):
            result = enrich_funds(seed(), now=NOW)
        self.assertEqual(result["sectors"][0]["funds"][0]["price"], 1.11)
        self.assertEqual(result["sources"][-1]["error_code"], "invalid_number")
        self.assert_safe(result)

    def test_partial_daily_batch_retains_prior_fund_quote(self):
        with provider():
            first = enrich_funds(seed(), now=NOW)
        tomorrow = NOW + timedelta(days=1)
        with provider({"fund_daily": [quote("510999.SH", "20261117")],
                       "etf_share_size": []}):
            result = enrich_funds(first, now=tomorrow)
        fund = result["sectors"][0]["funds"][0]
        self.assertEqual(fund["price"], 1.2)
        self.assertEqual(fund["price_date"], "2026-11-16")
        self.assertTrue(fund["stale"])

    def test_unknown_calendar_makes_no_requests(self):
        with provider() as (calls, _, _):
            result = enrich_funds(seed(), now=NOW.replace(year=2027))
        self.assertEqual(calls, [])
        self.assertEqual(result["sources"][-1]["error_code"], "unknown_calendar")

    def test_honors_shared_primary_service_cooldown(self):
        data = seed()
        data["stock_api_state"] = {"cooldown_until": (NOW + timedelta(hours=2)).isoformat()}
        with provider() as (calls, _, _):
            result = enrich_funds(data, now=NOW)
        self.assertEqual(calls, [])
        self.assertEqual(result["fund_state"]["status"], "cooldown")


if __name__ == "__main__":
    unittest.main()
