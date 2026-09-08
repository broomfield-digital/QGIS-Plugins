"""Guard the gridded path: CRS, per-band time, and one stretch for every band.

This module exists because the GDAL provider hands back a raster that looks
fine and animates wrongly. Three measured gaps, each asserted here on the raw
``.nc`` **before** anything is asserted about the fix, so the later tests prove
a repair rather than a default:

* ``QgsRasterLayer(<power .nc>, ...).crs().authid()`` is ``''`` -- POWER ships
  no projection at all. In a WGS84 project the omission is invisible; in a
  projected one the layer lands in the wrong hemisphere.
* ``dataProvider().temporalCapabilities().hasTemporalCapabilities()`` is
  ``False``, for the mosaicked GeoTIFF as well as the source NetCDF, so
  ``TemporalRangeFromDataProvider`` is not available and every band's range has
  to be handed over explicitly.
* the provider's own band names are the raw CF axis --
  ``'Band 1: time=15737 (days since 1980-12-31)'``.

The one property that needs two layers to demonstrate is the **half-open**
interval. At an instant on a band boundary, half-open ranges match exactly one
band and closed ranges match two; the second is what makes a Temporal
Controller animation flicker between adjacent frames at every step.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from osgeo import gdal, osr
from qgis.core import (
    Qgis,
    QgsColorRampShader,
    QgsDateTimeRange,
    QgsRasterLayer,
    QgsSingleBandGrayRenderer,
    QgsSingleBandPseudoColorRenderer,
    QgsStyle,
)
from qgis.PyQt.QtCore import QDateTime, Qt, QTimeZone

from nasa_power.gdalio.mosaic import CREATION_OPTIONS, band_statistics, mosaic
from nasa_power.qgis_bridge.layers_raster import (
    POWER_CRS,
    band_for,
    build_raster_layer,
    style_raster_layer,
)
from nasa_power.qgis_bridge.styling import CLASS_COUNT
from tests.qgis.qgis_case import FIXTURES, QgisTestCase

#: The MERRA-2 daily pair. The two tiles share the lat-40.0 row, so the mosaic
#: is 3 x 9, not 3 x 10 -- pinned in the mosaic suite, relied on here.
DAILY_TILES = ("regional_daily_t2m_tileN.nc", "regional_daily_t2m_tileS.nc")
SOLAR_TILES = ("regional_solar_tileN.nc", "regional_solar_tileS.nc")

#: Measured on the daily fixture pair: 3 bands, 2024-02-01 .. 2024-02-03.
DAILY_BANDS = 3
DAILY_WIDTH, DAILY_HEIGHT = 3, 9

#: Measured whole-cube min/max over all three daily bands, in C.
CUBE_MIN, CUBE_MAX = -5.48, 8.48
#: Band 1 alone. Its minimum is 3.2 C warmer than the cube's, which is the gap
#: a per-band stretch would render as a colour change between frames.
BAND1_MIN, BAND1_MAX = -2.28, 8.48

#: The instant on the band 1 / band 2 boundary.
BOUNDARY = datetime(2024, 2, 2, tzinfo=timezone.utc)


def _mid(day: int) -> datetime:
    """Noon UTC on a February 2024 day -- unambiguously inside one band."""
    return datetime(2024, 2, day, 12, tzinfo=timezone.utc)


def _utc(moment: datetime) -> QDateTime:
    """A UTC ``QDateTime``, built the way the module builds one.

    Deliberately duplicated rather than imported from ``layers_raster``: the
    closed-range comparison layer has to be constructed here, with no help from
    the module under test, or the contrast proves nothing.
    """
    stamp = QDateTime(moment)
    stamp.setTimeZone(QTimeZone.utc())
    return stamp


def _instant(moment: datetime) -> QgsDateTimeRange:
    """A zero-width range, which is what a Temporal Controller frame is."""
    stamp = _utc(moment)
    return QgsDateTimeRange(stamp, stamp)


class RasterFixtureMixin:
    """One temp dir and one mosaic per class, built from committed fixtures."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.addClassCleanup(cls._tmp.cleanup)

    @classmethod
    def mosaic_daily(cls):
        return mosaic(
            [FIXTURES / name for name in DAILY_TILES],
            cls.tmp / "daily.tif",
            temporal="daily",
            parameter="T2M",
            sources=["MERRA2"],
        )

    @classmethod
    def mosaic_solar(cls):
        return mosaic(
            [FIXTURES / name for name in SOLAR_TILES],
            cls.tmp / "solar.tif",
            temporal="daily",
            parameter="ALLSKY_SFC_SW_DWN",
            sources=["CERES"],
        )

    @classmethod
    def mosaic_monthly(cls):
        return mosaic(
            [FIXTURES / "regional_monthly_t2m.nc"],
            cls.tmp / "monthly.tif",
            temporal="monthly",
            parameter="T2M",
        )

    @classmethod
    def raw_netcdf(cls, name: str = DAILY_TILES[0]) -> Path:
        """A copy of a fixture ``.nc``, outside the repo.

        Opening one through GDAL can drop a ``.aux.xml`` statistics sidecar
        beside it; the copy keeps that out of ``tests/fixtures``.
        """
        target = cls.tmp / Path(name).name
        if not target.exists():
            shutil.copy(FIXTURES / name, target)
        return target

    @classmethod
    def write_raster(cls, name: str, arrays) -> Path:
        """A minimal EPSG:4326 GeoTIFF, for cases no fixture covers."""
        path = cls.tmp / name
        height, width = arrays[0].shape
        driver = gdal.GetDriverByName("GTiff")
        out = driver.Create(
            str(path), width, height, len(arrays), gdal.GDT_Float32, CREATION_OPTIONS
        )
        out.SetGeoTransform((-106.0, 0.625, 0.0, 42.0, 0.0, -0.5))
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        out.SetProjection(srs.ExportToWkt())
        for index, values in enumerate(arrays, start=1):
            band = out.GetRasterBand(index)
            band.WriteArray(np.asarray(values, dtype="float32"))
            band.SetNoDataValue(float("nan"))
        out.FlushCache()
        out = None
        return path


class ProviderGapTests(RasterFixtureMixin, QgisTestCase):
    """What the GDAL provider does *not* give us. Everything else answers these."""

    def test_power_netcdf_carries_no_projection_at_all(self) -> None:
        # Measured on every POWER regional NetCDF: GetProjection() == ''.
        # Asserted at the GDAL level first so the layer-level assertion below
        # cannot be satisfied by a QGIS default.
        dataset = gdal.Open(str(self.raw_netcdf()))
        self.assertEqual("", dataset.GetProjection())
        dataset = None

    def test_a_raw_netcdf_layer_has_no_crs(self) -> None:
        layer = QgsRasterLayer(str(self.raw_netcdf()), "raw", "gdal")
        self.assertTrue(layer.isValid())
        # It loads, it draws, and it is unreferenced: authid '' and a CRS that
        # reports itself invalid. This is the whole reason setCrs() is called
        # unconditionally rather than only when the source lacks one.
        self.assertEqual("", layer.crs().authid())
        self.assertFalse(layer.crs().isValid())

    def test_the_provider_reports_no_temporal_capabilities(self) -> None:
        raw = QgsRasterLayer(str(self.raw_netcdf()), "raw", "gdal")
        self.assertFalse(
            raw.dataProvider().temporalCapabilities().hasTemporalCapabilities()
        )
        # And the GeoTIFF we write is no better -- GDAL has nowhere to put a
        # time axis -- so TemporalRangeFromDataProvider is not an option on
        # either end of the pipeline and FixedRangePerBand is forced.
        result = self.mosaic_daily()
        built = QgsRasterLayer(str(result.path), "mosaic", "gdal")
        self.assertFalse(
            built.dataProvider().temporalCapabilities().hasTemporalCapabilities()
        )

    def test_a_raw_netcdf_layer_is_not_temporal_and_names_bands_by_axis(self) -> None:
        layer = QgsRasterLayer(str(self.raw_netcdf()), "raw", "gdal")
        self.assertFalse(layer.temporalProperties().isActive())
        # The raw CF axis, verbatim: unreadable, and the reason mosaic sets a
        # description per band.
        self.assertIn("time=15737", layer.bandName(1))
        self.assertIn("days since 1980-12-31", layer.bandName(1))

    def test_a_python_utc_datetime_becomes_a_local_time_qdatetime(self) -> None:
        # The trap _qdatetime exists for. QDateTime(aware_datetime) keeps the
        # wall clock but marks it LocalTime, so without setTimeZone() every
        # band range would be off by the machine's UTC offset -- 7 h in
        # Denver, 9 h in Tokyo -- and a midnight-crossing frame would show the
        # wrong day. LocalTime here regardless of what the local zone is.
        naive = QDateTime(datetime(2024, 2, 1, tzinfo=timezone.utc))
        self.assertEqual(Qt.TimeSpec.LocalTime, naive.timeSpec())


class BuildRasterLayerTests(RasterFixtureMixin, QgisTestCase):
    """The layer itself: valid, referenced, and readable in the band list."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.result = cls.mosaic_daily()

    def test_the_layer_is_valid(self) -> None:
        layer = build_raster_layer(self.result.path, "T2M", self.result.intervals)
        self.assertTrue(layer.isValid())
        self.assertEqual("T2M", layer.name())

    def test_the_layer_is_stamped_epsg_4326(self) -> None:
        layer = build_raster_layer(self.result.path, "T2M", self.result.intervals)
        self.assertEqual("EPSG:4326", layer.crs().authid())
        self.assertTrue(layer.crs().isValid())
        self.assertEqual(POWER_CRS.authid(), layer.crs().authid())

    def test_the_crs_is_set_even_when_the_source_has_none(self) -> None:
        # Point it straight at the unreferenced NetCDF: the 3 bands line up
        # with the 3 daily intervals. If build_raster_layer only stamped the
        # GeoTIFF's existing CRS through, this would come back ''.
        layer = build_raster_layer(
            self.raw_netcdf(), "raw", self.result.intervals
        )
        self.assertEqual("EPSG:4326", layer.crs().authid())

    def test_band_count_and_size_match_the_mosaic(self) -> None:
        layer = build_raster_layer(self.result.path, "T2M", self.result.intervals)
        self.assertEqual(DAILY_BANDS, self.result.band_count)
        self.assertEqual(DAILY_BANDS, layer.bandCount())
        self.assertEqual(len(self.result.intervals), layer.bandCount())
        # 3 x 9, not 3 x 10: the two tiles share the lat-40.0 row.
        self.assertEqual((DAILY_WIDTH, DAILY_HEIGHT), (layer.width(), layer.height()))

    def test_band_names_carry_the_date_not_the_axis_value(self) -> None:
        layer = build_raster_layer(self.result.path, "T2M", self.result.intervals)
        days = ("2024-02-01", "2024-02-02", "2024-02-03")
        for index, day in enumerate(days, start=1):
            with self.subTest(band=index):
                name = layer.bandName(index)
                self.assertIn(day, name)
                self.assertIn("T2M", name)
                # 15737 is what Translate would have carried through.
                self.assertNotIn("time=", name)
                self.assertNotIn("15737", name)

    def test_monthly_bands_are_the_24_months_with_no_annual_means(self) -> None:
        result = self.mosaic_monthly()
        layer = build_raster_layer(result.path, "T2M", result.intervals)
        # 26 bands for 24 months: 202013 and 202113 are annual means, dropped.
        self.assertEqual(24, layer.bandCount())
        self.assertEqual([13, 26], result.dropped_bands)
        self.assertEqual(12, max(start.month for start, _end in result.intervals))
        self.assertIn("2020-01-01", layer.bandName(1))
        # Band 13 of the output is January 2021, not the 2020 annual mean.
        self.assertIn("2021-01-01", layer.bandName(13))
        self.assertIn("2021-12-01", layer.bandName(24))

    def test_an_unloadable_path_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            build_raster_layer(self.tmp / "does-not-exist.tif", "nope", [])


class TemporalPropertiesTests(RasterFixtureMixin, QgisTestCase):
    """FixedRangePerBand, active, 1-based, and half-open."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.result = cls.mosaic_daily()

    def setUp(self) -> None:
        super().setUp()
        self.layer = build_raster_layer(self.result.path, "T2M", self.result.intervals)
        self.properties = self.layer.temporalProperties()

    def test_the_mode_is_fixed_range_per_band(self) -> None:
        # Not TemporalRangeFromDataProvider: the provider has no capabilities.
        self.assertEqual(
            Qgis.RasterTemporalMode.FixedRangePerBand, self.properties.mode()
        )

    def test_temporal_properties_are_active(self) -> None:
        # An inactive layer is simply always visible: the Temporal Controller
        # would move and nothing would change.
        self.assertTrue(self.properties.isActive())

    def test_there_is_one_range_per_band_keyed_one_based(self) -> None:
        ranges = self.properties.fixedRangePerBand()
        self.assertEqual(DAILY_BANDS, len(ranges))
        # 1..3, matching GDAL and QGIS band numbering. A 0-based dict would
        # leave band 3 with no range and show band 1 for two different days.
        self.assertEqual([1, 2, 3], sorted(ranges))

    def test_each_range_matches_its_interval_in_utc(self) -> None:
        ranges = self.properties.fixedRangePerBand()
        for index, (start, end) in enumerate(self.result.intervals, start=1):
            with self.subTest(band=index):
                span = ranges[index]
                # offsetFromUtc 0 proves setTimeZone() ran: without it these
                # are LocalTime and the instant is wrong by the local offset.
                self.assertEqual(0, span.begin().offsetFromUtc())
                self.assertEqual(0, span.end().offsetFromUtc())
                self.assertEqual(
                    start.strftime("%Y-%m-%dT%H:%M:%S"),
                    span.begin().toUTC().toString("yyyy-MM-ddTHH:mm:ss"),
                )
                self.assertEqual(
                    end.strftime("%Y-%m-%dT%H:%M:%S"),
                    span.end().toUTC().toString("yyyy-MM-ddTHH:mm:ss"),
                )

    def test_every_range_is_half_open(self) -> None:
        for index, span in self.properties.fixedRangePerBand().items():
            with self.subTest(band=index):
                self.assertTrue(span.includeBeginning())
                self.assertFalse(span.includeEnd())

    def test_every_range_is_tagged_utc_not_merely_offset_zero(self) -> None:
        # offsetFromUtc() == 0 is NOT enough to prove _qdatetime ran. On a
        # machine whose local zone is UTC -- which most CI is -- a LocalTime
        # QDateTime also answers 0, so deleting setTimeZone() leaves the whole
        # module broken and the offset assertions above still green. Measured
        # by deleting it and re-running: TZ=Asia/Tokyo fails 4 tests, TZ=UTC
        # fails none.
        #
        # The zone tag itself is invariant. Measured on this build:
        #   bare QDateTime(aware dt)  spec=LocalTime  id=b'GMT' under TZ=UTC,
        #                                             b'Asia/Tokyo' under Tokyo
        #   _qdatetime(aware dt)      spec=TimeZone   id=b'UTC' in every zone
        # so this assertion fails on a broken module in a UTC CI too.
        for index, span in self.properties.fixedRangePerBand().items():
            with self.subTest(band=index):
                for stamp in (span.begin(), span.end()):
                    self.assertEqual(Qt.TimeSpec.TimeZone, stamp.timeSpec())
                    self.assertEqual(b"UTC", bytes(stamp.timeZone().id()))

    def test_monthly_gets_twenty_four_ranges(self) -> None:
        result = self.mosaic_monthly()
        layer = build_raster_layer(result.path, "T2M", result.intervals)
        ranges = layer.temporalProperties().fixedRangePerBand()
        self.assertEqual(24, len(ranges))
        self.assertEqual(list(range(1, 25)), sorted(ranges))


class NoFlickerTests(RasterFixtureMixin, QgisTestCase):
    """The half-open interval, demonstrated against a closed one.

    A closed range on band 1 ends at the same instant band 2's begins, so an
    animation frame that lands on midnight matches both. QGIS then picks one
    and redraws -- the flicker this module is written to avoid.
    """

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.result = cls.mosaic_daily()

    def _closed_layer(self) -> QgsRasterLayer:
        """The same raster with closed ranges, built here, not by the module."""
        layer = QgsRasterLayer(str(self.result.path), "closed", "gdal")
        layer.setCrs(POWER_CRS)
        properties = layer.temporalProperties()
        properties.setMode(Qgis.RasterTemporalMode.FixedRangePerBand)
        properties.setFixedRangePerBand(
            {
                index: QgsDateTimeRange(_utc(start), _utc(end))
                for index, (start, end) in enumerate(self.result.intervals, start=1)
            }
        )
        properties.setIsActive(True)
        return layer

    def test_a_boundary_instant_matches_exactly_one_half_open_band(self) -> None:
        layer = build_raster_layer(self.result.path, "T2M", self.result.intervals)
        # Feb 2 00:00 UTC is band 1's end and band 2's start.
        bands = layer.temporalProperties().filteredBandsForTemporalRange(
            layer, _instant(BOUNDARY)
        )
        self.assertEqual([2], list(bands))

    def test_the_same_instant_matches_two_closed_bands(self) -> None:
        layer = self._closed_layer()
        bands = layer.temporalProperties().filteredBandsForTemporalRange(
            layer, _instant(BOUNDARY)
        )
        # Two bands for one instant: measured, and exactly the flicker.
        self.assertEqual([1, 2], sorted(bands))

    def test_every_band_boundary_is_unambiguous(self) -> None:
        layer = build_raster_layer(self.result.path, "T2M", self.result.intervals)
        for index, (start, _end) in enumerate(self.result.intervals, start=1):
            with self.subTest(boundary=start.isoformat()):
                bands = layer.temporalProperties().filteredBandsForTemporalRange(
                    layer, _instant(start)
                )
                self.assertEqual([index], list(bands))

    def test_an_instant_one_microsecond_before_a_boundary_is_the_earlier_band(
        self,
    ) -> None:
        layer = build_raster_layer(self.result.path, "T2M", self.result.intervals)
        bands = layer.temporalProperties().filteredBandsForTemporalRange(
            layer, _instant(BOUNDARY - timedelta(milliseconds=1))
        )
        self.assertEqual([1], list(bands))


class BandForTests(RasterFixtureMixin, QgisTestCase):
    """The band a given instant resolves to, including 'none'."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.result = cls.mosaic_daily()

    def setUp(self) -> None:
        super().setUp()
        self.layer = build_raster_layer(self.result.path, "T2M", self.result.intervals)

    def test_a_mid_band_instant_maps_to_its_own_band(self) -> None:
        for day, expected in ((1, 1), (2, 2), (3, 3)):
            with self.subTest(day=day):
                self.assertEqual(expected, band_for(self.layer, _mid(day)))

    def test_a_boundary_instant_maps_to_the_later_band(self) -> None:
        # Half-open: midnight belongs to the day that starts, not the one that
        # ends.
        self.assertEqual(2, band_for(self.layer, BOUNDARY))

    def test_the_first_and_last_instants_are_inside(self) -> None:
        self.assertEqual(1, band_for(self.layer, self.result.intervals[0][0]))
        self.assertEqual(
            DAILY_BANDS,
            band_for(self.layer, self.result.intervals[-1][1] - timedelta(seconds=1)),
        )

    def test_an_instant_outside_the_series_is_zero(self) -> None:
        # 0, not -1: bands are 1-based, so 0 is the falsy "no band" a caller
        # can test with `if band_for(...)`. QGIS itself answers -1 here, which
        # is truthy and would be passed on as a band number.
        before = datetime(2023, 1, 1, tzinfo=timezone.utc)
        after = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(0, band_for(self.layer, before))
        self.assertEqual(0, band_for(self.layer, after))
        # The end of the last band is exclusive, so it is outside too.
        self.assertEqual(0, band_for(self.layer, self.result.intervals[-1][1]))


class StyleRasterLayerTests(RasterFixtureMixin, QgisTestCase):
    """One pseudocolour ramp, stretched once across the whole cube."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.result = cls.mosaic_daily()

    def setUp(self) -> None:
        super().setUp()
        self.layer = build_raster_layer(self.result.path, "T2M", self.result.intervals)

    @staticmethod
    def _shader(layer: QgsRasterLayer) -> QgsColorRampShader:
        return layer.renderer().shader().rasterShaderFunction()

    def test_a_pseudocolour_renderer_is_installed(self) -> None:
        self.assertTrue(style_raster_layer(self.layer, "T2M", "C"))
        self.assertIsInstance(self.layer.renderer(), QgsSingleBandPseudoColorRenderer)

    def test_the_shader_has_class_count_ramp_items(self) -> None:
        style_raster_layer(self.layer, "T2M", "C")
        self.assertEqual(
            CLASS_COUNT, len(self._shader(self.layer).colorRampItemList())
        )

    def test_continuous_would_have_ignored_the_class_count(self) -> None:
        # Why EqualInterval is set. Measured on this build: Continuous answers
        # 5 items whatever it is asked for -- 7 and 11 both come back as 5.
        for asked in (CLASS_COUNT, 11):
            with self.subTest(asked=asked):
                function = QgsColorRampShader(0.0, 10.0)
                function.setColorRampType(Qgis.ShaderInterpolationMethod.Linear)
                function.setClassificationMode(
                    Qgis.ShaderClassificationMethod.Continuous
                )
                function.setSourceColorRamp(QgsStyle.defaultStyle().colorRamp("RdBu"))
                function.classifyColorRamp(asked, -1)
                self.assertEqual(5, len(function.colorRampItemList()))
        style_raster_layer(self.layer, "T2M", "C")
        self.assertEqual(
            Qgis.ShaderClassificationMethod.EqualInterval,
            self._shader(self.layer).classificationMode(),
        )

    def test_the_stretch_is_the_whole_cube_not_band_one(self) -> None:
        style_raster_layer(self.layer, "T2M", "C")
        renderer = self.layer.renderer()
        cube = band_statistics(self.result.path)
        self.assertAlmostEqual(CUBE_MIN, cube[0], places=5)
        self.assertAlmostEqual(CUBE_MAX, cube[1], places=5)
        self.assertAlmostEqual(cube[0], renderer.classificationMin(), places=5)
        self.assertAlmostEqual(cube[1], renderer.classificationMax(), places=5)
        # The shader carries the same limits, so the legend and the pixels agree.
        shader = self._shader(self.layer)
        self.assertAlmostEqual(cube[0], shader.minimumValue(), places=5)
        self.assertAlmostEqual(cube[1], shader.maximumValue(), places=5)

    def test_band_one_alone_would_have_given_a_different_minimum(self) -> None:
        # The contrast that makes the previous test mean something. Measured:
        # band 1 bottoms out at -2.28 C and band 3 at -5.48 C, so a per-band
        # stretch would repaint 3.2 C of the ramp between frames -- movement a
        # viewer reads as weather.
        dataset = gdal.Open(str(self.result.path))
        values = np.asarray(dataset.GetRasterBand(1).ReadAsArray(), dtype="float64")
        dataset = None
        values = values[np.isfinite(values)]
        band_one_min, band_one_max = float(values.min()), float(values.max())
        self.assertAlmostEqual(BAND1_MIN, band_one_min, places=5)
        self.assertAlmostEqual(BAND1_MAX, band_one_max, places=5)
        style_raster_layer(self.layer, "T2M", "C")
        self.assertLess(self.layer.renderer().classificationMin(), band_one_min - 3.0)
        # The first ramp item is the cube's floor, not band 1's.
        first = self._shader(self.layer).colorRampItemList()[0]
        self.assertAlmostEqual(CUBE_MIN, first.value, places=5)

    def test_temperature_inverts_the_ramp_and_irradiance_does_not(self) -> None:
        style_raster_layer(self.layer, "T2M", "C")
        temperature = self._shader(self.layer).colorRampItemList()

        solar = self.mosaic_solar()
        solar_layer = build_raster_layer(solar.path, "solar", solar.intervals)
        self.assertTrue(
            style_raster_layer(solar_layer, "ALLSKY_SFC_SW_DWN", "kW-hr/m^2/day")
        )
        irradiance = self._shader(solar_layer).colorRampItemList()

        rd_bu = QgsStyle.defaultStyle().colorRamp("RdBu")
        inferno = QgsStyle.defaultStyle().colorRamp("Inferno")
        # RdBu runs red -> blue, so the coldest value would come out red.
        # Inverted, the floor is #0571b0 and the ceiling #ca0020: warm is warm.
        self.assertEqual("#ca0020", rd_bu.color(0.0).name())
        self.assertEqual("#0571b0", temperature[0].color.name())
        self.assertEqual("#ca0020", temperature[-1].color.name())
        # Inferno is already dark -> bright and is used as it comes.
        self.assertEqual(inferno.color(0.0).name(), irradiance[0].color.name())
        self.assertEqual("#000004", irradiance[0].color.name())

    def test_inverting_does_not_corrupt_the_shared_style(self) -> None:
        # colorRamp() hands back a clone; if it ever handed back the ramp
        # itself, styling one temperature layer would flip RdBu for every
        # other layer in the session.
        style_raster_layer(self.layer, "T2M", "C")
        self.assertEqual(
            "#ca0020", QgsStyle.defaultStyle().colorRamp("RdBu").color(0.0).name()
        )

    def test_an_all_nodata_raster_is_declined_and_left_alone(self) -> None:
        # A legal POWER response: an ocean-only box for a land parameter.
        path = self.write_raster("allnan.tif", [np.full((3, 3), np.nan)])
        self.assertIsNone(band_statistics(path))
        # GDAL logs "Failed to compute statistics, no valid pixels found" to
        # stderr when the provider builds its default renderer here. That is
        # the correct answer for this raster, not a fault, so it is silenced
        # rather than left to look like a suite failure.
        with gdal.quiet_errors():
            layer = QgsRasterLayer(str(path), "nodata", "gdal")
            self.assertTrue(layer.isValid())
        before = layer.renderer()
        self.assertIsInstance(before, QgsSingleBandGrayRenderer)
        self.assertFalse(style_raster_layer(layer, "T2M", "C"))
        # Untouched, not half-configured against an empty range.
        self.assertIs(before, layer.renderer())
        self.assertNotIsInstance(layer.renderer(), QgsSingleBandPseudoColorRenderer)

    def test_a_flat_raster_still_gets_a_non_degenerate_range(self) -> None:
        # Every pixel 5.0, which POWER does return -- a fill-only box, or a
        # fraction that is 0 everywhere. Measured on this build: a degenerate
        # shader is not merely blank, it is fatal.
        # QgsColorRampShader(5.0, 5.0).classifyColorRamp(7, -1) kills the
        # interpreter with SIGBUS (exit 138), so the pad is a crash guard.
        path = self.write_raster("flat.tif", [np.full((3, 3), 5.0)])
        self.assertEqual((5.0, 5.0), band_statistics(path))
        layer = QgsRasterLayer(str(path), "flat", "gdal")
        self.assertTrue(style_raster_layer(layer, "T2M", "C"))
        renderer = layer.renderer()
        self.assertLess(renderer.classificationMin(), renderer.classificationMax())
        # 1% of the value on each side.
        self.assertAlmostEqual(4.95, renderer.classificationMin(), places=6)
        self.assertAlmostEqual(5.05, renderer.classificationMax(), places=6)
        self.assertEqual(CLASS_COUNT, len(self._shader(layer).colorRampItemList()))

    def test_a_flat_zero_raster_pads_by_one(self) -> None:
        # abs(0) * 0.01 is 0, so the proportional pad has to fall back to 1.0
        # or an all-zero field still classifies into a single bucket.
        path = self.write_raster("zero.tif", [np.zeros((3, 3))])
        layer = QgsRasterLayer(str(path), "zero", "gdal")
        self.assertTrue(style_raster_layer(layer, "T2M", "C"))
        self.assertAlmostEqual(-1.0, layer.renderer().classificationMin(), places=6)
        self.assertAlmostEqual(1.0, layer.renderer().classificationMax(), places=6)

    def test_explicit_limits_override_the_computed_range(self) -> None:
        cube = band_statistics(self.result.path)
        self.assertTrue(
            style_raster_layer(self.layer, "T2M", "C", limits=(-40.0, 40.0))
        )
        renderer = self.layer.renderer()
        self.assertEqual(-40.0, renderer.classificationMin())
        self.assertEqual(40.0, renderer.classificationMax())
        # A shared scale across several fetches is the point: the numbers are
        # the caller's, not the cube's.
        self.assertNotAlmostEqual(cube[0], renderer.classificationMin(), places=3)
        self.assertNotAlmostEqual(cube[1], renderer.classificationMax(), places=3)
        self.assertAlmostEqual(-40.0, self._shader(self.layer).minimumValue(), places=6)
        self.assertAlmostEqual(40.0, self._shader(self.layer).maximumValue(), places=6)

    def test_degenerate_explicit_limits_are_padded_too(self) -> None:
        # The pad sits after the limits branch, so a caller pinning a shared
        # scale at a single value gets the same protection the computed path
        # gets. It has to: a min == max shader takes the process down with it.
        self.assertTrue(style_raster_layer(self.layer, "T2M", "C", limits=(7.0, 7.0)))
        renderer = self.layer.renderer()
        self.assertAlmostEqual(6.93, renderer.classificationMin(), places=6)
        self.assertAlmostEqual(7.07, renderer.classificationMax(), places=6)
        self.assertEqual(CLASS_COUNT, len(self._shader(self.layer).colorRampItemList()))
