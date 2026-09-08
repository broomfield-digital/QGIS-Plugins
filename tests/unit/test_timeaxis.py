"""Guards :mod:`nasa_power.core.timeaxis`.

Two things are being pinned here, and the second is the expensive one:

* every key shape decodes to the right **half-open** UTC interval, tzinfo
  included -- closed intervals make two adjacent days both match an instant on
  their shared boundary and the QGIS temporal controller flickers between them;
* ``YYYY13`` and ``ANN`` are **annual means**, not timesteps, and come back as
  ``None`` so nothing downstream plots them. ``point_monthly_yyyy13.json`` is a
  real 2020-2021 monthly response: 26 keys for 24 months.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from nasa_power.core.timeaxis import (
    CLIMATOLOGY_MONTHS,
    TEMPORAL_LEVELS,
    TimeKeyError,
    decode_monthly_stamp,
    decode_time_key,
    decode_time_keys,
    dropped_keys,
    month_end,
)
from tests.unit.support import load_json

UTC = timezone.utc


def utc(year, month=1, day=1, hour=0):
    return datetime(year, month, day, hour, tzinfo=UTC)


class TestKeyShapes(unittest.TestCase):
    """One key of each shape, decoded to the interval it names."""

    def test_hourly_key_is_one_hour(self):
        start, end = decode_time_key("2024060117", "hourly")
        self.assertEqual(start, utc(2024, 6, 1, 17))
        self.assertEqual(end, utc(2024, 6, 1, 18))
        self.assertEqual(end - start, timedelta(hours=1))

    def test_daily_key_is_one_day(self):
        start, end = decode_time_key("20240201", "daily")
        self.assertEqual(start, utc(2024, 2, 1))
        self.assertEqual(end, utc(2024, 2, 2))
        self.assertEqual(end - start, timedelta(days=1))

    def test_monthly_key_runs_to_the_first_of_the_next_month(self):
        start, end = decode_time_key("202001", "monthly")
        self.assertEqual(start, utc(2020, 1, 1))
        self.assertEqual(end, utc(2020, 2, 1))

    def test_a_month_is_not_assumed_to_be_thirty_days(self):
        # February 2020 is 29 days; a fixed timedelta(days=30) would overrun
        # into March and overlap the next interval.
        start, end = decode_time_key("202002", "monthly")
        self.assertEqual(end - start, timedelta(days=29))
        self.assertEqual(end, utc(2020, 3, 1))

    def test_december_rolls_the_year(self):
        start, end = decode_time_key("202012", "monthly")
        self.assertEqual(start, utc(2020, 12, 1))
        self.assertEqual(end, utc(2021, 1, 1))

    def test_climatology_uses_the_nominal_year(self):
        start, end = decode_time_key("JAN", "climatology")
        self.assertEqual(start, utc(2001, 1, 1))
        self.assertEqual(end, utc(2001, 2, 1))

    def test_climatology_december_rolls_the_nominal_year(self):
        start, end = decode_time_key("DEC", "climatology")
        self.assertEqual(start, utc(2001, 12, 1))
        self.assertEqual(end, utc(2002, 1, 1))

    def test_the_nominal_climatology_year_is_a_non_leap_year(self):
        # February must be unambiguous, so the default year must not be a leap.
        start, end = decode_time_key("FEB", "climatology")
        self.assertEqual(end - start, timedelta(days=28))

    def test_the_nominal_climatology_year_is_overridable(self):
        start, end = decode_time_key("MAR", "climatology", climatology_year=1999)
        self.assertEqual(start, utc(1999, 3, 1))
        self.assertEqual(end, utc(1999, 4, 1))

    def test_climatology_keys_are_case_insensitive(self):
        self.assertEqual(
            decode_time_key("jan", "climatology"),
            decode_time_key("JAN", "climatology"),
        )

    def test_every_climatology_month_decodes_in_calendar_order(self):
        starts = [decode_time_key(m, "climatology")[0] for m in CLIMATOLOGY_MONTHS]
        self.assertEqual(len(starts), 12)
        self.assertEqual(starts, sorted(starts))
        self.assertEqual([s.month for s in starts], list(range(1, 13)))

    def test_surrounding_whitespace_is_tolerated(self):
        self.assertEqual(decode_time_key(" 20240201 ", "daily"), decode_time_key("20240201", "daily"))


class TestEverythingIsUtcAware(unittest.TestCase):
    """POWER's own default is Local Solar Time; naive datetimes hide that."""

    def test_every_temporal_returns_tz_aware_utc(self):
        cases = (
            ("hourly", "2024060117"),
            ("daily", "20240201"),
            ("monthly", "202001"),
            ("climatology", "JAN"),
        )
        for temporal, key in cases:
            with self.subTest(temporal=temporal):
                start, end = decode_time_key(key, temporal)
                for moment in (start, end):
                    self.assertIsNotNone(moment.tzinfo)
                    self.assertEqual(moment.utcoffset(), timedelta(0))
                    self.assertEqual(moment.tzinfo, UTC)


class TestAnnualMeansAreDropped(unittest.TestCase):
    """``YYYY13`` and ``ANN`` are annual means sharing the monthly dictionary."""

    def test_yyyy13_is_not_a_thirteenth_month(self):
        self.assertIsNone(decode_time_key("202013", "monthly"))
        self.assertIsNone(decode_time_key("202113", "monthly"))

    def test_ann_is_not_a_thirteenth_climatological_period(self):
        self.assertIsNone(decode_time_key("ANN", "climatology"))
        self.assertIsNone(decode_time_key("ann", "climatology"))

    def test_dropping_is_reported_not_silent(self):
        keys = ["202001", "202013", "202101", "202113"]
        self.assertEqual(dropped_keys(keys, "monthly"), ["202013", "202113"])
        self.assertEqual(
            dropped_keys(list(CLIMATOLOGY_MONTHS) + ["ANN"], "climatology"), ["ANN"]
        )

    def test_nothing_is_dropped_from_a_clean_daily_series(self):
        self.assertEqual(dropped_keys(["20240201", "20240202"], "daily"), [])


class TestAgainstTheMonthlyFixture(unittest.TestCase):
    """point_monthly_yyyy13.json -- a real 2020-2021 monthly point response."""

    def setUp(self):
        payload = load_json("point_monthly_yyyy13.json")
        self.series = payload["properties"]["parameter"]["T2M"]
        self.keys = list(self.series)

    def test_the_response_carries_twenty_six_keys_for_twenty_four_months(self):
        self.assertEqual(len(self.keys), 26)

    def test_decoding_yields_exactly_twenty_four_timesteps(self):
        decoded = decode_time_keys(self.keys, "monthly")
        self.assertEqual(len(decoded), 24)

    def test_the_two_dropped_keys_are_the_annual_means(self):
        self.assertEqual(dropped_keys(self.keys, "monthly"), ["202013", "202113"])

    def test_no_decoded_interval_starts_in_a_thirteenth_month(self):
        months = {start.month for _key, start, _end in decode_time_keys(self.keys, "monthly")}
        self.assertEqual(max(months), 12)
        self.assertEqual(months, set(range(1, 13)))

    def test_the_series_spans_january_2020_to_january_2022(self):
        decoded = decode_time_keys(self.keys, "monthly")
        self.assertEqual(decoded[0][1], utc(2020, 1, 1))
        self.assertEqual(decoded[-1][2], utc(2022, 1, 1))

    def test_the_original_key_is_kept_so_values_stay_addressable(self):
        for key, start, _end in decode_time_keys(self.keys, "monthly"):
            with self.subTest(key=key):
                self.assertIn(key, self.series)
                self.assertEqual(key, f"{start.year:04d}{start.month:02d}")

    def test_the_annual_mean_value_is_not_one_of_the_kept_values(self):
        # 202013 holds 10.81 C, a plausible-looking Boulder monthly mean; the
        # point of dropping it is that it is not September's, or anyone's.
        kept = {key for key, _s, _e in decode_time_keys(self.keys, "monthly")}
        self.assertNotIn("202013", kept)
        self.assertNotIn("202113", kept)
        self.assertEqual(self.series["202013"], 10.81)


class TestIntervalsAreHalfOpenAndContiguous(unittest.TestCase):
    """Closed intervals make the QGIS animation flicker on every boundary."""

    def _assert_contiguous(self, decoded):
        self.assertGreater(len(decoded), 1)
        for (_k0, _s0, end), (_k1, start, _e1) in zip(decoded, decoded[1:]):
            self.assertEqual(end, start)

    def _assert_no_instant_matches_twice(self, decoded):
        boundaries = [start for _k, start, _e in decoded] + [decoded[-1][2]]
        for instant in boundaries:
            matches = [k for k, s, e in decoded if s <= instant < e]
            self.assertLessEqual(len(matches), 1, f"{instant} matched {matches}")

    def test_monthly_fixture_intervals_are_contiguous(self):
        payload = load_json("point_monthly_yyyy13.json")
        decoded = decode_time_keys(list(payload["properties"]["parameter"]["T2M"]), "monthly")
        self._assert_contiguous(decoded)
        self._assert_no_instant_matches_twice(decoded)

    def test_daily_intervals_are_contiguous_across_a_leap_day(self):
        decoded = decode_time_keys(["20240228", "20240229", "20240301"], "daily")
        self.assertEqual([k for k, _s, _e in decoded], ["20240228", "20240229", "20240301"])
        self._assert_contiguous(decoded)
        self._assert_no_instant_matches_twice(decoded)

    def test_hourly_intervals_are_contiguous_across_a_day_boundary(self):
        decoded = decode_time_keys(["2024060122", "2024060123", "2024060200"], "hourly")
        self._assert_contiguous(decoded)
        self._assert_no_instant_matches_twice(decoded)

    def test_climatology_intervals_are_contiguous(self):
        decoded = decode_time_keys(list(CLIMATOLOGY_MONTHS) + ["ANN"], "climatology")
        self.assertEqual(len(decoded), 12)
        self._assert_contiguous(decoded)
        self._assert_no_instant_matches_twice(decoded)

    def test_the_shared_boundary_belongs_to_the_later_interval_only(self):
        # Midnight on the 2nd is the *start* of the 2nd, not the end of the 1st.
        first_start, first_end = decode_time_key("20240201", "daily")
        second_start, _second_end = decode_time_key("20240202", "daily")
        midnight = utc(2024, 2, 2)
        self.assertEqual(first_end, second_start)
        self.assertFalse(first_start <= midnight < first_end)
        self.assertTrue(second_start <= midnight)


class TestSorting(unittest.TestCase):
    """JSON object order is not guaranteed; the time axis must be."""

    def test_shuffled_keys_come_back_in_time_order(self):
        shuffled = ["202012", "202001", "202113", "202101", "202013", "202006"]
        decoded = decode_time_keys(shuffled, "monthly")
        self.assertEqual([k for k, _s, _e in decoded], ["202001", "202006", "202012", "202101"])
        starts = [s for _k, s, _e in decoded]
        self.assertEqual(starts, sorted(starts))

    def test_shuffled_hourly_keys_sort_by_hour_not_by_string_luck(self):
        decoded = decode_time_keys(["2024060109", "2024060100", "2024060123"], "hourly")
        self.assertEqual([s.hour for _k, s, _e in decoded], [0, 9, 23])

    def test_shuffled_climatology_keys_sort_by_calendar_month(self):
        decoded = decode_time_keys(["DEC", "FEB", "ANN", "JAN"], "climatology")
        self.assertEqual([k for k, _s, _e in decoded], ["JAN", "FEB", "DEC"])


class TestMalformedKeysRaise(unittest.TestCase):
    """A key that does not match its shape means the response schema changed."""

    def test_wrong_shape_for_the_temporal_level(self):
        cases = (
            ("hourly", "20240601"),      # a daily key at the hourly endpoint
            ("daily", "2024060117"),     # an hourly key at the daily endpoint
            ("daily", "202402"),
            ("monthly", "20240201"),
            ("climatology", "202001"),
        )
        for temporal, key in cases:
            with self.subTest(temporal=temporal, key=key):
                with self.assertRaises(TimeKeyError):
                    decode_time_key(key, temporal)

    def test_non_numeric_and_empty_keys(self):
        for key in ("", "   ", "notakey", "2024-02-01", "20240201T00", "2024 02 01"):
            with self.subTest(key=key):
                with self.assertRaises(TimeKeyError):
                    decode_time_key(key, "daily")

    def test_month_zero_and_month_fourteen_are_not_silently_accepted(self):
        # 13 is the documented annual mean; 00 and 14 are neither months nor
        # annual means, so they are a parse failure, not a drop.
        for key in ("202000", "202014", "202099"):
            with self.subTest(key=key):
                with self.assertRaises(TimeKeyError):
                    decode_time_key(key, "monthly")

    def test_an_unknown_temporal_level_raises(self):
        with self.assertRaises(TimeKeyError):
            decode_time_key("20240201", "weekly")
        with self.assertRaises(TimeKeyError):
            decode_time_key("20240201", "Daily")  # levels are lowercase

    def test_the_known_levels_are_the_four_power_serves(self):
        self.assertEqual(TEMPORAL_LEVELS, ("hourly", "daily", "monthly", "climatology"))

    def test_time_key_error_is_a_value_error(self):
        # Callers that only care an argument was wrong keep catching ValueError.
        self.assertTrue(issubclass(TimeKeyError, ValueError))

    def test_a_malformed_key_in_a_batch_is_not_swallowed(self):
        with self.assertRaises(TimeKeyError):
            decode_time_keys(["202001", "notakey"], "monthly")


class TestMonthlyStamp(unittest.TestCase):
    """The NetCDF monthly axis: raw YYYYMM ints, no ``time#units`` to decode."""

    def test_an_integer_stamp_decodes(self):
        start, end = decode_monthly_stamp(202001)
        self.assertEqual(start, utc(2020, 1, 1))
        self.assertEqual(end, utc(2020, 2, 1))

    def test_a_string_stamp_decodes_the_same_way(self):
        self.assertEqual(decode_monthly_stamp("202001"), decode_monthly_stamp(202001))

    def test_the_annual_mean_band_returns_none(self):
        # regional_monthly_t2m.nc has 26 bands: 202001..202013, 202101..202113.
        self.assertIsNone(decode_monthly_stamp(202013))
        self.assertIsNone(decode_monthly_stamp("202113"))

    def test_a_full_twenty_six_band_axis_yields_twenty_four_timesteps(self):
        axis = [202000 + m for m in range(1, 14)] + [202100 + m for m in range(1, 14)]
        self.assertEqual(len(axis), 26)
        decoded = [decode_monthly_stamp(v) for v in axis]
        self.assertEqual(sum(1 for d in decoded if d is None), 2)
        self.assertEqual(sum(1 for d in decoded if d is not None), 24)

    def test_december_rolls_the_year(self):
        self.assertEqual(decode_monthly_stamp(202012)[1], utc(2021, 1, 1))

    def test_a_stamp_that_is_not_yyyymm_raises(self):
        for stamp in (2020131, 20200, "", "2020-01", 1.5):
            with self.subTest(stamp=stamp):
                with self.assertRaises(TimeKeyError):
                    decode_monthly_stamp(stamp)

    def test_a_cf_style_day_count_is_rejected_rather_than_misread(self):
        # If a future response *does* carry a CF epoch, its values are small day
        # counts; reading 45 as a YYYYMM must fail loudly, not become year 0.
        with self.assertRaises(TimeKeyError):
            decode_monthly_stamp(45)


class TestMonthEnd(unittest.TestCase):
    """Used to render a monthly request window as explicit dates."""

    def test_month_lengths(self):
        self.assertEqual(month_end(2020, 1).day, 31)
        self.assertEqual(month_end(2020, 4).day, 30)
        self.assertEqual(month_end(2021, 2).day, 28)

    def test_february_in_a_leap_year(self):
        self.assertEqual(month_end(2020, 2).day, 29)

    def test_february_in_a_century_that_is_not_a_leap_year(self):
        self.assertEqual(month_end(1900, 2).day, 28)
        self.assertEqual(month_end(2000, 2).day, 29)


if __name__ == "__main__":
    unittest.main()
