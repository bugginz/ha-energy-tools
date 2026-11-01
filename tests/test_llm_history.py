"""Tests for the llm_history custom integration's pure shaping helpers.

Stdlib unittest only (no pytest, no Home Assistant), same as tests/test_foxctl.py:

    python3 -m unittest tests.test_llm_history -v
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "ha" / "custom_components" / "llm_history"))
import shaping  # noqa: E402

SYD = ZoneInfo("Australia/Sydney")
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=SYD)


class ParseWhenTest(unittest.TestCase):
    """Tool time arguments: durations ago, ISO datetimes, 'now'."""

    def test_duration_hours_is_that_long_before_now(self):
        self.assertEqual(shaping.parse_when("24h", NOW, SYD), NOW - timedelta(hours=24))

    def test_duration_units_minutes_days_weeks(self):
        self.assertEqual(shaping.parse_when("90m", NOW, SYD), NOW - timedelta(minutes=90))
        self.assertEqual(shaping.parse_when("7d", NOW, SYD), NOW - timedelta(days=7))
        self.assertEqual(shaping.parse_when("2w", NOW, SYD), NOW - timedelta(weeks=2))

    def test_duration_years_are_365_days(self):
        self.assertEqual(shaping.parse_when("1y", NOW, SYD), NOW - timedelta(days=365))

    def test_now_keyword(self):
        self.assertEqual(shaping.parse_when("now", NOW, SYD), NOW)

    def test_naive_iso_is_local_time_not_utc(self):
        got = shaping.parse_when("2026-10-31T18:00", NOW, SYD)
        self.assertEqual(got, datetime(2026, 10, 31, 18, 0, tzinfo=SYD))

    def test_iso_with_z_suffix_is_utc(self):
        got = shaping.parse_when("2026-10-31T07:00:00Z", NOW, SYD)
        self.assertEqual(got, datetime(2026, 10, 31, 7, 0, tzinfo=timezone.utc))

    def test_bare_date_is_local_midnight(self):
        got = shaping.parse_when("2026-10-01", NOW, SYD)
        self.assertEqual(got, datetime(2026, 10, 1, 0, 0, tzinfo=SYD))

    def test_garbage_raises_value_error(self):
        with self.assertRaises(ValueError):
            shaping.parse_when("yesterday-ish", NOW, SYD)


class ResolveWindowTest(unittest.TestCase):

    def test_defaults_to_default_start_until_now(self):
        start, end = shaping.resolve_window(None, None, NOW, SYD, default_start="24h")
        self.assertEqual((start, end), (NOW - timedelta(hours=24), NOW))

    def test_start_after_end_raises(self):
        with self.assertRaises(ValueError):
            shaping.resolve_window("1h", "2h", NOW, SYD, default_start="24h")


class PickBucketTest(unittest.TestCase):
    """Smallest 'nice' bucket that keeps a series within max_points."""

    def test_one_day_at_200_points_is_ten_minutes(self):
        self.assertEqual(shaping.pick_bucket(86400, 200), 600)

    def test_short_span_uses_one_minute(self):
        self.assertEqual(shaping.pick_bucket(3600, 200), 60)

    def test_huge_span_falls_back_to_whole_days(self):
        # 2000 days at 100 points -> 20-day buckets, still a whole number of days.
        got = shaping.pick_bucket(2000 * 86400, 100)
        self.assertEqual(got % 86400, 0)
        self.assertLessEqual(2000 * 86400 / got, 100)


class AlignStartTest(unittest.TestCase):
    """Buckets start on local clock boundaries, so chart rows read 12:00, 12:30."""

    def test_half_hour_bucket_floors_to_the_half_hour(self):
        ts = datetime(2026, 11, 1, 11, 49, 49, tzinfo=SYD).timestamp()
        got = shaping.align_start(ts, 1800, SYD)
        self.assertEqual(datetime.fromtimestamp(got, SYD),
                         datetime(2026, 11, 1, 11, 30, tzinfo=SYD))

    def test_day_bucket_floors_to_local_midnight_not_utc(self):
        ts = datetime(2026, 10, 24, 17, 49, 49, tzinfo=SYD).timestamp()
        got = shaping.align_start(ts, 86400, SYD)
        self.assertEqual(datetime.fromtimestamp(got, SYD),
                         datetime(2026, 10, 24, 0, 0, tzinfo=SYD))


class BucketNumericTest(unittest.TestCase):
    """Time-weighted bucketing of a held-value state series."""

    def test_value_held_from_before_the_window_fills_the_bucket(self):
        rows = shaping.bucket_numeric([(0, 5.0)], start=100, end=200, bucket=100)
        self.assertEqual(rows, [[100, 5.0, 5.0, 5.0]])

    def test_mean_is_time_weighted_not_a_plain_average(self):
        # 2.0 held for 90s then 12.0 for 10s -> (180 + 120) / 100 = 3.0,
        # where a plain average of the two samples would say 7.0.
        rows = shaping.bucket_numeric([(0, 2.0), (90, 12.0)], start=0, end=100, bucket=100)
        self.assertEqual(rows, [[0, 3.0, 2.0, 12.0]])

    def test_a_change_exactly_on_a_boundary_belongs_to_the_next_bucket(self):
        rows = shaping.bucket_numeric([(0, 1.0), (100, 9.0)], start=0, end=200, bucket=100)
        self.assertEqual(rows, [[0, 1.0, 1.0, 1.0], [100, 9.0, 9.0, 9.0]])

    def test_unavailable_time_is_excluded_and_empty_buckets_are_dropped(self):
        # None = unknown/unavailable: bucket 0 only counts the 50s of real data,
        # bucket 100 has no data at all and is omitted.
        rows = shaping.bucket_numeric([(0, None), (50, 4.0), (100, None)],
                                      start=0, end=200, bucket=100)
        self.assertEqual(rows, [[0, 4.0, 4.0, 4.0]])


class ClassifyTest(unittest.TestCase):

    def test_numbers_with_occasional_unavailable_are_numeric(self):
        self.assertEqual(shaping.classify(["1.5", "unavailable", "2", "unknown"]), "numeric")

    def test_any_real_word_makes_it_text(self):
        self.assertEqual(shaping.classify(["Auto", "Auto · no spare", "unavailable"]), "text")
        self.assertEqual(shaping.classify(["on", "off"]), "text")

    def test_all_unavailable_is_text(self):
        self.assertEqual(shaping.classify(["unavailable"]), "text")


class ToNumberTest(unittest.TestCase):

    def test_parses_numbers_and_rejects_non_numbers(self):
        self.assertEqual(shaping.to_number("0.837"), 0.837)
        self.assertIsNone(shaping.to_number("unavailable"))
        self.assertIsNone(shaping.to_number("nan"))


class TextChangesTest(unittest.TestCase):

    def test_collapses_repeats_and_keeps_the_latest_rows(self):
        pts = [(0, "off"), (10, "off"), (20, "on"), (30, "off"), (40, "on")]
        rows, truncated = shaping.text_changes(pts, limit=2)
        self.assertEqual(rows, [[30, "off"], [40, "on"]])
        self.assertTrue(truncated)

    def test_not_truncated_when_under_limit(self):
        rows, truncated = shaping.text_changes([(0, "a"), (5, "b")], limit=10)
        self.assertEqual(rows, [[0, "a"], [5, "b"]])
        self.assertFalse(truncated)


class DefaultStatTypesTest(unittest.TestCase):

    def test_counters_default_to_change(self):
        self.assertEqual(shaping.default_stat_types(has_sum=True, has_mean=False), ["change"])

    def test_measurements_default_to_mean_min_max(self):
        self.assertEqual(shaping.default_stat_types(has_sum=False, has_mean=True),
                         ["mean", "min", "max"])


class FormatRowsTest(unittest.TestCase):

    def test_timestamps_become_local_iso_and_values_are_rounded(self):
        ts = datetime(2026, 11, 1, 1, 0, tzinfo=timezone.utc).timestamp()
        got = shaping.format_rows([[ts, 3.14159, None]], SYD)
        self.assertEqual(got, [["2026-11-01T12:00:00+11:00", 3.142, None]])

    def test_statistics_rows_are_columnar_in_requested_type_order(self):
        ts = datetime(2026, 11, 1, 1, 0, tzinfo=timezone.utc).timestamp()
        rows = [{"start": ts, "end": ts + 3600, "mean": 1.23456, "max": 2.0}]
        got = shaping.statistics_table(rows, ["max", "mean", "min"], SYD)
        self.assertEqual(got, {
            "columns": ["start", "max", "mean", "min"],
            "rows": [["2026-11-01T12:00:00+11:00", 2.0, 1.235, None]],
        })


if __name__ == "__main__":
    unittest.main()
