"""服务日算术与日历边界。"""

import unittest
from datetime import date

from empq.calendar import OutsideCalendar, ServiceCalendar


def make_cal() -> ServiceCalendar:
    # 2026-11-02(一) 至 11-06(五) 为服务日，11-07/08 非服务日
    return ServiceCalendar.build(
        [date(2026, 11, d) for d in range(2, 7)],
        [date(2026, 11, 7), date(2026, 11, 8)])


class CalendarTests(unittest.TestCase):
    def test_basic_arithmetic(self):
        cal = make_cal()
        self.assertTrue(cal.is_service_day(date(2026, 11, 4)))
        self.assertFalse(cal.is_service_day(date(2026, 11, 7)))
        self.assertEqual(cal.add_service_days(date(2026, 11, 2), 3), date(2026, 11, 5))
        self.assertEqual(cal.count_service_days(date(2026, 11, 2), date(2026, 11, 4)), 2)

    def test_outside_calendar_is_explicit_not_guessed(self):
        cal = make_cal()
        with self.assertRaises(OutsideCalendar):
            cal.add_service_days(date(2026, 11, 5), 2)
        with self.assertRaises(OutsideCalendar):
            cal.ordinal(date(2026, 11, 9))
        self.assertFalse(cal.is_published(date(2026, 11, 9)))

    def test_days_until_signed(self):
        cal = make_cal()
        self.assertEqual(cal.days_until(date(2026, 11, 6), date(2026, 11, 4)), 2)
        self.assertEqual(cal.days_until(date(2026, 11, 3), date(2026, 11, 6)), -3)

    def test_reject_duplicate_and_overlap(self):
        with self.assertRaises(ValueError):
            ServiceCalendar.build([date(2026, 11, 2), date(2026, 11, 2)], [])
        with self.assertRaises(ValueError):
            ServiceCalendar.build([date(2026, 11, 2)], [date(2026, 11, 2)])


if __name__ == "__main__":
    unittest.main()
