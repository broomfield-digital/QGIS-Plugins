"""Stitch real POWER tiles together and check the result against a second wire format.

Everything here runs on the committed ``.nc`` captures, because the facts being
pinned are properties of POWER's actual grids and none of them survive being
invented:

* **Whether two tiles share an edge row depends on the parent grid.** MERRA-2's
  0.5 deg grid has a node exactly on integer degrees, so tiles meeting at
  latitude 40.0 both return that row: 3x5 + 3x5 must mosaic to **3x9**, not
  3x10. CERES's 1.0 deg grid is centred on half-degrees, so an integer boundary
  falls *between* cells and nothing is shared: 2x2 + 2x2 gives **2x4**. Both
  numbers are measured, and neither is derivable from the other.
* **The two families are not co-registered** (P-32), so mosaicking across them
  is a hard error, not a warning -- a misaligned raster looks entirely
  plausible.
* **Regional NetCDF carries no CRS** (P-18). :class:`RawFixtureTests` asserts
  ``GetProjection() == ''`` on the raw files *first*, so the EPSG:4326
  assertion on the output proves the fix rather than a GDAL default.
* **``YYYY13`` on the raster path** (P-8): 26 bands for 24 months.

:class:`CrossFormatTests` is the strongest correctness check available. The same
bbox and day were fetched twice, once as NetCDF and once as the GeoJSON
FeatureCollection of cell centres, and every one of the 15 JSON cells must land
in the middle of a mosaic pixel carrying its exact value. Georeferencing,
row order, band selection and the dedupe all have to be right simultaneously
for that to hold, and the two formats were produced by independent code paths
at NASA.
"""

from __future__ import annotations

import math
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from osgeo import gdal, osr

from nasa_power.gdalio.mosaic import (
    NODATA,
    MosaicError,
    band_statistics,
    check_same_family,
    mosaic,
)
from tests.qgis.qgis_case import FIXTURES, QgisTestCase, load_json

gdal.UseExceptions()

UTC = timezone.utc

MET_TILE_N = FIXTURES / "regional_daily_t2m_tileN.nc"
MET_TILE_S = FIXTURES / "regional_daily_t2m_tileS.nc"
SOLAR_TILE_N = FIXTURES / "regional_solar_tileN.nc"
SOLAR_TILE_S = FIXTURES / "regional_solar_tileS.nc"
MONTHLY = FIXTURES / "regional_monthly_t2m.nc"
#: The same bbox and day as the met tiles, fetched as GeoJSON instead.
REGIONAL_FC = "regional_daily_fc.json"

#: Each MERRA-2 tile is 3 columns x 5 rows on a 0.625 x 0.5 deg grid.
MET_TILE_SHAPE = (3, 5)
#: They share the lat-40.0 row, so the mosaic is 9 rows and not 10.
MET_MOSAIC_SHAPE = (3, 9)
MET_GEOTRANSFORM = (-105.9375, 0.625, 0.0, 42.25, 0.0, -0.5)

#: Each CERES tile is 2x2 on a 1.0 deg grid centred on half-degrees.
SOLAR_TILE_SHAPE = (2, 2)
#: Nothing is shared, so 2 + 2 rows really are 4.
SOLAR_MOSAIC_SHAPE = (2, 4)
SOLAR_GEOTRANSFORM = (-106.0, 1.0, 0.0, 42.0, 0.0, -1.0)

#: A 2020-2021 monthly response: 26 bands, 24 months, annual means at 13 and 26.
MONTHLY_BANDS_IN = 26
MONTHLY_BANDS_OUT = 24
MONTHLY_ANNUAL_MEAN_BANDS = [13, 26]

#: JSON uses -999.0 and NetCDF uses NaN: two fill sentinels for one API (P-11).
JSON_FILL_VALUE = -999.0

FEB_1 = datetime(2024, 2, 1, tzinfo=UTC)


def _pixel_of(geotransform, lon: float, lat: float) -> tuple[float, float]:
    """Fractional pixel coordinates of a map point. 0.5 means a cell centre."""
    return (
        (lon - geotransform[0]) / geotransform[1],
        (lat - geotransform[3]) / geotransform[5],
    )


class MosaicTestCase(QgisTestCase):
    """A scratch directory per test, and datasets closed before it is removed."""

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        # LIFO: the directory goes last, after every dataset is released.
        self.addCleanup(self._tmp.cleanup)
        self._datasets: list = []
        self.addCleanup(self._datasets.clear)
        self.tmp = Path(self._tmp.name)

    def out(self, name: str = "mosaic.tif") -> Path:
        return self.tmp / name

    def open(self, path) -> gdal.Dataset:
        dataset = gdal.Open(str(path))
        self._datasets.append(dataset)
        return dataset

    def met_mosaic(self, **kwargs):
        kwargs.setdefault("temporal", "daily")
        kwargs.setdefault("parameter", "T2M")
        kwargs.setdefault("sources", ("MERRA2",))
        return mosaic([MET_TILE_N, MET_TILE_S], self.out(), **kwargs)


class RawFixtureTests(MosaicTestCase):
    """What POWER actually hands over, asserted before anything is fixed."""

    def test_no_power_netcdf_carries_a_crs(self) -> None:
        # P-18. The layer still draws in a WGS84 project, so the omission is
        # invisible until someone opens a projected one. Asserted here so the
        # EPSG:4326 test below proves the fix and not a default.
        for tile in (MET_TILE_N, MET_TILE_S, SOLAR_TILE_N, SOLAR_TILE_S, MONTHLY):
            with self.subTest(tile=tile.name):
                self.assertEqual("", self.open(tile).GetProjection())

    def test_the_netcdf_fill_value_is_nan_while_the_json_is_minus_999(self) -> None:
        # P-11: two sentinels for one API, in the same bbox on the same day.
        nodata = self.open(MET_TILE_N).GetRasterBand(1).GetNoDataValue()
        self.assertTrue(math.isnan(nodata))
        self.assertEqual(JSON_FILL_VALUE, load_json(REGIONAL_FC)["header"]["fill_value"])

    def test_the_met_tiles_are_3x5_each_and_share_the_lat_40_row(self) -> None:
        north, south = self.open(MET_TILE_N), self.open(MET_TILE_S)
        for tile in (north, south):
            self.assertEqual(MET_TILE_SHAPE, (tile.RasterXSize, tile.RasterYSize))
        # North spans 42.25 down to 39.75, south 40.25 down to 37.75: the row
        # centred on 40.0 is inside both.
        self.assertEqual(42.25, north.GetGeoTransform()[3])
        self.assertEqual(40.25, south.GetGeoTransform()[3])
        self.assertEqual(-0.5, north.GetGeoTransform()[5])

    def test_the_solar_tiles_are_2x2_each_and_share_nothing(self) -> None:
        north, south = self.open(SOLAR_TILE_N), self.open(SOLAR_TILE_S)
        for tile in (north, south):
            self.assertEqual(SOLAR_TILE_SHAPE, (tile.RasterXSize, tile.RasterYSize))
        # North's south edge is exactly south's north edge: 42.0 -> 40.0 -> 38.0,
        # and the cell centres are on half-degrees, so no row is served twice.
        self.assertEqual(42.0, north.GetGeoTransform()[3])
        self.assertEqual(40.0, south.GetGeoTransform()[3])

    def test_the_monthly_file_really_has_26_bands(self) -> None:
        self.assertEqual(MONTHLY_BANDS_IN, self.open(MONTHLY).RasterCount)


class Merra2DedupeTests(MosaicTestCase):
    """3x5 + 3x5 = 3x9. The row on latitude 40.0 is served by both tiles."""

    def test_the_shared_edge_row_is_not_duplicated(self) -> None:
        result = self.met_mosaic()
        self.assertEqual(MET_MOSAIC_SHAPE, (result.width, result.height))
        # The arithmetic that would be wrong: 5 + 5 = 10 rows.
        self.assertNotEqual(10, result.height)

    def test_the_file_on_disk_has_those_dimensions_too(self) -> None:
        result = self.met_mosaic()
        dataset = self.open(result.path)
        self.assertEqual(MET_MOSAIC_SHAPE, (dataset.RasterXSize, dataset.RasterYSize))

    def test_the_geotransform_spans_both_tiles_at_the_tile_resolution(self) -> None:
        self.met_mosaic()
        transform = self.open(self.out()).GetGeoTransform()
        self.assertEqual(MET_GEOTRANSFORM, transform)
        # 42.25 down to 37.75 is 4.5 deg, which is 9 rows of 0.5 -- the whole
        # dedupe restated as geometry.
        self.assertAlmostEqual(4.5, abs(transform[5]) * MET_MOSAIC_SHAPE[1], places=9)

    def test_the_shared_row_holds_one_consistent_set_of_values(self) -> None:
        # Both tiles carry latitude 40.0. If they disagreed, "which tile wins"
        # would be a silent choice; they do not, so the VRT's last-wins rule
        # is safe. Row 4 of the mosaic is that row.
        self.met_mosaic()
        merged = self.open(self.out()).GetRasterBand(1).ReadAsArray()
        north = self.open(MET_TILE_N).GetRasterBand(1).ReadAsArray()
        south = self.open(MET_TILE_S).GetRasterBand(1).ReadAsArray()
        np.testing.assert_allclose(north[4], south[0], rtol=0, atol=1e-6)
        np.testing.assert_allclose(merged[4], north[4], rtol=0, atol=1e-6)


class CeresNonDedupeTests(MosaicTestCase):
    """2x2 + 2x2 = 2x4. The counter-example that makes the dedupe a measurement."""

    def test_nothing_is_shared_on_the_one_degree_grid(self) -> None:
        result = mosaic(
            [SOLAR_TILE_N, SOLAR_TILE_S], self.out(),
            temporal="daily", parameter="ALLSKY_SFC_SW_DWN", sources=("SYN1DEG",),
        )
        self.assertEqual(SOLAR_MOSAIC_SHAPE, (result.width, result.height))
        # If the dedupe were a blanket "drop a row per seam" it would be 3.
        self.assertNotEqual(3, result.height)

    def test_the_geotransform_runs_42_to_38(self) -> None:
        mosaic([SOLAR_TILE_N, SOLAR_TILE_S], self.out(),
               temporal="daily", parameter="ALLSKY_SFC_SW_DWN")
        transform = self.open(self.out()).GetGeoTransform()
        self.assertEqual(SOLAR_GEOTRANSFORM, transform)
        self.assertAlmostEqual(
            38.0, transform[3] + transform[5] * SOLAR_MOSAIC_SHAPE[1], places=9
        )

    def test_tile_order_does_not_change_the_result(self) -> None:
        south_first = mosaic([SOLAR_TILE_S, SOLAR_TILE_N], self.out("a.tif"),
                             temporal="daily", parameter="ALLSKY_SFC_SW_DWN")
        north_first = mosaic([SOLAR_TILE_N, SOLAR_TILE_S], self.out("b.tif"),
                             temporal="daily", parameter="ALLSKY_SFC_SW_DWN")
        self.assertEqual((south_first.width, south_first.height),
                         (north_first.width, north_first.height))
        np.testing.assert_allclose(
            self.open(self.out("a.tif")).GetRasterBand(1).ReadAsArray(),
            self.open(self.out("b.tif")).GetRasterBand(1).ReadAsArray(),
            rtol=0, atol=1e-6,
        )


class CrossFormatTests(MosaicTestCase):
    """The mosaic against the GeoJSON of the same request. 15 cells, 0 mismatches."""

    def features(self) -> list[tuple[float, float, float]]:
        return [
            (feature["geometry"]["coordinates"][0],
             feature["geometry"]["coordinates"][1],
             feature["properties"]["parameter"]["T2M"]["20240201"])
            for feature in load_json(REGIONAL_FC)["features"]
        ]

    def test_the_json_capture_is_the_15_cell_bbox_it_claims_to_be(self) -> None:
        # 3 x 5 cell centres for a 2 deg bbox on the MERRA-2 grid, matching one
        # NetCDF tile exactly. Guards the cross-check below against a fixture
        # that quietly shrank.
        self.assertEqual(15, len(self.features()))
        self.assertEqual(["MERRA2"], load_json(REGIONAL_FC)["header"]["sources"])

    def test_every_json_cell_centre_lands_in_the_middle_of_a_mosaic_pixel(self) -> None:
        # The JSON serves cell CENTRES; the raster is anchored on cell corners.
        # So each feature must sit at exactly x.5 of a pixel in both axes --
        # a half-pixel georeferencing error would show up here and nowhere else.
        self.met_mosaic()
        transform = self.open(self.out()).GetGeoTransform()
        for lon, lat, _ in self.features():
            with self.subTest(lon=lon, lat=lat):
                x, y = _pixel_of(transform, lon, lat)
                self.assertAlmostEqual(0.5, x - math.floor(x), places=9)
                self.assertAlmostEqual(0.5, y - math.floor(y), places=9)

    def test_every_json_value_matches_the_pixel_it_falls_in(self) -> None:
        # Measured: 15 cells checked, 0 mismatches. Two independent wire
        # formats agreeing cell for cell is the strongest evidence available
        # that the raster is georeferenced, ordered and banded correctly.
        result = self.met_mosaic()
        dataset = self.open(result.path)
        transform = dataset.GetGeoTransform()
        band = dataset.GetRasterBand(1)  # 20240201, the day the JSON covers
        self.assertEqual("T2M 2024-02-01", band.GetDescription())
        values = band.ReadAsArray()

        checked = 0
        mismatched: list[tuple[float, float, float, float]] = []
        for lon, lat, expected in self.features():
            x, y = _pixel_of(transform, lon, lat)
            column, row = int(math.floor(x)), int(math.floor(y))
            self.assertTrue(0 <= column < result.width, f"{lon} outside the mosaic")
            self.assertTrue(0 <= row < result.height, f"{lat} outside the mosaic")
            found = float(values[row, column])
            checked += 1
            if abs(found - expected) > 1e-4:
                mismatched.append((lon, lat, expected, found))

        self.assertEqual(15, checked)
        self.assertEqual([], mismatched)


class NoYFlipTests(MosaicTestCase):
    """Row 0 is the north edge. GDAL already reverses the south-up CF ``lat``."""

    def test_the_geotransform_is_north_up(self) -> None:
        self.met_mosaic()
        self.assertLess(self.open(self.out()).GetGeoTransform()[5], 0)

    def test_row_zero_is_the_northernmost_latitude(self) -> None:
        result = self.met_mosaic()
        transform = self.open(result.path).GetGeoTransform()
        top = transform[3] + 0.5 * transform[5]
        bottom = transform[3] + (result.height - 0.5) * transform[5]
        self.assertGreater(top, bottom)
        self.assertAlmostEqual(42.0, top, places=9)
        self.assertAlmostEqual(38.0, bottom, places=9)

    def test_row_zero_carries_the_latitude_42_values_from_the_json(self) -> None:
        # A flip would be silent: the raster would still draw, upside down,
        # with plausible temperatures. This is the only assertion that catches
        # it, because it names the values that belong on that row.
        self.met_mosaic()
        row_zero = self.open(self.out()).GetRasterBand(1).ReadAsArray()[0]
        expected = [
            feature["properties"]["parameter"]["T2M"]["20240201"]
            for feature in load_json(REGIONAL_FC)["features"]
            if feature["geometry"]["coordinates"][1] == 42.0
        ]
        self.assertEqual(3, len(expected))
        np.testing.assert_allclose(row_zero, expected, rtol=0, atol=1e-4)

    def test_the_raw_tile_is_already_north_up_so_no_flip_is_applied(self) -> None:
        # The CF lat array runs south to north, but GDAL reverses it on read,
        # so a flip in this codebase would be a second one.
        tile = self.open(MET_TILE_N)
        self.assertLess(tile.GetGeoTransform()[5], 0)
        np.testing.assert_allclose(
            tile.GetRasterBand(1).ReadAsArray()[0],
            [2.84, 5.61, 6.06],  # the latitude 42.0 row, from the JSON capture
            rtol=0, atol=1e-4,
        )


class MonthlyAnnualMeanTests(MosaicTestCase):
    """26 bands in, 24 out, and nothing downstream ever sees a month 13."""

    def build(self):
        return mosaic([MONTHLY], self.out(), temporal="monthly",
                      parameter="T2M", sources=("MERRA2",))

    def test_26_in_24_out(self) -> None:
        result = self.build()
        self.assertEqual(MONTHLY_BANDS_IN, self.open(MONTHLY).RasterCount)
        self.assertEqual(MONTHLY_BANDS_OUT, result.band_count)
        self.assertEqual(MONTHLY_BANDS_OUT, self.open(result.path).RasterCount)
        self.assertEqual(MONTHLY_BANDS_OUT, len(result.intervals))

    def test_the_dropped_bands_are_named(self) -> None:
        # 202013 and 202113: reported, not silently discarded, so a user who
        # asked for two years and got 24 frames can see which two went.
        self.assertEqual(MONTHLY_ANNUAL_MEAN_BANDS, self.build().dropped_bands)

    def test_no_interval_is_a_thirteenth_month(self) -> None:
        months = {start.month for start, _ in self.build().intervals}
        self.assertNotIn(13, months)
        self.assertEqual(12, max(months))
        self.assertEqual(1, min(months))

    def test_the_kept_months_are_the_24_real_ones_in_order(self) -> None:
        intervals = self.build().intervals
        self.assertEqual(datetime(2020, 1, 1, tzinfo=UTC), intervals[0][0])
        self.assertEqual(datetime(2021, 12, 1, tzinfo=UTC), intervals[-1][0])
        self.assertEqual(datetime(2022, 1, 1, tzinfo=UTC), intervals[-1][1])
        for earlier, later in zip(intervals, intervals[1:]):
            # Half-open and contiguous straight across the dropped annual mean.
            self.assertEqual(earlier[1], later[0])

    def test_output_band_13_is_january_2021_not_the_annual_mean(self) -> None:
        # Source band 13 is 202013. Output band 13 must be 202101, which is
        # source band 14 -- the off-by-one this whole path exists to avoid.
        result = self.build()
        dataset = self.open(result.path)
        self.assertEqual("T2M 2021-01-01", dataset.GetRasterBand(13).GetDescription())
        self.assertEqual("T2M 2021-12-01", dataset.GetRasterBand(24).GetDescription())

    def test_the_dropped_band_values_are_not_in_the_output(self) -> None:
        # The annual mean is a real, plausible-looking field; the check that it
        # is gone has to be on the numbers, not on the band count.
        result = self.build()
        annual_mean = self.open(MONTHLY).GetRasterBand(13).ReadAsArray()
        output = self.open(result.path)
        for index in range(1, output.RasterCount + 1):
            with self.subTest(band=index):
                self.assertFalse(
                    np.allclose(output.GetRasterBand(index).ReadAsArray(),
                                annual_mean, rtol=0, atol=1e-6)
                )


class CrossFamilyTests(MosaicTestCase):
    """Solar and meteorology are not co-registered, so this is an error (P-32)."""

    def test_mosaicking_a_solar_tile_with_a_met_tile_raises(self) -> None:
        with self.assertRaises(MosaicError) as caught:
            mosaic([SOLAR_TILE_N, MET_TILE_N], self.out(), temporal="daily")
        self.assertIn("CROSS_FAMILY_MOSAIC", str(caught.exception))

    def test_the_refusal_does_not_depend_on_tile_order(self) -> None:
        with self.assertRaises(MosaicError) as caught:
            mosaic([MET_TILE_N, SOLAR_TILE_N], self.out(), temporal="daily")
        self.assertIn("CROSS_FAMILY_MOSAIC", str(caught.exception))

    def test_nothing_is_written_when_the_mosaic_is_refused(self) -> None:
        # A half-written raster left behind would be loadable, and wrong.
        with self.assertRaises(MosaicError):
            mosaic([SOLAR_TILE_N, MET_TILE_N], self.out(), temporal="daily")
        self.assertFalse(self.out().exists())
        self.assertFalse(self.out().with_suffix(".vrt").exists())

    def test_check_same_family_accepts_tiles_from_one_grid(self) -> None:
        self.assertIsNone(check_same_family([MET_TILE_N, MET_TILE_S]))
        self.assertIsNone(check_same_family([SOLAR_TILE_N, SOLAR_TILE_S]))

    def test_check_same_family_names_the_code(self) -> None:
        with self.assertRaises(MosaicError) as caught:
            check_same_family([MET_TILE_N, SOLAR_TILE_S])
        self.assertIn("CROSS_FAMILY_MOSAIC", str(caught.exception))

    def test_a_solar_geometry_parameter_has_no_grid_to_mosaic(self) -> None:
        # SG_* is computed from geometry, so there is no raster to build even
        # though every tile handed in is on one grid.
        with self.assertRaises(MosaicError):
            check_same_family([MET_TILE_N], parameter="SG_DEC")

    def test_the_grids_really_do_differ(self) -> None:
        # The measurement the refusal rests on: 0.625 x 0.5 against 1.0 x 1.0.
        met = self.open(MET_TILE_N).GetGeoTransform()
        solar = self.open(SOLAR_TILE_N).GetGeoTransform()
        self.assertEqual((0.625, -0.5), (met[1], met[5]))
        self.assertEqual((1.0, -1.0), (solar[1], solar[5]))


class OutputPropertyTests(MosaicTestCase):
    """Everything the GeoTIFF has to say for itself once the plugin is gone."""

    def test_the_output_is_epsg_4326(self) -> None:
        # RawFixtureTests proved the input has no CRS at all, so this is the
        # fix and not a default.
        self.met_mosaic()
        srs = osr.SpatialReference()
        srs.ImportFromWkt(self.open(self.out()).GetProjection())
        self.assertEqual("EPSG", srs.GetAuthorityName(None))
        self.assertEqual("4326", srs.GetAuthorityCode(None))

    def test_band_descriptions_are_readable_dates(self) -> None:
        # Not "Band 1: time=15737 (days since 1980-12-31)", which is what
        # gdal.Translate of the VRT would have put in the QGIS band list.
        self.met_mosaic()
        dataset = self.open(self.out())
        self.assertEqual(
            ["T2M 2024-02-01", "T2M 2024-02-02", "T2M 2024-02-03"],
            [dataset.GetRasterBand(i).GetDescription()
             for i in range(1, dataset.RasterCount + 1)],
        )

    def test_every_band_carries_the_unit_the_vrt_drops(self) -> None:
        # BuildVRT loses the band unit type as well as the dataset metadata,
        # so "C" has to come from a source tile or a temperature map ends up
        # unlabelled and a reader guesses.
        result = self.met_mosaic()
        vrt = gdal.BuildVRT(str(self.tmp / "probe.vrt"),
                            [str(MET_TILE_N), str(MET_TILE_S)], VRTNodata="nan")
        vrt.FlushCache()
        self.assertEqual("C", self.open(MET_TILE_N).GetRasterBand(1).GetUnitType())
        self.assertEqual("", vrt.GetRasterBand(1).GetUnitType())
        vrt = None

        self.assertEqual("C", result.units)
        dataset = self.open(result.path)
        for index in range(1, dataset.RasterCount + 1):
            with self.subTest(band=index):
                self.assertEqual("C", dataset.GetRasterBand(index).GetUnitType())

    def test_the_solar_unit_survives_too(self) -> None:
        result = mosaic([SOLAR_TILE_N, SOLAR_TILE_S], self.out(),
                        temporal="daily", parameter="ALLSKY_SFC_SW_DWN")
        self.assertEqual("kW-hr/m^2/day", result.units)
        self.assertEqual("kW-hr/m^2/day",
                         self.open(result.path).GetRasterBand(1).GetUnitType())

    def test_nodata_is_nan_on_every_band(self) -> None:
        # POWER's NetCDF fill value, carried through rather than translated to
        # a magic number that a colour ramp would happily stretch across.
        self.assertTrue(math.isnan(NODATA))
        self.met_mosaic()
        dataset = self.open(self.out())
        for index in range(1, dataset.RasterCount + 1):
            with self.subTest(band=index):
                self.assertTrue(math.isnan(dataset.GetRasterBand(index).GetNoDataValue()))

    def test_each_band_stamps_its_own_half_open_interval(self) -> None:
        result = self.met_mosaic()
        dataset = self.open(result.path)
        for index, (start, end) in enumerate(result.intervals, start=1):
            with self.subTest(band=index):
                band = dataset.GetRasterBand(index)
                self.assertEqual(start.isoformat(),
                                 band.GetMetadataItem("POWER_DATETIME_START"))
                self.assertEqual(end.isoformat(),
                                 band.GetMetadataItem("POWER_DATETIME_END"))
                # Round-trips to an aware UTC datetime, which is what the
                # temporal controller needs.
                parsed = datetime.fromisoformat(
                    band.GetMetadataItem("POWER_DATETIME_START"))
                self.assertEqual(start, parsed)
                self.assertEqual(UTC, parsed.tzinfo)

    def test_band_1_start_is_the_day_the_json_covers(self) -> None:
        self.assertEqual(FEB_1, self.met_mosaic().intervals[0][0])

    def test_dataset_metadata_carries_the_provenance(self) -> None:
        result = self.met_mosaic()
        metadata = self.open(result.path).GetMetadata()
        self.assertEqual("T2M", metadata["POWER_PARAMETER"])
        self.assertEqual("C", metadata["POWER_UNITS"])
        self.assertEqual("MERRA2", metadata["POWER_SOURCES"])
        self.assertEqual("daily", metadata["POWER_TEMPORAL"])
        self.assertEqual("2", metadata["POWER_TILES"])

    def test_several_sources_are_kept_as_a_list(self) -> None:
        result = mosaic([MET_TILE_N, MET_TILE_S], self.out(), temporal="daily",
                        parameter="T2M", sources=("MERRA2", "GEOS-5.12.4"))
        self.assertEqual("MERRA2,GEOS-5.12.4",
                         self.open(result.path).GetMetadata()["POWER_SOURCES"])
        self.assertEqual(("MERRA2", "GEOS-5.12.4"), result.sources)

    def test_the_result_describes_the_file_it_wrote(self) -> None:
        result = self.met_mosaic()
        dataset = self.open(result.path)
        self.assertEqual(self.out(), result.path)
        self.assertTrue(result.path.exists())
        self.assertEqual(result.band_count, dataset.RasterCount)
        self.assertEqual(result.width, dataset.RasterXSize)
        self.assertEqual(result.height, dataset.RasterYSize)
        self.assertEqual("T2M", result.parameter)
        self.assertEqual([], result.dropped_bands)


class VrtScaffoldingTests(MosaicTestCase):
    """The VRT is a means, not an output -- unless the caller asks for it."""

    def test_the_scaffolding_vrt_is_deleted(self) -> None:
        # It points at cache files that may be cleaned later; left behind, it
        # invites someone to load a layer that breaks a week from now.
        self.met_mosaic()
        self.assertFalse(self.out().with_suffix(".vrt").exists())
        self.assertEqual(["mosaic.tif"], sorted(p.name for p in self.tmp.iterdir()))

    def test_an_explicit_vrt_path_is_kept_and_is_loadable(self) -> None:
        kept = self.tmp / "keep.vrt"
        result = self.met_mosaic(vrt_path=kept)
        self.assertTrue(kept.exists())
        vrt = self.open(kept)
        self.assertEqual(3, vrt.RasterCount)
        self.assertEqual((result.width, result.height),
                         (vrt.RasterXSize, vrt.RasterYSize))

    def test_the_output_is_written_either_way(self) -> None:
        result = self.met_mosaic(vrt_path=self.tmp / "keep.vrt")
        self.assertTrue(result.path.exists())
        self.assertEqual(MET_MOSAIC_SHAPE, (result.width, result.height))


class BandStatisticsTests(MosaicTestCase):
    """One stretch for the whole animation, so the colours move only with the data."""

    def write(self, name: str, bands) -> Path:
        path = self.tmp / name
        driver = gdal.GetDriverByName("GTiff")
        height, width = np.asarray(bands[0]).shape
        dataset = driver.Create(str(path), width, height, len(bands), gdal.GDT_Float32)
        for index, values in enumerate(bands, start=1):
            band = dataset.GetRasterBand(index)
            band.WriteArray(np.asarray(values, dtype="float32"))
            band.SetNoDataValue(NODATA)
        dataset.FlushCache()
        dataset = None
        return path

    def test_the_range_spans_every_band_not_just_the_first(self) -> None:
        # Band 3's maximum is 99 and band 2's minimum is -1, neither of them in
        # band 1. Per-band limits make an animation shimmer: the colours would
        # move because the scale moved, not because the weather did.
        path = self.write("synthetic.tif", [
            [[0.0, 1.0], [2.0, 3.0]],       # band 1: 0 .. 3
            [[-1.0, 0.5], [0.0, 0.0]],      # band 2: -1 .. 0.5
            [[4.0, 99.0], [1.0, 2.0]],      # band 3: 1 .. 99
        ])
        low, high = band_statistics(path)
        self.assertAlmostEqual(-1.0, low, places=5)
        self.assertAlmostEqual(99.0, high, places=5)
        # What band 1 alone would have said.
        self.assertNotAlmostEqual(3.0, high, places=5)

    def test_nodata_pixels_do_not_enter_the_range(self) -> None:
        path = self.write("withnan.tif", [
            [[float("nan"), 1.0], [2.0, 3.0]],
            [[-1.0, float("nan")], [0.0, 0.0]],
        ])
        low, high = band_statistics(path)
        self.assertAlmostEqual(-1.0, low, places=5)
        self.assertAlmostEqual(3.0, high, places=5)

    def test_an_all_nodata_raster_has_no_range(self) -> None:
        # A legal POWER response: an ocean-only bbox for a land parameter
        # (P-30). Styling has to decline rather than divide by a nonexistent
        # spread.
        nan = float("nan")
        path = self.write("allnan.tif", [[[nan, nan], [nan, nan]]] * 2)
        self.assertIsNone(band_statistics(path))

    def test_one_band_of_data_among_empty_ones_still_gives_a_range(self) -> None:
        nan = float("nan")
        path = self.write("mixed.tif", [
            [[nan, nan], [nan, nan]],
            [[5.0, 7.0], [nan, nan]],
        ])
        self.assertEqual((5.0, 7.0), band_statistics(path))

    def test_the_real_mosaic_range_comes_from_two_different_bands(self) -> None:
        # Measured on the committed tiles: the minimum -5.48 is in band 3 and
        # the maximum 8.48 is in band 1, so a band-1-only stretch would clip
        # the coldest day off the bottom of the ramp.
        result = self.met_mosaic()
        low, high = band_statistics(result.path)
        self.assertAlmostEqual(-5.48, low, places=5)
        self.assertAlmostEqual(8.48, high, places=5)

        dataset = self.open(result.path)
        band_1 = dataset.GetRasterBand(1).ReadAsArray()
        self.assertLess(low, float(np.nanmin(band_1)))
        self.assertAlmostEqual(high, float(np.nanmax(band_1)), places=5)


class EdgeCaseTests(MosaicTestCase):
    """The two ends of the tile list."""

    def test_no_tiles_at_all_raises(self) -> None:
        with self.assertRaises(MosaicError):
            mosaic([], self.out(), temporal="daily", parameter="T2M")

    def test_a_single_tile_needs_no_tiling_and_is_passed_through(self) -> None:
        # A bbox under 10 deg is one request, which is the common case.
        result = mosaic([MET_TILE_N], self.out(), temporal="daily",
                        parameter="T2M", sources=("MERRA2",))
        self.assertEqual(MET_TILE_SHAPE, (result.width, result.height))
        self.assertEqual(3, result.band_count)
        dataset = self.open(result.path)
        self.assertEqual(MET_GEOTRANSFORM, dataset.GetGeoTransform())
        self.assertEqual("1", dataset.GetMetadata()["POWER_TILES"])
        np.testing.assert_allclose(
            dataset.GetRasterBand(1).ReadAsArray(),
            self.open(MET_TILE_N).GetRasterBand(1).ReadAsArray(),
            rtol=0, atol=1e-6,
        )

    def test_a_string_path_works_as_well_as_a_path(self) -> None:
        # Callers hand in whatever the cache gave them.
        result = mosaic([str(MET_TILE_N)], str(self.out()), temporal="daily",
                        parameter="T2M")
        self.assertEqual(MET_TILE_SHAPE, (result.width, result.height))

    def test_the_output_directory_is_created(self) -> None:
        nested = self.tmp / "a" / "b" / "out.tif"
        result = mosaic([MET_TILE_N], nested, temporal="daily", parameter="T2M")
        self.assertTrue(result.path.exists())


class SyntheticSourceTests(MosaicTestCase):
    """Three paths through ``mosaic`` that no committed capture reaches.

    ``tests/fixtures`` has no hourly regional response and none that is
    *only* annual means, so the sources here are built by hand. A source is a
    plain GeoTIFF carrying the two things ``mosaic`` actually reads off a tile
    -- dataset-level ``time#units`` and per-band ``NETCDF_DIM_time`` -- which
    is the whole interface ``band_times`` depends on.
    """

    #: 2024-02-01 00:00 UTC written as hours since 1980-12-31: 15737 x 24.
    #: The daily fixtures carry 15737 for that same midnight.
    HOURLY_BASE = 15737 * 24

    ONE_DEGREE = (-106.0, 1.0, 0.0, 42.0, 0.0, -1.0)

    def source(self, name, arrays, stamps, units, geotransform=None):
        """A stand-in tile: ``time#units`` on the dataset, stamps on the bands."""
        path = self.tmp / name
        height, width = np.asarray(arrays[0]).shape
        dataset = gdal.GetDriverByName("GTiff").Create(
            str(path), width, height, len(arrays), gdal.GDT_Float32
        )
        dataset.SetGeoTransform(geotransform or self.ONE_DEGREE)
        if units:
            dataset.SetMetadata({"time#units": units})
        for index, values in enumerate(arrays, start=1):
            band = dataset.GetRasterBand(index)
            band.WriteArray(np.asarray(values, dtype="float32"))
            band.SetNoDataValue(NODATA)
            band.SetUnitType("C")
            band.SetMetadataItem("NETCDF_DIM_time", str(stamps[index - 1]))
        dataset.FlushCache()
        dataset = None
        return path

    def hourly_source(self, name="hourly.tif", hours=3):
        return self.source(
            name,
            [np.full((2, 2), float(n)) for n in range(hours)],
            [self.HOURLY_BASE + n for n in range(hours)],
            "hours since 1980-12-31",
        )

    def test_hourly_band_labels_carry_the_hour_and_daily_ones_do_not(self) -> None:
        # The only branch in the band label. Three midnight-to-02:00 bands all
        # fall on 2024-02-01, so with the daily format they would come out as
        # three bands with identical descriptions and the QGIS band list could
        # not tell one frame from another.
        result = mosaic([self.hourly_source()], self.out(), temporal="hourly",
                        parameter="T2M")
        dataset = self.open(result.path)
        self.assertEqual(
            ["T2M 2024-02-01 00:00", "T2M 2024-02-01 01:00", "T2M 2024-02-01 02:00"],
            [dataset.GetRasterBand(i).GetDescription() for i in (1, 2, 3)],
        )
        # Same source read as daily: one date, repeated, and no hour at all.
        daily = mosaic([self.hourly_source("h2.tif")], self.out("daily.tif"),
                       temporal="daily", parameter="T2M")
        other = self.open(daily.path)
        self.assertEqual(
            ["T2M 2024-02-01"] * 3,
            [other.GetRasterBand(i).GetDescription() for i in (1, 2, 3)],
        )

    def test_hourly_intervals_are_one_hour_wide_and_contiguous(self) -> None:
        result = mosaic([self.hourly_source()], self.out(), temporal="hourly",
                        parameter="T2M")
        self.assertEqual(FEB_1, result.intervals[0][0])
        for start, end in result.intervals:
            self.assertEqual(timedelta(hours=1), end - start)
        for earlier, later in zip(result.intervals, result.intervals[1:]):
            self.assertEqual(earlier[1], later[0])

    def test_a_response_that_is_all_annual_means_is_refused(self) -> None:
        # 202013 alone: every band a YYYY13, so keep_bands returns nothing and
        # there is no timestep to render. Writing a 0-band GeoTIFF instead
        # would hand QGIS a layer with no bands and no error to explain it.
        path = self.source("annual.tif", [np.full((2, 2), 5.0)], [202013], None)
        with self.assertRaises(MosaicError):
            mosaic([path], self.out(), temporal="monthly", parameter="T2M")
        self.assertFalse(self.out().exists())

    def test_a_nodata_pixel_does_not_punch_through_a_valid_neighbour(self) -> None:
        # Two tiles overlapping on one row, the second all-nodata across it.
        # Measured in both orders: the valid row survives, because the source
        # tiles declare NaN as their band nodata and the VRT skips those
        # pixels. (Measured separately: dropping VRTNodata="nan" from the
        # BuildVRT call changes none of these numbers -- it stamps the VRT
        # band's own sentinel, it is not what protects the overlap.)
        nan = float("nan")
        north = self.source("north.tif", [np.array([[1.0, 2.0], [3.0, 4.0]])],
                            [15737], "days since 1980-12-31")
        south = self.source("south.tif", [np.array([[nan, nan], [7.0, 8.0]])],
                            [15737], "days since 1980-12-31",
                            geotransform=(-106.0, 1.0, 0.0, 41.0, 0.0, -1.0))
        expected = [[1.0, 2.0], [3.0, 4.0], [7.0, 8.0]]
        for order, name in (([north, south], "a.tif"), ([south, north], "b.tif")):
            with self.subTest(nodata_tile_last=order[-1].name == "south.tif"):
                result = mosaic(order, self.out(name), temporal="daily",
                                parameter="T2M")
                self.assertEqual((2, 3), (result.width, result.height))
                values = self.open(result.path).GetRasterBand(1).ReadAsArray()
                np.testing.assert_allclose(values, expected, rtol=0, atol=1e-6)

    def test_a_nodata_pixel_stays_nan_and_is_not_written_as_a_number(self) -> None:
        # P-11, end to end on the pixels rather than on the declaration.
        # Measured: not one of the five committed .nc files contains a single
        # NaN (0 of 504 values), so no other test in this suite watches a fill
        # value travel through the write. Turned into 0.0 on the way out --
        # which is what a np.nan_to_num would do -- an ocean cell renders as a
        # plausible 0 C instead of transparent, and the whole-cube stretch
        # anchors on a floor that is not in the data.
        nan = float("nan")
        path = self.source("holes.tif", [np.array([[nan, 2.0], [3.0, nan]])],
                           [15737], "days since 1980-12-31")
        result = mosaic([path], self.out(), temporal="daily", parameter="T2M")
        values = self.open(result.path).GetRasterBand(1).ReadAsArray()
        self.assertTrue(math.isnan(float(values[0, 0])))
        self.assertTrue(math.isnan(float(values[1, 1])))
        self.assertEqual([2.0, 3.0], [float(values[0, 1]), float(values[1, 0])])
        # And the stretch ignores them rather than pinning its floor at 0.
        self.assertEqual((2.0, 3.0), band_statistics(result.path))
