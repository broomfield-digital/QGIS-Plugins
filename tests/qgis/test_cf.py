"""Decode a POWER NetCDF time axis, and pin the one integration bug that got through.

``gdalio.cf`` exists because there is no single rule for a POWER time axis.
Measured on the committed captures, the CF epoch changes with the parameter
*and* with the era -- ``days since 1980-12-31`` for MERRA-2 ``T2M``, ``days
since 2000-12-31`` for CERES solar in 2024 -- while the monthly endpoint
carries **no** ``time#units`` attribute at all, so its raw values simply *are*
``YYYYMM``. Band 1 of the two daily fixtures reads ``15737`` and ``8432``: the
same calendar day written against two different epochs, which is why two
responses may never be merged on raw index.

The last class is a regression test for a bug that actually shipped into
integration. ``gdal.BuildVRT`` carries per-band ``NETCDF_DIM_time`` through but
drops **dataset-level** metadata, so a VRT has no ``time#units``. Reading the
units off the VRT therefore made every daily response look like the units-less
monthly case, and the decoder then tried to read ``15737`` as ``YYYYMM``.
:func:`nasa_power.gdalio.mosaic.mosaic` takes the units from a source tile
instead, and :class:`VrtLosesTimeUnitsTests` is what keeps that from being
folklore.

This module lives in ``tests/qgis`` rather than ``tests/unit`` only because
``band_times`` and ``units_of`` take an open GDAL dataset; the decoding itself
is pure.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from osgeo import gdal

from nasa_power.core.timeaxis import TimeKeyError
from nasa_power.gdalio.cf import (
    CfTimeError,
    band_times,
    decode_axis,
    keep_bands,
    parse_units,
    units_of,
)
from tests.qgis.qgis_case import FIXTURES, QgisTestCase

gdal.UseExceptions()

UTC = timezone.utc

MET_TILE_N = FIXTURES / "regional_daily_t2m_tileN.nc"
MET_TILE_S = FIXTURES / "regional_daily_t2m_tileS.nc"
SOLAR_TILE_N = FIXTURES / "regional_solar_tileN.nc"
MONTHLY = FIXTURES / "regional_monthly_t2m.nc"

#: ``time#units`` as it appears in each fixture, read off the file.
MET_UNITS = "days since 1980-12-31"
SOLAR_2024_UNITS = "days since 2000-12-31"
#: The 1985 CERES era answers with a third epoch again (P-20).
SOLAR_1985_UNITS = "days since 1984-01-01"

#: ``NETCDF_DIM_time`` for band 1 of each daily fixture. Both are 2024-02-01.
MET_BAND_1_RAW = 15737
SOLAR_BAND_1_RAW = 8432

#: The three days every daily fixture covers.
FEB = [datetime(2024, 2, day, tzinfo=UTC) for day in (1, 2, 3, 4)]
DAY = timedelta(days=1)

#: The monthly fixture: 26 bands for 24 months, the annual means at 13 and 26.
MONTHLY_RAW = [202001 + n for n in range(13)] + [202101 + n for n in range(13)]


class ParseUnitsTests(QgisTestCase):
    """Every ``time#units`` form POWER has actually been seen to write."""

    def test_merra2_daily_epoch(self) -> None:
        # Measured on regional_daily_t2m_tileN.nc.
        step, epoch = parse_units(MET_UNITS)
        self.assertEqual(timedelta(days=1), step)
        self.assertEqual(datetime(1980, 12, 31, tzinfo=UTC), epoch)

    def test_ceres_2024_epoch_is_a_different_one(self) -> None:
        # Measured on regional_solar_tileN.nc: same request window, twenty
        # years of difference in the epoch. Nothing may hardcode either.
        step, epoch = parse_units(SOLAR_2024_UNITS)
        self.assertEqual(timedelta(days=1), step)
        self.assertEqual(datetime(2000, 12, 31, tzinfo=UTC), epoch)

    def test_ceres_1985_epoch_is_a_third_one(self) -> None:
        # P-20: the epoch moves with the era as well as with the parameter.
        _, epoch = parse_units(SOLAR_1985_UNITS)
        self.assertEqual(datetime(1984, 1, 1, tzinfo=UTC), epoch)

    def test_hourly_step(self) -> None:
        step, epoch = parse_units("hours since 1980-12-31")
        self.assertEqual(timedelta(hours=1), step)
        self.assertEqual(datetime(1980, 12, 31, tzinfo=UTC), epoch)

    def test_every_epoch_spelling_gives_the_same_instant(self) -> None:
        # POWER writes the date several ways; all of them are the same moment.
        forms = (
            "days since 2000-12-31",
            "days since 2000-12-31 00:00:00",
            "days since 2000-12-31 00:00",
            "days since 2000-12-31T00:00:00",
            "days since 2000-12-31T00:00:00Z",
            "days since 2000-12-31 00:00:00 UTC",
            "  days   since   2000-12-31  ",
            "Days since 2000-12-31",
        )
        for units in forms:
            with self.subTest(units=units):
                step, epoch = parse_units(units)
                self.assertEqual(timedelta(days=1), step)
                self.assertEqual(datetime(2000, 12, 31, tzinfo=UTC), epoch)

    def test_a_trailing_utc_designator_is_stripped(self) -> None:
        # Regression: the epoch tail used to be normalised with a blanket
        # str.replace("T", " ") before the timezone designator was removed,
        # which rewrote "UTC" as "U C" and made the UTC branch of the
        # designator pattern unreachable -- so this one spelling raised
        # CfTimeError while "...T00:00:00Z" parsed fine.
        for units in ("days since 2000-12-31 00:00:00 UTC",
                      "days since 2000-12-31T00:00:00 utc",
                      "days since 2000-12-31T00:00:00+00:00",
                      "days since 2000-12-31 00:00:00+0000"):
            with self.subTest(units=units):
                step, epoch = parse_units(units)
                self.assertEqual(timedelta(days=1), step)
                self.assertEqual(datetime(2000, 12, 31, tzinfo=UTC), epoch)

    def test_the_epoch_is_timezone_aware_utc(self) -> None:
        # A naive epoch would propagate into QDateTime and shift the animation
        # by the machine's local offset -- silently, and only off-UTC.
        _, epoch = parse_units(MET_UNITS)
        self.assertIsNotNone(epoch.tzinfo)
        self.assertEqual(timedelta(0), epoch.utcoffset())

    def test_unsupported_unit_raises(self) -> None:
        # "months since" is not decodable to a fixed timedelta at all, so it
        # has to fail loudly rather than be approximated.
        for units in ("weeks since 1980-12-31", "months since 1980-12-31",
                      "years since 1980-12-31"):
            with self.subTest(units=units):
                with self.assertRaises(CfTimeError):
                    parse_units(units)

    def test_malformed_string_raises(self) -> None:
        for units in ("", "not a units string", "days since", "1980-12-31",
                      "days since never", "since 1980-12-31"):
            with self.subTest(units=units):
                with self.assertRaises(CfTimeError):
                    parse_units(units)

    def test_cf_time_error_is_a_value_error(self) -> None:
        # Callers that only catch ValueError still catch this.
        self.assertTrue(issubclass(CfTimeError, ValueError))


class DecodeAxisTests(QgisTestCase):
    """Half-open, contiguous intervals -- P-19."""

    def test_intervals_are_contiguous_and_half_open(self) -> None:
        spans = decode_axis([15737, 15738, 15739], MET_UNITS, "daily")
        self.assertEqual(3, len(spans))
        self.assertEqual([(FEB[0], FEB[1]), (FEB[1], FEB[2]), (FEB[2], FEB[3])],
                         spans)
        for earlier, later in zip(spans, spans[1:]):
            # Each end IS the next start: closed intervals would let two
            # adjacent days both match an instant and the animation flickers.
            self.assertEqual(earlier[1], later[0])

    def test_hourly_axis_is_contiguous_too(self) -> None:
        spans = decode_axis([0, 1, 2, 3], "hours since 1980-12-31", "hourly")
        self.assertEqual(4, len(spans))
        for earlier, later in zip(spans, spans[1:]):
            self.assertEqual(earlier[1], later[0])
        for start, end in spans:
            self.assertEqual(timedelta(hours=1), end - start)

    def test_the_last_band_is_as_wide_as_its_neighbours(self) -> None:
        # The final band has no successor to bound it; reusing the previous
        # spacing keeps the last frame of an animation the same length.
        spans = decode_axis([15737, 15738, 15739], MET_UNITS, "daily")
        self.assertEqual(DAY, spans[-1][1] - spans[-1][0])

    def test_a_single_band_axis_has_a_non_zero_duration(self) -> None:
        # There is no spacing to copy, so the axis unit itself is the width.
        # A zero-width range matches no instant and the layer never draws.
        spans = decode_axis([15737], MET_UNITS, "daily")
        self.assertEqual(1, len(spans))
        self.assertEqual((FEB[0], FEB[1]), spans[0])
        self.assertEqual(DAY, spans[0][1] - spans[0][0])

        hourly = decode_axis([9], "hours since 1980-12-31", "hourly")
        self.assertEqual(timedelta(hours=1), hourly[0][1] - hourly[0][0])

    def test_the_two_families_decode_to_the_same_day_from_different_indices(self) -> None:
        # 15737 and 8432 are 7305 apart and both mean 2024-02-01. This is the
        # whole reason nothing may be merged on raw index.
        self.assertEqual(7305, MET_BAND_1_RAW - SOLAR_BAND_1_RAW)
        met = decode_axis([MET_BAND_1_RAW], MET_UNITS, "daily")
        solar = decode_axis([SOLAR_BAND_1_RAW], SOLAR_2024_UNITS, "daily")
        self.assertEqual(FEB[0], met[0][0])
        self.assertEqual(FEB[0], solar[0][0])

    def test_string_values_decode(self) -> None:
        # NETCDF_DIM_time arrives as a string from GDAL, never as an int.
        self.assertEqual(
            decode_axis([15737, 15738], MET_UNITS, "daily"),
            decode_axis(["15737", "15738"], MET_UNITS, "daily"),
        )


class MonthlyAxisTests(QgisTestCase):
    """No ``time#units`` at all, so the raw values *are* ``YYYYMM`` -- P-8/P-21."""

    def test_units_none_is_read_as_yyyymm(self) -> None:
        spans = decode_axis([202001, 202002], None, "daily")
        self.assertEqual(
            [(datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 2, 1, tzinfo=UTC)),
             (datetime(2020, 2, 1, tzinfo=UTC), datetime(2020, 3, 1, tzinfo=UTC))],
            spans,
        )

    def test_temporal_monthly_is_read_as_yyyymm(self) -> None:
        spans = decode_axis([202012], None, "monthly")
        self.assertEqual(
            (datetime(2020, 12, 1, tzinfo=UTC), datetime(2021, 1, 1, tzinfo=UTC)),
            spans[0],
        )

    def test_yyyy13_is_none_not_a_thirteenth_month(self) -> None:
        # 202013 is 2020's ANNUAL MEAN. Rendered as a month it is a spurious
        # frame every thirteenth step.
        spans = decode_axis([202012, 202013, 202101], None, "monthly")
        self.assertIsNone(spans[1])
        self.assertIsNotNone(spans[0])
        self.assertIsNotNone(spans[2])

    def test_the_axis_keeps_its_length_so_band_numbers_still_line_up(self) -> None:
        # 26 in, 26 out, two of them None: the Nones mark positions, they are
        # not removed here, or every later band number would be off by one.
        spans = decode_axis(MONTHLY_RAW, None, "monthly")
        self.assertEqual(26, len(spans))
        self.assertEqual([13, 26], [i for i, s in enumerate(spans, 1) if s is None])

    def test_december_runs_into_the_next_january(self) -> None:
        spans = decode_axis([202012], None, "monthly")
        self.assertEqual(datetime(2021, 1, 1, tzinfo=UTC), spans[0][1])

    def test_a_daily_value_under_a_missing_units_string_raises(self) -> None:
        # This is the shape of the shipped bug: 15737 is not a YYYYMM stamp,
        # so the units-less path fails loudly instead of inventing a date.
        with self.assertRaises(TimeKeyError):
            decode_axis([15737], None, "daily")


class KeepBandsTests(QgisTestCase):
    """1-based band numbers, and intervals that line up with them index for index."""

    def test_band_numbers_are_one_based(self) -> None:
        spans = decode_axis([202001, 202002], None, "monthly")
        keep, intervals = keep_bands(spans)
        self.assertEqual([1, 2], keep)
        self.assertEqual(spans, intervals)

    def test_annual_means_are_dropped_and_the_rest_still_align(self) -> None:
        spans = decode_axis(MONTHLY_RAW, None, "monthly")
        keep, intervals = keep_bands(spans)
        self.assertEqual(24, len(keep))
        self.assertEqual(24, len(intervals))
        # 13 and 26 gone, everything else in order and still 1-based.
        self.assertEqual(list(range(1, 13)) + list(range(14, 26)), keep)
        for position, band in enumerate(keep):
            # intervals[position] must be the span of GDAL band `band`.
            self.assertEqual(spans[band - 1], intervals[position])

    def test_kept_intervals_stay_contiguous_across_the_dropped_band(self) -> None:
        # December 2020 ends where January 2021 starts; the annual mean sits
        # between them in the file and must leave no gap behind.
        _, intervals = keep_bands(decode_axis(MONTHLY_RAW, None, "monthly"))
        for earlier, later in zip(intervals, intervals[1:]):
            self.assertEqual(earlier[1], later[0])
        self.assertEqual(datetime(2021, 1, 1, tzinfo=UTC), intervals[12][0])

    def test_no_nones_keeps_everything(self) -> None:
        spans = decode_axis([15737, 15738, 15739], MET_UNITS, "daily")
        keep, intervals = keep_bands(spans)
        self.assertEqual([1, 2, 3], keep)
        self.assertEqual(spans, intervals)

    def test_all_nones_keeps_nothing(self) -> None:
        keep, intervals = keep_bands([None, None])
        self.assertEqual([], keep)
        self.assertEqual([], intervals)

    def test_empty_axis(self) -> None:
        self.assertEqual(([], []), keep_bands([]))


class FixtureUnitsTests(QgisTestCase):
    """``units_of`` against the three real files, including the one with none."""

    def test_merra2_tile_carries_the_1980_epoch(self) -> None:
        dataset = gdal.Open(str(MET_TILE_N))
        self.assertEqual(MET_UNITS, units_of(dataset))
        dataset = None

    def test_both_met_tiles_share_an_epoch(self) -> None:
        # Two tiles of one parameter do share one; that is what makes a mosaic
        # of them decodable from either tile's units.
        north = gdal.Open(str(MET_TILE_N))
        south = gdal.Open(str(MET_TILE_S))
        self.assertEqual(units_of(north), units_of(south))
        north = south = None

    def test_ceres_tile_carries_the_2000_epoch(self) -> None:
        dataset = gdal.Open(str(SOLAR_TILE_N))
        self.assertEqual(SOLAR_2024_UNITS, units_of(dataset))
        dataset = None

    def test_the_monthly_file_has_no_time_units_at_all(self) -> None:
        # P-21. None is a real answer here, not a lookup failure: there is no
        # CF epoch on the monthly endpoint to decode against.
        dataset = gdal.Open(str(MONTHLY))
        self.assertIsNone(units_of(dataset))
        self.assertNotIn("time#units", dataset.GetMetadata())
        dataset = None


class BandTimesTests(QgisTestCase):
    """Decode whole datasets, fixture by fixture."""

    def test_met_tile_decodes_to_three_february_days(self) -> None:
        dataset = gdal.Open(str(MET_TILE_N))
        spans = band_times(dataset, "daily")
        dataset = None
        self.assertEqual([(FEB[0], FEB[1]), (FEB[1], FEB[2]), (FEB[2], FEB[3])],
                         spans)

    def test_solar_tile_decodes_to_the_same_three_days(self) -> None:
        # Same window, same answer, from raw values thousands apart.
        dataset = gdal.Open(str(SOLAR_TILE_N))
        spans = band_times(dataset, "daily")
        dataset = None
        self.assertEqual([(FEB[0], FEB[1]), (FEB[1], FEB[2]), (FEB[2], FEB[3])],
                         spans)

    def test_monthly_file_decodes_to_26_bands_with_2_annual_means(self) -> None:
        dataset = gdal.Open(str(MONTHLY))
        spans = band_times(dataset, "monthly")
        dataset = None
        self.assertEqual(26, len(spans))
        self.assertEqual([13, 26], [i for i, s in enumerate(spans, 1) if s is None])
        self.assertEqual(datetime(2020, 1, 1, tzinfo=UTC), spans[0][0])
        self.assertEqual(datetime(2021, 12, 1, tzinfo=UTC), spans[24][0])

    def test_explicit_units_override_the_dataset(self) -> None:
        # The path mosaic() uses: units handed in rather than read off the
        # dataset, because the dataset it passes is a VRT.
        dataset = gdal.Open(str(MET_TILE_N))
        spans = band_times(dataset, "daily", MET_UNITS)
        dataset = None
        self.assertEqual(FEB[0], spans[0][0])

    def test_a_dataset_without_netcdf_dim_time_raises(self) -> None:
        # A plain GeoTIFF is not a POWER regional NetCDF, and saying so beats
        # decoding band 1 of whatever it is as a timestep.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plain.tif"
            driver = gdal.GetDriverByName("GTiff")
            dataset = driver.Create(str(path), 2, 2, 1, gdal.GDT_Float32)
            dataset.FlushCache()
            with self.assertRaises(CfTimeError):
                band_times(dataset, "daily", MET_UNITS)
            dataset = None


class VrtLosesTimeUnitsTests(QgisTestCase):
    """The regression test for the shipped bug.

    ``BuildVRT`` keeps per-band ``NETCDF_DIM_time`` and drops dataset metadata.
    Reading ``time#units`` off the VRT therefore returned ``None``, which sent
    every *daily* response down the units-less monthly path.
    """

    def build_vrt(self, tmp: str) -> gdal.Dataset:
        vrt = gdal.BuildVRT(
            str(Path(tmp) / "tiles.vrt"),
            [str(MET_TILE_N), str(MET_TILE_S)],
            VRTNodata="nan",
        )
        vrt.FlushCache()
        return vrt

    def test_the_vrt_really_does_lose_time_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vrt = self.build_vrt(tmp)
            # The source tile has it...
            source = gdal.Open(str(MET_TILE_N))
            self.assertEqual(MET_UNITS, units_of(source))
            source = None
            # ...and the VRT built from that very tile does not.
            self.assertIsNone(units_of(vrt))
            self.assertEqual({}, vrt.GetMetadata())
            vrt = None

    def test_the_vrt_does_keep_per_band_netcdf_dim_time(self) -> None:
        # Which is what makes the fix possible at all: the axis values survive,
        # only the units to interpret them against are gone.
        with tempfile.TemporaryDirectory() as tmp:
            vrt = self.build_vrt(tmp)
            raw = [
                vrt.GetRasterBand(i).GetMetadataItem("NETCDF_DIM_time")
                for i in range(1, vrt.RasterCount + 1)
            ]
            vrt = None
        self.assertEqual(["15737", "15738", "15739"], raw)

    def test_decoding_the_vrt_off_its_own_metadata_fails_loudly(self) -> None:
        # The bug itself: units_of(vrt) is None, so a daily response takes
        # the units-less monthly path and 15737 reaches the YYYYMM decoder,
        # which cannot read a five-digit stamp. Every daily mosaic died here.
        with tempfile.TemporaryDirectory() as tmp:
            vrt = self.build_vrt(tmp)
            with self.assertRaises(TimeKeyError):
                band_times(vrt, "daily")
            vrt = None

    def test_decoding_the_vrt_with_the_source_units_is_correct(self) -> None:
        # The fix: take time#units from a source tile, hand it to band_times.
        with tempfile.TemporaryDirectory() as tmp:
            vrt = self.build_vrt(tmp)
            source = gdal.Open(str(MET_TILE_N))
            spans = band_times(vrt, "daily", units_of(source))
            source = None
            vrt = None
        self.assertEqual([(FEB[0], FEB[1]), (FEB[1], FEB[2]), (FEB[2], FEB[3])],
                         spans)
