import unittest
from datetime import date, datetime, timezone

from pipeline.calendar import market_session, is_trading_day, previous_trading_day


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
        result = self.session("2027-01-04T10:00:00")
        self.assertEqual(result["phase"], "unknown")
        self.assertIsNone(result["target_date"])
        self.assertIsNone(self.session("2026-12-31T16:00:00")["target_date"])

    def test_timezone_is_shanghai(self):
        result = market_session(datetime(2026, 11, 16, 4, 0, tzinfo=timezone.utc))
        self.assertEqual(result["phase"], "midday_break")


if __name__ == "__main__":
    unittest.main()
