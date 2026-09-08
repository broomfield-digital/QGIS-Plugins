"""What QA refuses, what it merely warns about, and what it stays quiet on.

Every assertion here is on a finding's ``code`` and ``level``. The messages are
prose written for someone who did not read the module and will be reworded; the
codes are the contract the layer metadata, the dock panel and the Processing
feedback all agree on, so those are what is pinned.

Two severity choices are asserted explicitly because they look like tidiness
mistakes and are not:

* ``RECORD_START`` is a WARNING. POWER answers 200 to a pre-1984 radiation
  request when a meteorology parameter rides along (see
  ``point_1983_partial.json``), so blocking would refuse a request the API
  honours.
* ``MIXED_PROVENANCE`` is an ERROR rather than a warning: a series that is half
  GEWEX SRB and half CERES SYN1deg looks completely normal on a chart.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from nasa_power.core.api import PowerRequest
from nasa_power.core.decode import Observation, ResponseFacts, parse_point_response
from nasa_power.core.provenance import Family
from nasa_power.core.qa import (
    FEATURE_COUNT_LIMIT,
    Level,
    QaFinding,
    QaReport,
    check_valid_range,
    estimate_feature_count,
    postfetch,
    preflight,
)
from tests.unit.support import load_bytes, load_json

#: Boulder, the coordinate every point fixture was captured at.
LAT, LON = 40.02, -105.27

#: A bounding box POWER accepts: 2 degrees on both axes, the measured minimum.
LEGAL_BBOX = {"lat_min": 40.0, "lat_max": 42.0, "lon_min": -106.0, "lon_max": -104.0}


def _finding(report: QaReport, code: str) -> QaFinding:
    for finding in report.findings:
        if finding.code == code:
            return finding
    raise AssertionError(f"{code} not in {report.codes()}")


class PreflightBlockingTests(unittest.TestCase):
    """Asks the API would reject, refused locally instead."""

    def test_hourly_regional_is_refused(self) -> None:
        # Measured: this combination answers a 19 KB text/html 404, not a JSON
        # API error (tests/fixtures/error_404_hourly_regional.html), so nothing
        # downstream can explain the failure. It has to be caught here.
        report = preflight(
            temporal="hourly",
            mode="regional",
            parameters=["T2M"],
            start=date(2024, 2, 1),
            end=date(2024, 2, 1),
            bbox=LEGAL_BBOX,
        )
        self.assertIn("HOURLY_REGIONAL", report.codes())
        self.assertTrue(report.is_blocked)

    def test_an_exactly_two_degree_extent_is_not_refused_for_a_rounding_error(self):
        # The dock hands preflight a bbox built by subtraction, and a genuine
        # 2.0-degree extent does not always subtract to 2.0: 0.01..2.01 gives
        # 1.9999999999999998. Refusing it would block a legal fetch with
        # "needs at least 2 deg" while showing the span as 2. preflight must use
        # the same predicate the planner does, or the badge and the fetch
        # disagree about what is legal.
        for lat_min in (0.002, 0.01, 30.002):
            with self.subTest(lat_min=lat_min):
                self.assertLess((lat_min + 2.0) - lat_min, 2.0, "origin no longer drifts")
                report = preflight(
                    temporal="daily",
                    mode="regional",
                    parameters=["T2M"],
                    start=date(2024, 2, 1),
                    end=date(2024, 2, 1),
                    bbox={
                        "lat_min": lat_min,
                        "lat_max": lat_min + 2.0,
                        "lon_min": 0.0,
                        "lon_max": 3.0,
                    },
                )
                self.assertNotIn("BBOX_SPAN", report.codes())
                self.assertFalse(report.is_blocked)

    def test_one_degree_bbox_is_refused(self) -> None:
        # Measured 422: "Please provide at least a 2 degree range in latitude;
        # otherwise use the point endpoint" (error_422_bbox.json). The binding
        # regional constraint is a MINIMUM, which is the surprising direction.
        report = preflight(
            temporal="daily",
            mode="regional",
            parameters=["T2M"],
            start=date(2024, 2, 1),
            end=date(2024, 2, 1),
            bbox={"lat_min": 40.0, "lat_max": 41.0, "lon_min": -106.0, "lon_max": -104.0},
        )
        self.assertIn("BBOX_SPAN", report.codes())
        self.assertTrue(report.is_blocked)

    def test_two_regional_parameters_are_refused(self) -> None:
        # The regional endpoint serves exactly one parameter per request.
        report = preflight(
            temporal="daily",
            mode="regional",
            parameters=["T2M", "PS"],
            start=date(2024, 2, 1),
            end=date(2024, 2, 1),
            bbox=LEGAL_BBOX,
        )
        self.assertIn("PARAM_COUNT", report.codes())
        self.assertTrue(report.is_blocked)
        self.assertEqual(("T2M", "PS"), _finding(report, "PARAM_COUNT").affected)

    def test_daily_aggregate_at_hourly_is_refused(self) -> None:
        # Measured: an hourly request for T2M_MAX is a 422 ("One of your
        # parameters is incorrect"). T2M_MAX is a daily aggregate.
        report = preflight(
            temporal="hourly",
            mode="point",
            parameters=["T2M_MAX"],
            start=date(2024, 2, 1),
            end=date(2024, 2, 1),
            sites=[{"name": "boulder"}],
        )
        self.assertIn("PARAM_NOT_AT_TEMPORAL", report.codes())
        self.assertTrue(report.is_blocked)
        self.assertEqual(("T2M_MAX",), _finding(report, "PARAM_NOT_AT_TEMPORAL").affected)

    def test_end_before_start_is_refused(self) -> None:
        report = preflight(
            temporal="daily",
            mode="point",
            parameters=["T2M"],
            start=date(2024, 2, 3),
            end=date(2024, 2, 1),
            sites=[{"name": "boulder"}],
        )
        self.assertIn("DATE_ORDER", report.codes())
        self.assertTrue(report.is_blocked)

    def test_no_parameters_is_refused(self) -> None:
        report = preflight(
            temporal="daily",
            mode="point",
            parameters=[],
            start=date(2024, 2, 1),
            end=date(2024, 2, 1),
            sites=[{"name": "boulder"}],
        )
        self.assertEqual(["NO_PARAMETERS"], report.codes())
        self.assertTrue(report.is_blocked)

    def test_fetchable_ask_is_not_blocked(self) -> None:
        report = preflight(
            temporal="daily",
            mode="point",
            parameters=["T2M"],
            start=date(2024, 2, 1),
            end=date(2024, 2, 3),
            sites=[{"name": "boulder"}],
        )
        self.assertEqual([], report.codes())
        self.assertFalse(report.is_blocked)


class PreflightNonBlockingTests(unittest.TestCase):
    """Costly or surprising asks the plugin still sends."""

    def test_twenty_five_point_parameters_are_chunked_not_refused(self) -> None:
        # Point requests cap at 20 parameters; the planner chunks rather than
        # refusing, so this is INFO.
        parameters = [f"T2M_{i:03d}" for i in range(25)]
        report = preflight(
            temporal="daily",
            mode="point",
            parameters=parameters,
            start=date(2024, 2, 1),
            end=date(2024, 2, 3),
            sites=[{"name": "boulder"}],
        )
        self.assertIn("PARAM_CHUNKED", report.codes())
        self.assertFalse(report.is_blocked)
        self.assertIs(Level.INFO, _finding(report, "PARAM_CHUNKED").level)

    def test_conus_bbox_is_tiled_not_refused(self) -> None:
        report = preflight(
            temporal="daily",
            mode="regional",
            parameters=["T2M"],
            start=date(2024, 2, 1),
            end=date(2024, 2, 1),
            bbox={"lat_min": 25.0, "lat_max": 49.0, "lon_min": -125.0, "lon_max": -66.0},
        )
        self.assertIn("TILED", report.codes())
        self.assertFalse(report.is_blocked)
        self.assertIs(Level.INFO, _finding(report, "TILED").level)

    def test_lst_is_warned_about(self) -> None:
        # Measured at Boulder: the same hourly irradiance peak of 911.15 sits at
        # ...17 in UTC and ...10 in LST, a seven-hour phase error.
        report = preflight(
            temporal="hourly",
            mode="point",
            parameters=["ALLSKY_SFC_SW_DWN"],
            start=date(2024, 6, 1),
            end=date(2024, 6, 1),
            sites=[{"name": "boulder"}],
            time_standard="LST",
        )
        self.assertIn("LST_REQUESTED", report.codes())
        self.assertFalse(report.is_blocked)
        self.assertIs(Level.WARNING, _finding(report, "LST_REQUESTED").level)

    def test_large_feature_count_is_warned_about(self) -> None:
        report = preflight(
            temporal="hourly",
            mode="point",
            parameters=["T2M", "PS"],
            start=date(2023, 1, 1),
            end=date(2023, 12, 31),
            sites=[{"name": n} for n in "abcd"],
        )
        self.assertIn("FEATURE_COUNT_LIMIT", report.codes())
        self.assertFalse(report.is_blocked)
        self.assertIs(Level.WARNING, _finding(report, "FEATURE_COUNT_LIMIT").level)


class PreflightProvenanceTests(unittest.TestCase):
    """The 2001 solar seam and the 1984 radiation record start."""

    def test_solar_window_across_2001_is_an_error(self) -> None:
        # Bisected against the live API: 2000-12-31 returns ['SRB'] and
        # 2001-01-01 returns ['SYN1DEG'] (point_solar_2000/2001.json).
        report = preflight(
            temporal="daily",
            mode="point",
            parameters=["ALLSKY_SFC_SW_DWN"],
            start=date(1998, 1, 1),
            end=date(2003, 12, 31),
            sites=[{"name": "boulder"}],
        )
        self.assertIn("MIXED_PROVENANCE", report.codes())
        self.assertIs(Level.ERROR, _finding(report, "MIXED_PROVENANCE").level)
        self.assertFalse(report.is_blocked)

    def test_solar_window_after_2001_is_clean(self) -> None:
        report = preflight(
            temporal="daily",
            mode="point",
            parameters=["ALLSKY_SFC_SW_DWN"],
            start=date(2010, 1, 1),
            end=date(2011, 12, 31),
            sites=[{"name": "boulder"}],
        )
        self.assertNotIn("MIXED_PROVENANCE", report.codes())

    def test_meteorology_never_crosses_the_solar_seam(self) -> None:
        # MERRA-2 has no equivalent of the SRB/CERES change, so no window
        # length should raise the seam for a meteorology-only ask.
        for start, end in (
            (date(1998, 1, 1), date(2003, 12, 31)),
            (date(1981, 1, 1), date(2024, 12, 31)),
        ):
            with self.subTest(start=start, end=end):
                report = preflight(
                    temporal="daily",
                    mode="point",
                    parameters=["T2M", "PS", "WS10M"],
                    start=start,
                    end=end,
                    sites=[{"name": "boulder"}],
                )
                self.assertNotIn("MIXED_PROVENANCE", report.codes())

    def test_pre_1984_radiation_warns_but_does_not_block(self) -> None:
        # Deliberate severity choice, not an oversight: POWER answers 200 to
        # this exact ask when a meteorology parameter rides along, dropping the
        # radiation parameter silently (point_1983_partial.json). Blocking would
        # refuse a request the API honours; the fill is caught post-fetch.
        report = preflight(
            temporal="daily",
            mode="point",
            parameters=["T2M", "ALLSKY_SFC_SW_DWN"],
            start=date(1983, 6, 1),
            end=date(1983, 6, 3),
            sites=[{"name": "boulder"}],
        )
        self.assertIn("RECORD_START", report.codes())
        self.assertIs(Level.WARNING, _finding(report, "RECORD_START").level)
        self.assertFalse(report.is_blocked)
        self.assertEqual(("ALLSKY_SFC_SW_DWN",), _finding(report, "RECORD_START").affected)


class EstimateFeatureCountTests(unittest.TestCase):
    def test_year_of_hourly_at_four_sites_for_two_parameters(self) -> None:
        # 365 days x 24 hours x 2 parameters x 4 sites. The long form is the
        # only layout the temporal controller can animate, so this count cannot
        # simply be reduced -- it is warned about instead.
        estimate = estimate_feature_count(
            temporal="hourly",
            parameters=["T2M", "PS"],
            start=date(2023, 1, 1),
            end=date(2023, 12, 31),
            n_sites=4,
        )
        self.assertEqual(70_080, estimate)
        self.assertGreater(estimate, FEATURE_COUNT_LIMIT)

    def test_no_sites_means_no_features(self) -> None:
        self.assertEqual(
            0,
            estimate_feature_count(
                temporal="daily",
                parameters=["T2M"],
                start=date(2024, 1, 1),
                end=date(2024, 12, 31),
                n_sites=0,
            ),
        )


def _point_request(temporal: str, params: tuple[str, ...], **kwargs: object) -> PowerRequest:
    fields: dict[str, object] = dict(
        url="https://power.larc.nasa.gov/api/temporal/test",
        temporal=temporal,
        mode="point",
        params=params,
        community="RE",
        start="20240201",
        end="20240203",
        site="boulder",
        latitude=LAT,
        longitude=LON,
    )
    fields.update(kwargs)
    return PowerRequest(**fields)  # type: ignore[arg-type]


class PostfetchTests(unittest.TestCase):
    """Checks against real captured responses."""

    def test_unsplit_request_reports_two_sources(self) -> None:
        # header.sources is per REQUEST, not per parameter: asking for T2M and
        # ALLSKY_SFC_SW_DWN together returns ['MERRA2', 'SYN1DEG'], so neither
        # value is attributable. This is why the planner splits by family.
        requested = ("T2M", "ALLSKY_SFC_SW_DWN")
        observations, facts = parse_point_response(
            load_bytes("point_daily_2param.json"),
            temporal="daily",
            requested=requested,
            site="boulder",
        )
        self.assertEqual(("MERRA2", "SYN1DEG"), facts.sources)
        report = postfetch(_point_request("daily", requested), facts, observations)
        self.assertIn("MIXED_SOURCES", report.codes())
        self.assertIs(Level.ERROR, _finding(report, "MIXED_SOURCES").level)
        self.assertEqual(("MERRA2", "SYN1DEG"), _finding(report, "MIXED_SOURCES").affected)

    def test_split_request_reports_no_mixed_sources(self) -> None:
        observations, facts = parse_point_response(
            load_bytes("point_solar_2001.json"),
            temporal="daily",
            requested=("ALLSKY_SFC_SW_DWN",),
            site="boulder",
        )
        self.assertEqual(("SYN1DEG",), facts.sources)
        report = postfetch(
            _point_request("daily", ("ALLSKY_SFC_SW_DWN",)), facts, observations
        )
        self.assertNotIn("MIXED_SOURCES", report.codes())

    def test_silently_omitted_parameter_is_reported(self) -> None:
        # HTTP 200 that contains only T2M for a T2M,ALLSKY_SFC_SW_DWN request,
        # explaining itself only in messages[]. Both halves must surface.
        requested = ("T2M", "ALLSKY_SFC_SW_DWN")
        observations, facts = parse_point_response(
            load_bytes("point_1983_partial.json"),
            temporal="daily",
            requested=requested,
            site="boulder",
        )
        report = postfetch(_point_request("daily", requested), facts, observations)
        self.assertIn("MISSING_PARAMETER", report.codes())
        self.assertIn("SOFT_MESSAGES", report.codes())
        self.assertEqual(
            ("ALLSKY_SFC_SW_DWN",), _finding(report, "MISSING_PARAMETER").affected
        )

    def test_annual_means_are_reported_as_dropped(self) -> None:
        # 26 keys for 24 months: 202013 and 202113 are annual means, not a
        # thirteenth month. Left in they are a spurious point every 13th step.
        payload = load_json("point_monthly_yyyy13.json")
        raw_keys = list(payload["properties"]["parameter"]["T2M"])
        self.assertEqual(26, len(raw_keys))

        observations, facts = parse_point_response(
            load_bytes("point_monthly_yyyy13.json"),
            temporal="monthly",
            requested=("T2M",),
            site="boulder",
        )
        self.assertEqual(24, len(observations))
        report = postfetch(
            _point_request("monthly", ("T2M",), start="2020", end="2021"),
            facts,
            observations,
            raw_time_keys=raw_keys,
        )
        self.assertIn("YYYY13_DROPPED", report.codes())
        self.assertEqual(("202013", "202113"), _finding(report, "YYYY13_DROPPED").affected)

    def test_cache_hit_is_reported(self) -> None:
        requested = ("T2M", "ALLSKY_SFC_SW_DWN")
        observations, facts = parse_point_response(
            load_bytes("point_daily_2param.json"),
            temporal="daily",
            requested=requested,
            site="boulder",
        )
        request = _point_request("daily", requested)
        self.assertNotIn("CACHE_HIT", postfetch(request, facts, observations).codes())
        cached = postfetch(request, facts, observations, was_cached=True)
        self.assertIn("CACHE_HIT", cached.codes())
        self.assertIs(Level.INFO, _finding(cached, "CACHE_HIT").level)

    def test_time_standard_drift_is_an_error(self) -> None:
        # POWER's own default is LST. A request that asked UTC and was answered
        # in LST is a ~7 h phase error at Boulder with nothing in the numbers
        # to reveal it, so it is an ERROR rather than a note.
        observations, facts = parse_point_response(
            load_bytes("point_hourly_lst.json"),
            temporal="hourly",
            requested=("ALLSKY_SFC_SW_DWN",),
            site="boulder",
        )
        self.assertEqual("LST", facts.time_standard)
        report = postfetch(
            _point_request("hourly", ("ALLSKY_SFC_SW_DWN",), time_standard="UTC"),
            facts,
            observations,
        )
        self.assertIn("TIME_STANDARD_DRIFT", report.codes())
        self.assertIs(Level.ERROR, _finding(report, "TIME_STANDARD_DRIFT").level)

    def test_matching_time_standard_does_not_drift(self) -> None:
        observations, facts = parse_point_response(
            load_bytes("point_hourly_utc.json"),
            temporal="hourly",
            requested=("ALLSKY_SFC_SW_DWN",),
            site="boulder",
        )
        self.assertEqual("UTC", facts.time_standard)
        report = postfetch(
            _point_request("hourly", ("ALLSKY_SFC_SW_DWN",), time_standard="UTC"),
            facts,
            observations,
        )
        self.assertNotIn("TIME_STANDARD_DRIFT", report.codes())

    def test_every_finding_carries_the_url(self) -> None:
        # A POWER error is unactionable without the URL beside it: the user
        # cannot tell which of six tiled requests was the malformed one.
        requested = ("T2M", "ALLSKY_SFC_SW_DWN")
        observations, facts = parse_point_response(
            load_bytes("point_daily_2param.json"),
            temporal="daily",
            requested=requested,
            site="boulder",
        )
        request = _point_request("daily", requested)
        report = postfetch(request, facts, observations, was_cached=True)
        self.assertTrue(report.findings)
        for finding in report.findings:
            self.assertEqual(request.url, finding.url, finding.code)


def _observation(value: float | None, parameter: str = "T2M") -> Observation:
    """A regional observation: cell coordinates are the response's own."""
    moment = datetime(2024, 2, 1, tzinfo=timezone.utc)
    return Observation(
        site="cell_40_-105",
        parameter=parameter,
        t_start=moment,
        t_end=moment,
        value=value,
        units="degC",
        native_value=value,
        native_units="C",
        longitude=LON,
        latitude=LAT,
        elevation_m=None,
        cell_longitude=None,
        cell_latitude=None,
        family=Family.METEOROLOGY,
        temporal="daily",
    )


class FillTests(unittest.TestCase):
    """Fill is -999.0 and is masked to None before scaling; QA counts the Nones."""

    def _codes(self, observations: list[Observation]) -> list[str]:
        request = PowerRequest(
            url="https://power.larc.nasa.gov/api/temporal/test",
            temporal="daily",
            mode="regional",
            params=("T2M",),
            community="RE",
            start="20240201",
            end="20240203",
        )
        return postfetch(request, ResponseFacts(), observations).codes()

    def test_every_value_missing_is_all_fill(self) -> None:
        codes = self._codes([_observation(None) for _ in range(3)])
        self.assertIn("ALL_FILL", codes)
        self.assertNotIn("PARTIAL_FILL", codes)

    def test_some_values_missing_is_partial_fill(self) -> None:
        codes = self._codes([_observation(1.0), _observation(None), _observation(3.0)])
        self.assertIn("PARTIAL_FILL", codes)
        self.assertNotIn("ALL_FILL", codes)

    def test_no_values_missing_is_neither(self) -> None:
        codes = self._codes([_observation(1.0), _observation(2.0)])
        self.assertNotIn("PARTIAL_FILL", codes)
        self.assertNotIn("ALL_FILL", codes)

    def test_fill_is_counted_per_parameter(self) -> None:
        # One dead parameter beside a healthy one must not be averaged away.
        codes = self._codes(
            [
                _observation(None, "ALLSKY_SFC_SW_DWN"),
                _observation(None, "ALLSKY_SFC_SW_DWN"),
                _observation(1.0, "T2M"),
                _observation(2.0, "T2M"),
            ]
        )
        self.assertIn("ALL_FILL", codes)
        self.assertNotIn("PARTIAL_FILL", codes)


class LevelTests(unittest.TestCase):
    def test_severity_is_ordered(self) -> None:
        self.assertGreater(Level.BLOCKING, Level.ERROR)
        self.assertGreater(Level.ERROR, Level.WARNING)
        self.assertGreater(Level.WARNING, Level.INFO)

    def test_worst_is_the_maximum(self) -> None:
        report = QaReport()
        report.add(
            QaFinding(Level.INFO, "A", "a"),
            QaFinding(Level.ERROR, "B", "b"),
            QaFinding(Level.WARNING, "C", "c"),
        )
        self.assertIs(Level.ERROR, report.worst)
        self.assertTrue(report.has_error)
        self.assertFalse(report.is_blocked)
        self.assertEqual(["B", "C"], [f.code for f in report.at_least(Level.WARNING)])

    def test_empty_report_has_no_worst(self) -> None:
        self.assertIsNone(QaReport().worst)
        self.assertFalse(QaReport().has_error)

    def test_blocking_beats_error(self) -> None:
        report = QaReport()
        report.add(QaFinding(Level.ERROR, "B", "b"), QaFinding(Level.BLOCKING, "A", "a"))
        self.assertIs(Level.BLOCKING, report.worst)
        self.assertTrue(report.is_blocked)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class UnitDriftTests(unittest.TestCase):
    """A units string the converter does not know must be reported, not guessed.

    ``units.rule_for`` returns None for an unknown string and the value passes
    through unscaled. That is the right behaviour, but silently it would put a
    kW-hr/m^2/day number on a W m-2 legend, so postfetch has to say so.
    """

    def _facts(self, units: dict[str, str]) -> ResponseFacts:
        return ResponseFacts(
            sources=("MERRA2",),
            fill_value=-999.0,
            time_standard="UTC",
            units=units,
            returned_parameters=tuple(units),
            requested_parameters=tuple(units),
        )

    def test_an_unknown_units_string_is_reported(self):
        request = _point_request("daily", ("T2M",))
        report = postfetch(request, self._facts({"T2M": "furlongs/fortnight"}), [])
        finding = _finding(report, "UNIT_DRIFT")
        self.assertEqual(finding.level, Level.WARNING)
        self.assertEqual(finding.affected, ("T2M",))
        self.assertEqual(finding.url, request.url)

    def test_known_units_produce_no_finding(self):
        # Every units string in the committed dictionaries is known, so this is
        # the normal case and must stay quiet.
        request = _point_request("daily", ("T2M", "ALLSKY_SFC_SW_DWN"))
        report = postfetch(
            request,
            self._facts({"T2M": "C", "ALLSKY_SFC_SW_DWN": "kW-hr/m^2/day"}),
            [],
        )
        self.assertNotIn("UNIT_DRIFT", report.codes())

    def test_an_empty_units_string_is_not_reported_as_drift(self):
        # A missing units string is a different fault from an unrecognised one
        # and would drown the finding in noise if it were folded in here.
        request = _point_request("daily", ("T2M",))
        report = postfetch(request, self._facts({"T2M": ""}), [])
        self.assertNotIn("UNIT_DRIFT", report.codes())

    def test_only_the_unknown_parameters_are_named(self):
        request = _point_request("daily", ("T2M", "MYSTERY"))
        report = postfetch(
            request, self._facts({"T2M": "C", "MYSTERY": "bananas"}), []
        )
        self.assertEqual(_finding(report, "UNIT_DRIFT").affected, ("MYSTERY",))


class FamilySplitTests(unittest.TestCase):
    """A mixed point ask costs an extra request; the user is told why."""

    def _preflight(self, parameters):
        return preflight(
            temporal="daily",
            mode="point",
            parameters=parameters,
            start=date(2024, 2, 1),
            end=date(2024, 2, 3),
            sites=[{"latitude": LAT, "longitude": LON}],
        )

    def test_two_families_report_the_split(self):
        report = self._preflight(["T2M", "ALLSKY_SFC_SW_DWN"])
        finding = _finding(report, "FAMILY_SPLIT")
        # INFO: it costs a request, it does not risk a wrong number.
        self.assertEqual(finding.level, Level.INFO)
        self.assertEqual(len(finding.affected), 2)

    def test_one_family_reports_nothing(self):
        self.assertNotIn("FAMILY_SPLIT", self._preflight(["T2M", "PS"]).codes())

    def test_three_families_are_counted_as_three(self):
        report = self._preflight(["T2M", "ALLSKY_SFC_SW_DWN", "SG_DAY_HOURS"])
        self.assertEqual(len(_finding(report, "FAMILY_SPLIT").affected), 3)

    def test_regional_does_not_report_a_split(self):
        # Two families, so the finding would fire if it were not mode-gated.
        # Regional serves one parameter per request, so there is no split to
        # explain -- the fan-out is over tiles, reported as TILED, and the
        # two-parameter ask is refused outright as PARAM_COUNT.
        report = preflight(
            temporal="daily",
            mode="regional",
            parameters=["T2M", "ALLSKY_SFC_SW_DWN"],
            start=date(2024, 2, 1),
            end=date(2024, 2, 3),
            bbox=LEGAL_BBOX,
        )
        self.assertIn("PARAM_COUNT", report.codes())
        self.assertNotIn("FAMILY_SPLIT", report.codes())


class CellSnapScopeTests(unittest.TestCase):
    """The derived cell centre is a point-mode statement only.

    A regional response's own coordinate already *is* the cell centre, so
    reporting a derived one there would claim arithmetic that was never done.
    """

    def _facts(self) -> ResponseFacts:
        return ResponseFacts(sources=("MERRA2",), fill_value=-999.0, time_standard="UTC")

    def _observation_with_cell(self) -> Observation:
        moment = datetime(2024, 2, 1, tzinfo=timezone.utc)
        return Observation(
            site="boulder",
            parameter="T2M",
            t_start=moment,
            t_end=moment,
            value=273.0,
            units="K",
            native_value=0.0,
            native_units="C",
            longitude=LON,
            latitude=LAT,
            elevation_m=None,
            cell_longitude=-105.0,
            cell_latitude=40.0,
            family=Family.METEOROLOGY,
            temporal="daily",
        )

    def test_point_mode_reports_the_derived_centre(self):
        report = postfetch(
            _point_request("daily", ("T2M",)),
            self._facts(),
            [self._observation_with_cell()],
        )
        self.assertEqual(_finding(report, "CELL_SNAP_DERIVED").level, Level.INFO)

    def test_regional_mode_does_not(self):
        request = _point_request("daily", ("T2M",), mode="regional", site=None)
        report = postfetch(request, self._facts(), [self._observation_with_cell()])
        self.assertNotIn("CELL_SNAP_DERIVED", report.codes())

    def test_a_point_observation_with_no_derived_centre_reports_nothing(self):
        observation = _observation(1.0)  # regional-shaped: cell coords are None
        report = postfetch(
            _point_request("daily", ("T2M",)), self._facts(), [observation]
        )
        self.assertNotIn("CELL_SNAP_DERIVED", report.codes())


class ValidRangeTests(unittest.TestCase):
    """``check_valid_range`` reports pixels outside the range POWER declared.

    The range lives in the **NetCDF band metadata** and nowhere else -- measured
    on ``regional_daily_t2m_tileN.nc``, ``T2M`` declares ``valid_min = -125``
    and ``valid_max = 80``, while the JSON responses carry no such attribute at
    all. So the count comes from the raster path.

    An earlier version of this function walked ``Observation``s, which meant it
    could never fire: the code path that has the values has no range, and the
    path that has the range does not build Observations.
    """

    RANGE = (-125.0, 80.0)

    def test_pixels_outside_the_range_are_an_error(self):
        report = check_valid_range("T2M", self.RANGE, out_of_range=7)
        finding = _finding(report, "VALID_RANGE")
        self.assertEqual(finding.level, Level.ERROR)
        self.assertEqual(finding.affected, ("T2M",))
        self.assertIn("7", finding.message)

    def test_no_offending_pixels_is_silent(self):
        self.assertEqual(check_valid_range("T2M", self.RANGE, 0).codes(), [])

    def test_a_response_declaring_no_range_is_silent(self):
        # Not every parameter declares one, and absence is not a fault.
        self.assertEqual(check_valid_range("T2M", None, 5).codes(), [])

    def test_the_declared_range_reaches_the_message(self):
        finding = _finding(check_valid_range("T2M", self.RANGE, 1), "VALID_RANGE")
        self.assertIn("-125", finding.message)
        self.assertIn("80", finding.message)

    def test_the_url_is_carried_so_the_request_is_identifiable(self):
        finding = _finding(
            check_valid_range("T2M", self.RANGE, 1, url="https://example.invalid/x"),
            "VALID_RANGE",
        )
        self.assertEqual(finding.url, "https://example.invalid/x")

class ReportSeverityBoundaryTests(unittest.TestCase):
    """``has_error`` is the layer's warning marker; its threshold is ERROR."""

    def _report(self, *levels: Level) -> QaReport:
        return QaReport([QaFinding(lvl, f"C{i}", "m") for i, lvl in enumerate(levels)])

    def test_a_warning_alone_does_not_mark_the_layer(self):
        # WARNING is common -- LST, RECORD_START, PARTIAL_FILL all reach it. If
        # it tripped the marker, the marker would be on almost every layer and
        # would stop meaning anything.
        self.assertFalse(self._report(Level.INFO, Level.WARNING).has_error)

    def test_an_error_marks_the_layer(self):
        self.assertTrue(self._report(Level.INFO, Level.ERROR).has_error)

    def test_blocking_marks_it_too(self):
        self.assertTrue(self._report(Level.BLOCKING).has_error)

    def test_an_empty_report_marks_nothing(self):
        self.assertFalse(QaReport().has_error)
        self.assertFalse(QaReport().is_blocked)
        self.assertIsNone(QaReport().worst)
