"""Taipei calendar boundaries for the monthly report (fixed UTC+8, half-open)."""
from datetime import datetime, timezone
import unittest

from discord_bot.features.monthly_report.periods import (
    due_month, month_bounds, month_of, next_boundary, parse_month, shift_month, taipei_date)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc).timestamp()


class MonthBoundaryTests(unittest.TestCase):
    def test_taipei_month_is_left_closed_right_open_in_utc_epoch(self):
        lo, hi = month_bounds("2026-10")
        self.assertEqual(lo, utc(2026, 9, 30, 16))
        self.assertEqual(hi, utc(2026, 10, 31, 16))
        self.assertEqual(month_of(lo), "2026-10")
        self.assertEqual(month_of(hi - 0.001), "2026-10")
        self.assertEqual(month_of(hi), "2026-11")

    def test_cross_year_and_leap_february(self):
        self.assertEqual(shift_month("2026-12", 1), "2027-01")
        self.assertEqual(shift_month("2027-01", -1), "2026-12")
        lo, hi = month_bounds("2026-12")
        self.assertEqual(hi, utc(2026, 12, 31, 16))
        lo, hi = month_bounds("2028-02")
        self.assertEqual((hi - lo) / 86400, 29)
        lo, hi = month_bounds("2027-02")
        self.assertEqual((hi - lo) / 86400, 28)

    def test_due_month_and_next_boundary_at_both_edges(self):
        boundary = utc(2026, 10, 31, 16)  # 2026-11-01 00:00 Taipei
        self.assertEqual(due_month(boundary - 1), "2026-09")
        self.assertEqual(due_month(boundary), "2026-10")
        self.assertEqual(next_boundary(boundary - 1), boundary)
        self.assertEqual(next_boundary(boundary), utc(2026, 11, 30, 16))
        self.assertEqual(due_month(utc(2026, 12, 31, 16)), "2026-12")

    def test_taipei_date_and_month_validation(self):
        self.assertEqual(str(taipei_date(utc(2026, 10, 29, 16))), "2026-10-30")
        for bad in ("2026-13", "2026-1", "202610", "", None, "1999-12"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_month(bad)


if __name__ == "__main__":
    unittest.main()
