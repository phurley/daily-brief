"""Offline checks for crawl frequency scheduling under a once-a-day run."""
import unittest
from datetime import datetime, timedelta, timezone

import crawl_sources as cs

NOW = datetime(2026, 10, 24, 1, 30, tzinfo=timezone.utc)


def source(frequency):
    return cs.Source(name=frequency, url="https://example.test", frequency=frequency)


class ScheduleTests(unittest.TestCase):
    def due(self, frequency, last_ok):
        jobs, _ = cs.select_jobs([source(frequency)], force=False,
                                 successes={frequency: last_ok}, now=NOW, limit=None)
        return bool(jobs)

    def test_daily_source_finished_after_yesterdays_start_is_still_due(self):
        # Yesterday's 01:30 run finished this source at 01:52.
        self.assertTrue(self.due("daily", NOW - timedelta(hours=23, minutes=38)))

    def test_daily_source_is_not_recrawled_the_same_day(self):
        self.assertFalse(self.due("daily", NOW - timedelta(hours=12)))

    def test_three_day_source_runs_every_third_daily_run(self):
        finished = NOW - timedelta(minutes=22)
        runs = [self.due("3days", finished - timedelta(days=n)) for n in range(1, 4)]
        self.assertEqual(runs, [False, False, True])

    def test_slack_is_capped_at_two_hours(self):
        self.assertFalse(self.due("weekly", NOW - timedelta(days=7, hours=-3)))
        self.assertTrue(self.due("weekly", NOW - timedelta(days=7, hours=-2)))


if __name__ == "__main__":
    unittest.main()
