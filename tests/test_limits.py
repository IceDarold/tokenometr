"""The limit math both the Mac app and the team site use."""
import os
import sys
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tokenometr_limits as limits  # noqa: E402

MOSCOW = ZoneInfo("Europe/Moscow")
NEW_YORK = ZoneInfo("America/New_York")
AUCKLAND = ZoneInfo("Pacific/Auckland")


@contextmanager
def machine_zone(name):
    """The machine's own zone for a moment, which what is told no zone falls back on."""
    with mock.patch.dict(os.environ, {"TZ": name}):
        time.tzset()
        try:
            yield
        finally:
            pass
    time.tzset()


def moment(year, month, day, hour=0, minute=0, tz=timezone.utc):
    return datetime(year, month, day, hour, minute, tzinfo=tz)


class WeighingTests(unittest.TestCase):
    def test_a_token_weighs_by_its_kind(self):
        # input, cache write, cache read, output: 1 · 2 · 0.1 · 5
        self.assertAlmostEqual(limits.weigh((10, 100, 1000, 20)), 10 + 200 + 100 + 100)

    def test_nothing_weighs_nothing(self):
        self.assertEqual(limits.weigh((0, 0, 0, 0)), 0)


class WeekTests(unittest.TestCase):
    def test_a_configured_reset_is_read_as_a_weekday_and_a_time(self):
        self.assertEqual(limits.parse_reset("tue 18:00"), (1, 18, 0))
        self.assertEqual(limits.parse_reset("Sunday 9:30"), (6, 9, 30))

    def test_what_cannot_be_read_is_nothing(self):
        for text in (None, "", "tue", "someday 9:00", "tue 9"):
            self.assertIsNone(limits.parse_reset(text), text)

    def test_the_week_runs_from_the_last_reset_to_the_next_in_the_zone_given(self):
        # Wednesday 2026-10-07 12:00 in Moscow: the week began on Sunday the 4th, 9:00 Moscow.
        now = moment(2026, 10, 7, 12, tz=MOSCOW)

        start, end = limits.week_bounds(now, limits.WEEK_RESET, MOSCOW)

        self.assertEqual(start, moment(2026, 10, 4, 9, tz=MOSCOW).timestamp())
        self.assertEqual(end, moment(2026, 10, 11, 9, tz=MOSCOW).timestamp())

    def test_the_zone_decides_when_the_week_turns(self):
        # Sunday 2026-10-04 08:00 UTC is 11:00 in Moscow (after its 9:00 reset) and 04:00 in
        # New York (before it): the same moment is in different weeks of the two.
        now = moment(2026, 10, 4, 8)

        in_moscow = limits.week_bounds(now, limits.WEEK_RESET, MOSCOW)
        in_new_york = limits.week_bounds(now, limits.WEEK_RESET, NEW_YORK)

        self.assertEqual(in_moscow[0], moment(2026, 10, 4, 9, tz=MOSCOW).timestamp())
        self.assertEqual(in_new_york[0], moment(2026, 9, 27, 9, tz=NEW_YORK).timestamp())

    def test_a_week_across_a_clock_change_keeps_the_local_hour(self):
        # New York put its clocks forward on Sunday 2026-03-08: the week of that day's reset
        # is only 167 hours long, and still ends at 9:00 local time.
        now = moment(2026, 3, 10, 12, tz=NEW_YORK)

        start, end = limits.week_bounds(now, limits.WEEK_RESET, NEW_YORK)

        self.assertEqual(start, moment(2026, 3, 8, 9, tz=NEW_YORK).timestamp())
        self.assertEqual(end, moment(2026, 3, 15, 9, tz=NEW_YORK).timestamp())

    def test_the_zone_of_the_machine_is_the_default(self):
        now = datetime.now(timezone.utc)

        self.assertEqual(limits.week_bounds(now), limits.week_bounds(now, limits.WEEK_RESET, None))

    def test_the_zone_of_the_machine_does_not_decide_when_the_week_turns_in_another(self):
        # Sunday 9:30 in Auckland, after the 9:00 reset there; on a machine in Honolulu it is still Saturday.
        now = moment(2026, 10, 4, 9, 30, tz=AUCKLAND)

        with machine_zone("Pacific/Honolulu"):
            start, end = limits.week_bounds(now, limits.WEEK_RESET, AUCKLAND)

        self.assertEqual(start, moment(2026, 10, 4, 9, tz=AUCKLAND).timestamp())
        self.assertEqual(end, moment(2026, 10, 11, 9, tz=AUCKLAND).timestamp())

    def test_without_a_zone_the_machine_says_when_the_week_turns(self):
        now = moment(2026, 10, 4, 9, 30, tz=AUCKLAND)

        with machine_zone("Pacific/Honolulu"):
            start, _ = limits.week_bounds(now)  # Saturday there: the week of the 27th

        self.assertEqual(start, moment(2026, 9, 27, 9, tz=ZoneInfo("Pacific/Honolulu")).timestamp())

    def test_a_reset_the_cli_reported_rolls_to_the_week_around_a_moment(self):
        resets_at = moment(2026, 10, 13, 18, tz=MOSCOW).timestamp()  # a Tuesday
        week = limits.WEEK_SECONDS

        later = limits.week_around(resets_at, resets_at + 3 * week + 5)
        earlier = limits.week_around(resets_at, resets_at - 2 * week - 5)
        same = limits.week_around(resets_at, resets_at - 100)

        self.assertEqual(later, (resets_at + 3 * week, resets_at + 4 * week))
        self.assertEqual(earlier, (resets_at - 3 * week, resets_at - 2 * week))
        self.assertEqual(same, (resets_at - week, resets_at))

    def test_the_moment_of_the_reset_begins_the_next_week(self):
        resets_at = moment(2026, 10, 13, 18, tz=MOSCOW).timestamp()

        self.assertEqual(limits.week_around(resets_at, resets_at)[0], resets_at)

    def test_the_periods_start_in_the_zone_given(self):
        now = moment(2026, 10, 7, 1, 30, tz=MOSCOW)  # still the 6th in New York and UTC

        with machine_zone("Pacific/Honolulu"):
            moscow = limits.period_starts(now, 123.0, 5.0, MOSCOW)
            new_york = limits.period_starts(now, 123.0, 5.0, NEW_YORK)

        self.assertEqual(moscow["today"], moment(2026, 10, 7, tz=MOSCOW).timestamp())
        self.assertEqual(new_york["today"], moment(2026, 10, 6, tz=NEW_YORK).timestamp())
        self.assertEqual(moscow["5h"], now.timestamp() - 5 * 3600)
        self.assertEqual((moscow["week"], moscow["all"]), (5.0, 123.0))


def reading(at, week_percent, org="acct", **more):
    return dict({"t": at * 1000, "org": org, "u": {"sd": week_percent}}, **more)


class BankTests(unittest.TestCase):
    def test_spending_counts_what_was_spent_after_a_moment_and_up_to_another(self):
        spent = limits.spending([(10, 1.0), (20, 2.0), (30, 4.0)])

        self.assertEqual(spent(10, 30), 6.0)  # after 10, up to and including 30
        self.assertEqual(spent(0, 20), 3.0)
        self.assertEqual(spent(30, 40), 0.0)

    def test_the_size_of_a_percent_follows_how_far_the_readings_moved(self):
        # 20 % of the limit went while 2 000 weighted tokens were spent: 100 per percent.
        readings = [reading(100, 10), reading(200, 30)]
        spent = limits.spending([(150, 2000.0)])

        per_percent = limits.calibrate(readings, lambda org: spent, lambda org, moment: (0, 10 ** 9))

        self.assertAlmostEqual(per_percent, 100.0)

    def test_a_small_move_says_too_little(self):
        readings = [reading(100, 10), reading(200, 15)]
        spent = limits.spending([(150, 500.0)])

        self.assertIsNone(limits.calibrate(readings, lambda org: spent, lambda org, moment: (0, 10 ** 9)))

    def test_an_interval_across_the_reset_is_left_out(self):
        readings = [reading(100, 10), reading(200, 40)]
        spent = limits.spending([(150, 3000.0)])
        week_of = lambda org, moment: (150, 10 ** 9)  # noqa: E731 - the week began between them

        self.assertIsNone(limits.calibrate(readings, lambda org: spent, week_of))

    def test_an_interval_in_which_the_account_was_not_used_here_is_left_out(self):
        readings = [reading(100, 10), reading(200, 40)]

        self.assertIsNone(limits.calibrate(readings, lambda org: limits.spending([]), lambda o, m: (0, 10 ** 9)))

    def test_what_is_left_is_the_latest_reading_less_what_was_spent_since(self):
        spent = limits.spending([(150, 1000.0), (250, 500.0)])

        bank = limits.account_bank([reading(200, 40)], 100.0, spent, 300, 0, 10 ** 6)

        # 40 % at 200, and 500 tokens, 5 %, spent after it.
        self.assertAlmostEqual(bank["usedPercent"], 45.0)
        self.assertAlmostEqual(bank["remaining"], 55.0 * 100.0)
        self.assertAlmostEqual(bank["total"], 10000.0)
        self.assertEqual((bank["readingAt"], bank["readingPercent"]), (200.0, 40))

    def test_the_part_of_a_reading_the_transcripts_do_not_explain_is_off_the_mac(self):
        spent = limits.spending([(150, 1000.0)])  # 10 % of the 40 % that the reading shows

        bank = limits.account_bank([reading(200, 40)], 100.0, spent, 300, 0, 10 ** 6)

        self.assertAlmostEqual(bank["offMacPercent"], 30.0)
        self.assertAlmostEqual(bank["offMacTokens"], 3000.0)

    def test_without_a_reading_this_week_what_was_spent_is_all_there_is(self):
        spent = limits.spending([(150, 1000.0)])

        bank = limits.account_bank([reading(50, 70)], 100.0, spent, 300, 100, 10 ** 6)

        self.assertAlmostEqual(bank["usedPercent"], 10.0)
        self.assertIsNone(bank["readingAt"])

    def test_without_the_size_of_a_percent_there_is_no_bank(self):
        self.assertIsNone(limits.account_bank([reading(200, 40)], None, limits.spending([]), 300, 0, 10 ** 6))

    def test_it_never_says_more_than_all_of_it_is_gone(self):
        bank = limits.account_bank([reading(200, 99)], 100.0, limits.spending([(250, 9000.0)]), 300, 0, 10 ** 6)

        self.assertEqual(bank["usedPercent"], 100.0)
        self.assertEqual(bank["remaining"], 0.0)


if __name__ == "__main__":
    unittest.main()
