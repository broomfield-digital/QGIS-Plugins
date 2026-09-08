"""Guard the long-form point layer: its shape, its clock, and its animation.

Four things here are worth more than the rest:

* **The half-open frame.** ``[t_start, t_end)`` is the whole reason the layer
  carries two datetime fields instead of one instant. The proof is a
  side-by-side: the half-open controller range selects one day, the closed one
  selects two. Only the second assertion shows the interval is doing work --
  a layer with a broken interval still passes the first.
* **Timestamps are UTC-explicit.** PyQt6 drops ``tzinfo`` when it converts a
  ``datetime``: ``QDateTime(datetime(2024,2,1,tzinfo=utc))`` comes back
  ``TimeSpec.LocalTime`` with ``offsetFromUtc() == -25200`` on this machine, so
  the same wall clock is a different instant. That is the LST trap arriving by
  a second route, and it is silent.
* **Fill is NULL, not zero.** POWER's -999.0 masked to ``None`` has to reach
  the attribute table as NULL and be dropped from the chart; a zero would read
  as darkness at noon.
* **A canvas click is not degrees.** In a Web Mercator project it is metres in
  the millions, which POWER would either reject or answer for the wrong place.
"""

from __future__ import annotations

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsDateTimeRange,
    QgsFeatureRequest,
    QgsFeatureSource,
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
    QgsVectorLayerTemporalContext,
)
from qgis.PyQt.QtCore import QDate, QDateTime, Qt, QTime, QTimeZone

from nasa_power.core.decode import parse_point_response
from nasa_power.qgis_bridge.layers_point import (
    FIELDS,
    build_point_layer,
    merge_into,
    parameters_in,
    series,
    sites_in,
    to_wgs84,
)
from tests.qgis.qgis_case import (
    BOULDER,
    QgisTestCase,
    load_bytes,
    load_json,
    observation,
)

#: The two parameters ``point_daily_2param.json`` was fetched for. Kept
#: separate from the response so the silent-omission case would show up as a
#: count mismatch rather than as agreement with itself.
REQUESTED = ("T2M", "ALLSKY_SFC_SW_DWN")

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
WEB_MERCATOR = QgsCoordinateReferenceSystem("EPSG:3857")

def utc(year: int, month: int, day: int, hour: int = 0) -> QDateTime:
    """A timezone-explicit ``QDateTime``, for building controller ranges."""
    stamp = QDateTime(QDate(year, month, day), QTime(hour, 0))
    stamp.setTimeZone(QTimeZone.utc())
    return stamp


class PointLayerShapeTests(QgisTestCase):
    """The layer a real response produces."""

    def setUp(self) -> None:
        super().setUp()
        self.observations, self.facts = parse_point_response(
            load_bytes("point_daily_2param.json"),
            temporal="daily",
            requested=REQUESTED,
            site="Boulder",
        )
        self.layer = build_point_layer(
            self.observations,
            "T2M daily",
            time_standard=self.facts.time_standard,
            sources=self.facts.sources,
        )

    def test_layer_is_valid(self) -> None:
        self.assertTrue(self.layer.isValid())

    def test_the_provider_builds_a_spatial_index(self) -> None:
        # index=yes in the provider URI. The temporal controller re-filters the
        # layer on every frame, so without an index each frame of a year of
        # hourly data is a full scan of 8760 features per site. Measured on
        # this build: the same URI without index=yes reports NotPresent (1),
        # with it Present (2) -- so this is observable, not a hope.
        self.assertEqual(
            QgsFeatureSource.SpatialIndexPresence.Present,
            self.layer.dataProvider().hasSpatialIndex(),
        )

    def test_one_feature_per_parameter_per_timestep(self) -> None:
        # 2 parameters x 3 days. The long form repeats the geometry; that
        # repetition is what the temporal controller animates.
        self.assertEqual(6, len(self.observations))
        self.assertEqual(6, self.layer.featureCount())

    def test_all_declared_fields_present_and_in_order(self) -> None:
        # Order is the attribute-table column order, so it is part of the
        # contract, not an accident of the dict.
        self.assertEqual(17, len(FIELDS))
        expected = [name for name, _kind, _length in FIELDS]
        self.assertEqual(expected, [f.name() for f in self.layer.fields()])

    def test_provenance_columns_carry_the_response_facts(self) -> None:
        feature = next(self.layer.getFeatures())
        self.assertEqual("UTC", feature["time_standard"])
        # header.sources for this request is ['MERRA2', 'SYN1DEG'] -- two
        # parents, attributable to neither, which is why the planner splits.
        self.assertEqual("MERRA2,SYN1DEG", feature["source"])

    def test_geometry_is_the_requested_coordinate_in_degrees(self) -> None:
        feature = next(self.layer.getFeatures())
        point = feature.geometry().asPoint()
        self.assertAlmostEqual(BOULDER[0], point.x(), places=6)
        self.assertAlmostEqual(BOULDER[1], point.y(), places=6)
        self.assertEqual(WGS84.authid(), self.layer.crs().authid())


class TemporalPropertiesTests(QgisTestCase):
    """The half-open interval, which is the point of the two datetime fields."""

    def setUp(self) -> None:
        super().setUp()
        observations, _facts = parse_point_response(
            load_bytes("point_daily_2param.json"),
            temporal="daily",
            requested=REQUESTED,
            site="Boulder",
        )
        self.layer = build_point_layer(observations, "T2M daily")
        self.properties = self.layer.temporalProperties()

    def _count(self, frame: QgsDateTimeRange) -> int:
        """Features the controller would draw for ``frame``."""
        expression = self.properties.createFilterString(
            QgsVectorLayerTemporalContext(), frame
        )
        request = QgsFeatureRequest().setFilterExpression(expression)
        return len(list(self.layer.getFeatures(request)))

    def test_mode_is_start_and_end_from_fields(self) -> None:
        # Not InstantFromField: instant mode plus a fixed duration was measured
        # to widen the window and select a day either side of the frame.
        self.assertEqual(
            Qgis.VectorTemporalMode.FeatureDateTimeStartAndEndFromFields,
            self.properties.mode(),
        )
        self.assertEqual("t_start", self.properties.startField())
        self.assertEqual("t_end", self.properties.endField())

    def test_temporal_properties_are_active(self) -> None:
        # Inactive properties are configured and ignored: the slider moves and
        # nothing on the map changes.
        self.assertTrue(self.properties.isActive())

    def test_half_open_frame_selects_exactly_one_day(self) -> None:
        # Measured: the half-open [Feb 2, Feb 3) frame yields
        #   ("t_start" < make_datetime(2024,2,3,0,0,0) OR "t_start" IS NULL)
        #   AND ("t_end" > make_datetime(2024,2,2,0,0,0) OR "t_end" IS NULL)
        # which matches Feb 2 for both parameters and nothing else.
        frame = QgsDateTimeRange(utc(2024, 2, 2), utc(2024, 2, 3), True, False)
        self.assertEqual(2, self._count(frame))

    def test_closed_frame_over_selects_the_next_day(self) -> None:
        # The control. A CLOSED [Feb 2, Feb 3] frame relaxes the first clause to
        # "t_start" <= Feb 3, so Feb 3's features match as well: 4, not 2.
        # This is the flicker the half-open frame exists to remove, and it is
        # the assertion that proves the interval is load-bearing -- a layer
        # whose end stamps were wrong would still pass the test above.
        frame = QgsDateTimeRange(utc(2024, 2, 2), utc(2024, 2, 3))
        self.assertEqual(4, self._count(frame))

    def test_a_frame_inside_one_day_still_selects_that_day(self) -> None:
        # Daily features are 24 h wide, so a sub-day frame must not fall
        # between two of them and draw an empty map.
        frame = QgsDateTimeRange(
            utc(2024, 2, 2, 6), utc(2024, 2, 2, 7), True, False
        )
        self.assertEqual(2, self._count(frame))


class HourlyAndMonthlyLayerTests(QgisTestCase):
    """The two temporals where the layer shape is not the daily one.

    Daily is the easy case: 24-hour features, day-aligned frames. Hourly is
    where the half-open interval actually earns its keep -- adjacent features
    share a boundary every hour instead of every day, so a closed frame
    double-counts 24 times a day rather than once. Monthly is where a feature
    can be something POWER did not measure at all.
    """

    def _layer(self, fixture: str, *, temporal: str, requested: tuple[str, ...]):
        observations, facts = parse_point_response(
            load_bytes(fixture), temporal=temporal, requested=requested, site="boulder"
        )
        return build_point_layer(
            observations, fixture, time_standard=facts.time_standard
        ), observations

    def _count(self, layer: QgsVectorLayer, frame: QgsDateTimeRange) -> int:
        expression = layer.temporalProperties().createFilterString(
            QgsVectorLayerTemporalContext(), frame
        )
        request = QgsFeatureRequest().setFilterExpression(expression)
        return len(list(layer.getFeatures(request)))

    def test_an_hourly_layer_has_one_feature_per_hour(self) -> None:
        layer, observations = self._layer(
            "point_hourly_utc.json", temporal="hourly", requested=("ALLSKY_SFC_SW_DWN",)
        )
        self.assertEqual(24, len(observations))
        self.assertEqual(24, layer.featureCount())

    def test_an_hourly_half_open_frame_selects_exactly_one_hour(self) -> None:
        # Measured: the filter is
        #   ("t_start" < make_datetime(2024,6,1,7,0,0) OR "t_start" IS NULL)
        #   AND ("t_end" > make_datetime(2024,6,1,6,0,0) OR "t_end" IS NULL)
        # -> 1 feature half-open, 2 closed. This is the flicker at the
        # resolution where it bites hardest: with closed frames an hourly
        # animation double-draws on every one of the day's 24 boundaries.
        layer, _ = self._layer(
            "point_hourly_utc.json", temporal="hourly", requested=("ALLSKY_SFC_SW_DWN",)
        )
        half_open = QgsDateTimeRange(utc(2024, 6, 1, 6), utc(2024, 6, 1, 7), True, False)
        closed = QgsDateTimeRange(utc(2024, 6, 1, 6), utc(2024, 6, 1, 7))
        self.assertEqual(1, self._count(layer, half_open))
        self.assertEqual(2, self._count(layer, closed))

    def test_the_lst_layer_records_the_standard_it_was_served_in(self) -> None:
        # POWER defaults to LST, and the wall clocks in an LST response are
        # NOT UTC -- the identical 2024-06-01 peak sits at hour 17 under UTC
        # and hour 10 under LST. Ring 1 deliberately does not shift them; the
        # layer stays honest by carrying the standard as a column, so a reader
        # pairing this against a UTC model can see the 7-hour gap.
        lst, _ = self._layer(
            "point_hourly_lst.json", temporal="hourly", requested=("ALLSKY_SFC_SW_DWN",)
        )
        utc_layer, _ = self._layer(
            "point_hourly_utc.json", temporal="hourly", requested=("ALLSKY_SFC_SW_DWN",)
        )
        self.assertEqual("LST", next(lst.getFeatures())["time_standard"])
        self.assertEqual("UTC", next(utc_layer.getFeatures())["time_standard"])

    def test_a_monthly_layer_drops_the_yyyy13_annual_means(self) -> None:
        # point_monthly_yyyy13.json carries 26 keys for 2020-2021: 24 months
        # plus 202013 and 202113, which are that year's annual MEAN and not a
        # thirteenth month. A feature for one would animate an average as if it
        # were a timestep, in December's slot.
        raw = load_json("point_monthly_yyyy13.json")
        self.assertEqual(26, len(raw["properties"]["parameter"]["T2M"]))

        layer, observations = self._layer(
            "point_monthly_yyyy13.json", temporal="monthly", requested=("T2M",)
        )
        self.assertEqual(24, len(observations))
        self.assertEqual(24, layer.featureCount())
        # And no two features share a timestep, which is what a duplicated
        # December would look like on the slider.
        stamps = [f["t_start"].toSecsSinceEpoch() for f in layer.getFeatures()]
        self.assertEqual(24, len(set(stamps)))


class UtcTimestampTests(QgisTestCase):
    """Timestamps must reach the layer with their timezone attached.

    PyQt6 converts a ``datetime`` by copying the wall clock and dropping
    ``tzinfo``: the result is ``TimeSpec.LocalTime``. Measured on this machine
    (America/Denver), an unfixed stamp reports ``offsetFromUtc() == -25200``
    and an epoch 25200 s later than the ``datetime`` it came from, so every
    value would sit seven hours from where POWER put it.
    """

    def setUp(self) -> None:
        super().setUp()
        self.observations, _facts = parse_point_response(
            load_bytes("point_daily_2param.json"),
            temporal="daily",
            requested=REQUESTED,
            site="Boulder",
        )
        self.layer = build_point_layer(self.observations, "T2M daily")

    def test_stored_timestamps_declare_utc(self) -> None:
        for feature in self.layer.getFeatures():
            for field in ("t_start", "t_end"):
                stamp = feature[field]
                self.assertNotEqual(Qt.TimeSpec.LocalTime, stamp.timeSpec())
                self.assertEqual(0, stamp.offsetFromUtc())
                self.assertEqual(b"UTC", bytes(stamp.timeZone().id()))

    def test_stored_timestamps_are_the_same_instant_as_the_source(self) -> None:
        # The assertion that still bites on a machine whose local time IS UTC:
        # compare instants, not wall-clock components. Under the unfixed
        # conversion every epoch here is 25200 s late.
        expected = sorted(
            (o.parameter, int(o.t_start.timestamp()), int(o.t_end.timestamp()))
            for o in self.observations
        )
        actual = sorted(
            (
                feature["parameter"],
                feature["t_start"].toSecsSinceEpoch(),
                feature["t_end"].toSecsSinceEpoch(),
            )
            for feature in self.layer.getFeatures()
        )
        self.assertEqual(expected, actual)


class FillAndSeriesTests(QgisTestCase):
    """Fill is an absence. It must not become a zero anywhere."""

    def test_fill_values_land_as_null_attributes(self) -> None:
        layer = build_point_layer(
            [observation(1, None), observation(2, 281.0), observation(3, None)],
            "with fill",
        )
        values = [f["value"] for f in layer.getFeatures()]
        self.assertEqual([None, 281.0, None], values)

        # And NULL to the expression engine too, not merely None to Python:
        # the renderer and the attribute table both go through the engine.
        request = QgsFeatureRequest().setFilterExpression('"value" IS NULL')
        self.assertEqual(2, len(list(layer.getFeatures(request))))

    def test_series_drops_nulls_rather_than_plotting_zero(self) -> None:
        layer = build_point_layer(
            [observation(1, None), observation(2, 281.0), observation(3, None)],
            "with fill",
        )
        points = series(layer, "T2M", "Boulder")
        self.assertEqual(1, len(points))
        self.assertEqual(281.0, points[0][1])
        self.assertNotIn(0.0, [value for _stamp, value in points])

    def test_series_is_time_ordered_whatever_order_it_was_built_in(self) -> None:
        shuffled = [observation(3, 3.0), observation(1, 1.0), observation(2, 2.0)]
        layer = build_point_layer(shuffled, "shuffled")
        points = series(layer, "T2M", "Boulder")
        self.assertEqual([1.0, 2.0, 3.0], [value for _stamp, value in points])
        stamps = [stamp for stamp, _value in points]
        self.assertEqual(sorted(stamps), stamps)

    def test_a_site_label_with_an_apostrophe_still_returns_its_series(self) -> None:
        # Site labels are not ours: gui/panels.py takes them from whatever
        # field a user's own point layer calls name/site/station, so O'Hare and
        # Coeur d'Alene are ordinary station names. An apostrophe interpolated
        # into a filter closes the string literal early, and QGIS answers an
        # invalid expression with zero features and NO error -- the chart just
        # comes up blank. Measured before the fix: 0 points for both names.
        for site in ("O'Hare", "Coeur d'Alene", "St John's"):
            with self.subTest(site=site):
                layer = build_point_layer(
                    [observation(1, 1.0, site=site), observation(2, 2.0, site=site)],
                    "apostrophe",
                )
                self.assertEqual(
                    [1.0, 2.0], [value for _stamp, value in series(layer, "T2M", site)]
                )

    def test_a_site_label_cannot_widen_the_selection(self) -> None:
        # The same defect from the other side, and the worse half: a label that
        # closes the literal and opens an always-true clause plotted BOTH sites'
        # values under a name matching neither. Measured before the fix: 2
        # points returned for a site that does not exist.
        layer = build_point_layer(
            [observation(1, 1.0, site="A"), observation(2, 2.0, site="B")], "two sites"
        )
        self.assertEqual([], series(layer, "T2M", "A' OR '1'='1"))

    def test_a_parameter_code_is_quoted_too(self) -> None:
        # Parameter codes come from POWER's dictionary rather than from a user
        # layer, so this is the cheaper half -- but it is the same expression.
        layer = build_point_layer([observation(1, 1.0, site="A")], "one")
        self.assertEqual([], series(layer, "T2M' OR '1'='1", "A"))

    def test_series_selects_one_parameter_at_one_site(self) -> None:
        layer = build_point_layer(
            [
                observation(1, 1.0, parameter="T2M", site="Boulder"),
                observation(1, 2.0, parameter="RH2M", site="Boulder"),
                observation(1, 3.0, parameter="T2M", site="Denver"),
            ],
            "mixed",
        )
        self.assertEqual([1.0], [v for _s, v in series(layer, "T2M", "Boulder")])
        self.assertEqual([3.0], [v for _s, v in series(layer, "T2M", "Denver")])


class LayerContentsTests(QgisTestCase):
    """What a layer says is in it, and appending to one already on the map."""

    def _multi(self):
        return [
            observation(1, 1.0, parameter="T2M", site="Boulder"),
            observation(2, 2.0, parameter="T2M", site="Boulder"),
            observation(1, 3.0, parameter="RH2M", site="Boulder"),
            observation(
                1, 4.0, parameter="T2M", site="Denver", longitude=-104.99, latitude=39.74
            ),
        ]

    def test_parameters_in_and_sites_in_are_distinct(self) -> None:
        layer = build_point_layer(self._multi(), "multi")
        self.assertEqual(["RH2M", "T2M"], parameters_in(layer))
        self.assertEqual(["Boulder", "Denver"], sites_in(layer))

    def test_helpers_are_empty_on_a_layer_without_the_fields(self) -> None:
        # A user's own point layer, handed to the dock as a site source, has
        # none of these columns. Measured: uniqueValues(-1) returns an empty
        # set rather than raising, so the index guards inside the helpers are
        # belt-and-braces and this stays green without them -- the returned []
        # is what is pinned.
        bare = QgsVectorLayer("Point?crs=EPSG:4326", "bare", "memory")
        self.assertTrue(bare.isValid())
        self.assertEqual([], parameters_in(bare))
        self.assertEqual([], sites_in(bare))

    def test_merge_into_appends_and_leaves_one_layer(self) -> None:
        layer = build_point_layer(self._multi(), "multi")
        before = layer.featureCount()
        # Read the extent BEFORE merging, which is what a layer already on the
        # map has done. Measured: updateExtents() only marks the cached extent
        # dirty, so a layer whose extent has never been read recomputes it on
        # first access and looks correct either way -- the stale-cache bug is
        # invisible unless the cache was populated first.
        self.assertAlmostEqual(39.74, layer.extent().yMinimum(), places=6)
        # And Lamar sits OUTSIDE that extent in both axes -- Boulder and Denver
        # span lon [-105.27, -104.99], lat [39.74, 40.02]. A site inside the box
        # would leave the extent unchanged whether updateExtents() ran or not.
        added = merge_into(
            layer,
            [
                observation(
                    3, 5.0, parameter="PS", site="Lamar", longitude=-102.62, latitude=38.09
                )
            ],
        )
        self.assertEqual(1, added)
        self.assertEqual(before + 1, layer.featureCount())
        self.assertEqual(["PS", "RH2M", "T2M"], parameters_in(layer))
        self.assertEqual(["Boulder", "Denver", "Lamar"], sites_in(layer))
        # Or the new site sits outside the layer's own extent and
        # zoom-to-layer cuts it off.
        self.assertAlmostEqual(38.09, layer.extent().yMinimum(), places=6)
        self.assertAlmostEqual(-102.62, layer.extent().xMaximum(), places=6)


class ReprojectionTests(QgisTestCase):
    """A canvas click is in the project's CRS, which is usually not degrees."""

    def test_web_mercator_click_round_trips_to_degrees(self) -> None:
        forward = QgsCoordinateTransform(WGS84, WEB_MERCATOR, QgsProject.instance())
        clicked = forward.transform(QgsPointXY(*BOULDER))

        # Measured: Boulder is (-11718602.80, 4868849.05) in EPSG:3857. Handed
        # to POWER unconverted this is not a coordinate at all.
        self.assertGreater(abs(clicked.x()), 1_000_000)
        self.assertGreater(abs(clicked.y()), 1_000_000)

        back = to_wgs84(clicked, WEB_MERCATOR)
        self.assertAlmostEqual(BOULDER[0], back.x(), delta=1e-6)
        self.assertAlmostEqual(BOULDER[1], back.y(), delta=1e-6)
        self.assertTrue(-180.0 <= back.x() <= 180.0)
        self.assertTrue(-90.0 <= back.y() <= 90.0)

    def test_already_wgs84_is_passed_through_untouched(self) -> None:
        # assertIs, not assertEqual on the components: a WGS84 -> WGS84
        # transform is numerically the identity too, so comparing coordinates
        # cannot tell the short-circuit from a pointless round trip through
        # PROJ. Object identity can -- transform() always returns a new
        # QgsPointXY. Mutation-checked: deleting the short-circuit fails this.
        point = QgsPointXY(*BOULDER)
        result = to_wgs84(point, WGS84)
        self.assertIs(point, result)
        self.assertEqual(point.x(), result.x())
        self.assertEqual(point.y(), result.y())
