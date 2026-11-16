import unittest
from datetime import date, datetime, timezone

from pipeline.calendar import (
    market_session, is_trading_day, next_trading_day, previous_trading_day,
)


class CalendarTests(unittest.TestCase):
    def session(self, value):
        return market_session(datetime.fromisoformat(value))

    def test_auction_boundary_does_not_predict_known_open(self):
        early = self.session("2026-11-16T09:24:59+08:00")
        ended = self.session("2026-11-16T09:25:00+08:00")
        self.assertEqual(early["target_kind"], "next_open")
        self.assertEqual(early["target_date"], "2026-11-16")
        self.assertEqual(ended["phase"], "auction_complete")
        self.assertEqual(ended["target_kind"], "today_close")

    def test_lunch_still_targets_today_close(self):
        for hour in ("11:30:00", "12:45:00"):
            result = self.session("2026-11-16T" + hour + "+08:00")
            self.assertEqual(result["phase"], "midday_break")
            self.assertEqual(result["target_date"], "2026-11-16")
            self.assertEqual(result["target_kind"], "today_close")

    def test_friday_close_and_weekend_target_monday(self):
        for value in ("2026-11-13T15:00:00", "2026-11-14T12:00:00"):
            self.assertEqual(self.session(value)["target_date"], "2026-11-16")

    def test_holiday_and_makeup_working_weekend_stay_closed(self):
        self.assertEqual(self.session("2026-10-01T12:00:00")["target_date"], "2026-10-08")
        self.assertFalse(is_trading_day(date(2026, 10, 10)))
        self.assertEqual(previous_trading_day("2026-10-08"), date(2026, 9, 30))
        self.assertEqual(self.session("2026-02-16T12:00:00")["target_date"], "2026-02-24")

    def test_unknown_year_and_next_year_fail_closed(self):
        self.assertIsNone(is_trading_day("2023-12-29"))
        result = self.session("2027-01-04T10:00:00")
        self.assertEqual(result["phase"], "unknown")
        self.assertIsNone(result["target_date"])
        self.assertIsNone(self.session("2026-12-31T16:00:00")["target_date"])

    def test_2024_published_holidays_and_reopening(self):
        cases = (
            ("2024-01-01", "2024-01-02"),
            ("2024-02-09", "2024-02-19"),
            ("2024-04-04", "2024-04-08"),
            ("2024-05-01", "2024-05-06"),
            ("2024-06-10", "2024-06-11"),
            ("2024-09-16", "2024-09-18"),
            ("2024-10-07", "2024-10-08"),
        )
        for closed, reopened in cases:
            with self.subTest(closed=closed):
                self.assertFalse(is_trading_day(closed))
                self.assertTrue(is_trading_day(reopened))
                self.assertEqual(next_trading_day(closed), date.fromisoformat(reopened))
        self.assertEqual(previous_trading_day("2024-02-19"), date(2024, 2, 8))

    def test_2025_published_holidays_and_reopening(self):
        cases = (
            ("2025-01-01", "2025-01-02"),
            ("2025-01-28", "2025-02-05"),
            ("2025-04-04", "2025-04-07"),
            ("2025-05-01", "2025-05-06"),
            ("2025-06-02", "2025-06-03"),
            ("2025-10-08", "2025-10-09"),
        )
        for closed, reopened in cases:
            with self.subTest(closed=closed):
                self.assertFalse(is_trading_day(closed))
                self.assertTrue(is_trading_day(reopened))
                self.assertEqual(next_trading_day(closed), date.fromisoformat(reopened))
        self.assertEqual(previous_trading_day("2025-02-05"), date(2025, 1, 27))

    def test_historical_makeup_weekends_are_not_trading_days(self):
        for day in ("2024-02-18", "2024-04-07", "2024-05-11", "2024-09-14",
                    "2025-01-26", "2025-02-08", "2025-04-27", "2025-10-11"):
            with self.subTest(day=day):
                self.assertFalse(is_trading_day(day))

    def test_adjacent_trading_days_cross_known_year_boundaries(self):
        self.assertEqual(next_trading_day("2024-12-31"), date(2025, 1, 2))
        self.assertEqual(previous_trading_day("2025-01-02"), date(2024, 12, 31))
        self.assertEqual(next_trading_day("2025-12-31"), date(2026, 1, 5))
        self.assertEqual(previous_trading_day("2026-01-05"), date(2025, 12, 31))
        self.assertIsNone(previous_trading_day("2024-01-02"))
        self.assertIsNone(next_trading_day("2026-12-31"))

    def test_timezone_is_shanghai(self):
        result = market_session(datetime(2026, 11, 16, 4, 0, tzinfo=timezone.utc))
        self.assertEqual(result["phase"], "midday_break")


if __name__ == "__main__":
    unittest.main()
