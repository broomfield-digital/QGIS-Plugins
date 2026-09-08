"""Guard the JSON decoder against the response shapes POWER actually serves.

Every payload here is a committed capture from power.larc.nasa.gov
(``tests/fixtures/MANIFEST.json`` records the URL for each), so these are
assertions about the live API, not about a mock. The three traps under test:

* a **200 that omits a requested parameter** and says so only in ``messages[]``
  -- ``point_1983_partial.json``;
* **``YYYY13`` is an annual mean**, so 26 monthly keys are 24 months --
  ``point_monthly_yyyy13.json``;
* the **time standard shifts the whole series**, not a label -- the same 911.15
  W m-2 peak sits at 17:00 UTC and at 10:00 LST.

Unit conversion is asserted on specific numbers rather than on units strings:
a conversion that relabels without scaling passes a units assertion and still
puts the wrong values on the map.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from nasa_power.core.decode import (
    Observation,
    PowerParseError,
    annual_means_dropped,
    parse_point_response,
    parse_regional_response,
    read_facts,
)
from nasa_power.core.provenance import Family
from tests.unit.support import FIXTURES, load_bytes, load_json

BOULDER = ("boulder", 40.02, -105.27)


def _by(observations, parameter, start):
    """The single observation for ``parameter`` beginning at ``start``."""
    hits = [o for o in observations if o.parameter == parameter and o.t_start == start]
    if len(hits) != 1:
        raise AssertionError(f"expected 1 observation, got {len(hits)}")
    return hits[0]


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class PointDailyBaselineTest(unittest.TestCase):
    """``point_daily_2param.json``: two parameters, two parents, three days."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = load_json("point_daily_2param.json")
        cls.observations, cls.facts = parse_point_response(
            load_bytes("point_daily_2param.json"),
            temporal="daily",
            requested=["T2M", "ALLSKY_SFC_SW_DWN"],
            site="boulder",
            # Native units are the default; this class asserts the conversion
            # arithmetic, so it opts in.
            convert=True,
        )

    def test_one_observation_per_parameter_per_day(self):
        # The long layout: 2 parameters x 3 days, never a wide row per day.
        self.assertEqual(len(self.observations), 6)
        self.assertEqual(len({o.parameter for o in self.observations}), 2)
        self.assertEqual(len({o.t_start for o in self.observations}), 3)

    def test_sources_are_per_request_and_attributable_to_neither(self):
        # Measured 2026-09-07: one request for T2M + ALLSKY_SFC_SW_DWN answers
        # with both parents in a single flat list, so no value in this response
        # can be labelled from header.sources alone. That is why the planner
        # splits by family.
        self.assertEqual(self.facts.sources, ("MERRA2", "SYN1DEG"))
        self.assertEqual(
            {o.family for o in self.observations},
            {Family.METEOROLOGY, Family.RADIATION},
        )

    def test_fill_value_and_time_standard_are_read_from_the_header(self):
        self.assertEqual(self.facts.fill_value, -999.0)
        # Requested UTC and got UTC. The endpoint is free to answer LST, which
        # is why the returned string is kept rather than the requested one.
        self.assertEqual(self.facts.time_standard, "UTC")

    def test_api_identity_is_recorded(self):
        # Version drifts by design (daily was v2.9.7 while monthly was v2.9.8),
        # so this pins that it is captured, not which one it is.
        self.assertTrue(self.facts.api_version)
        self.assertEqual(self.facts.api_version, self.raw["header"]["api"]["version"])
        self.assertEqual(self.facts.api_name, "POWER Daily API")

    def test_units_are_read_per_parameter(self):
        # Community RE: solar arrives as kW-hr/m^2/day. AG would say
        # MJ/m^2/day and SB W m-2 for the identical parameter, so a table keyed
        # on (parameter, temporal) is wrong the first time a user switches.
        self.assertEqual(
            dict(self.facts.units),
            {"T2M": "C", "ALLSKY_SFC_SW_DWN": "kW-hr/m^2/day"},
        )
        self.assertEqual(
            self.facts.long_names["T2M"], "Temperature at 2 Meters"
        )

    def test_temperature_converts_from_celsius_to_kelvin(self):
        obs = _by(self.observations, "T2M", _utc(2024, 2, 1))
        self.assertEqual(obs.native_value, 7.25)
        self.assertEqual(obs.native_units, "C")
        self.assertAlmostEqual(obs.value, 280.4, places=9)
        self.assertEqual(obs.units, "K")

    def test_daily_irradiance_converts_to_watts_per_square_metre(self):
        # 3.5455 kWh/m2/day = 3545.5 Wh spread over 24 h = 147.729... W/m2.
        obs = _by(self.observations, "ALLSKY_SFC_SW_DWN", _utc(2024, 2, 1))
        self.assertEqual(obs.native_value, 3.5455)
        self.assertEqual(obs.native_units, "kW-hr/m^2/day")
        self.assertAlmostEqual(obs.value, 3.5455 * 1000.0 / 24.0, places=9)
        self.assertAlmostEqual(obs.value, 147.729166666, places=6)
        self.assertEqual(obs.units, "W m-2")

    def test_intervals_are_half_open_days_in_utc(self):
        for obs in self.observations:
            self.assertEqual(obs.t_end - obs.t_start, timedelta(days=1))
            self.assertEqual(obs.t_start.tzinfo, timezone.utc)
            self.assertEqual(obs.t_end.tzinfo, timezone.utc)

    def test_geometry_echoes_the_requested_coordinate(self):
        # POWER never discloses the answering cell; the response coordinate is
        # the one that was asked for, elevation included.
        for obs in self.observations:
            self.assertEqual((obs.latitude, obs.longitude), (40.02, -105.27))
            self.assertEqual(obs.elevation_m, 1801.15)

    def test_point_cell_centre_is_derived_per_family(self):
        # Derived locally, and NOT the same cell for both parameters: the two
        # families are served on grids that are not co-registered.
        t2m = _by(self.observations, "T2M", _utc(2024, 2, 1))
        solar = _by(self.observations, "ALLSKY_SFC_SW_DWN", _utc(2024, 2, 1))
        self.assertEqual((t2m.cell_latitude, t2m.cell_longitude), (40.0, -105.0))
        self.assertEqual((solar.cell_latitude, solar.cell_longitude), (40.5, -105.5))

    def test_nothing_is_missing_or_unexpected(self):
        self.assertEqual(self.facts.missing_parameters, ())
        self.assertEqual(self.facts.unexpected_parameters, ())
        self.assertEqual(
            self.facts.returned_parameters, ("T2M", "ALLSKY_SFC_SW_DWN")
        )

    def test_no_value_is_masked_in_a_clean_response(self):
        self.assertTrue(all(o.value is not None for o in self.observations))


class SilentOmissionTest(unittest.TestCase):
    """``point_1983_partial.json``: HTTP 200 that quietly drops a parameter.

    Both parameters were requested; radiation does not exist before 1984-01-01,
    so POWER returned 200 with only T2M in ``properties.parameter`` and an
    explanation in ``messages[]``. A parser keyed on the requested list raises
    KeyError here; one keyed on the response loses a variable in silence.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.observations, cls.facts = parse_point_response(
            load_bytes("point_1983_partial.json"),
            temporal="daily",
            requested=["T2M", "ALLSKY_SFC_SW_DWN"],
            site="boulder",
        )

    def test_parsing_does_not_raise_and_yields_only_what_came_back(self):
        self.assertEqual(len(self.observations), 3)
        self.assertEqual({o.parameter for o in self.observations}, {"T2M"})

    def test_the_dropped_parameter_is_named(self):
        self.assertEqual(self.facts.missing_parameters, ("ALLSKY_SFC_SW_DWN",))
        self.assertEqual(self.facts.unexpected_parameters, ())

    def test_the_explanation_is_carried_out_of_the_response(self):
        # Prose is POWER's and will change; that there is exactly one message
        # to show the user is the fact worth pinning.
        self.assertEqual(len(self.facts.messages), 1)

    def test_only_the_surviving_parents_are_reported(self):
        self.assertEqual(self.facts.sources, ("MERRA2",))


class MonthlyAnnualMeanTest(unittest.TestCase):
    """``point_monthly_yyyy13.json``: 26 keys for 24 months."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = load_json("point_monthly_yyyy13.json")
        cls.observations, cls.facts = parse_point_response(
            cls.raw, temporal="monthly", requested=["T2M"], site="boulder"
        )

    def test_the_response_really_does_carry_twenty_six_keys(self):
        self.assertEqual(len(self.raw["properties"]["parameter"]["T2M"]), 26)

    def test_two_of_them_are_annual_means_and_are_dropped(self):
        self.assertEqual(len(self.observations), 24)
        self.assertEqual(
            annual_means_dropped(self.raw, "monthly"), ["202013", "202113"]
        )

    def test_no_thirteenth_month_survives(self):
        months = sorted({o.t_start.month for o in self.observations})
        self.assertEqual(months, list(range(1, 13)))
        self.assertEqual(
            sorted({o.t_start.year for o in self.observations}), [2020, 2021]
        )

    def test_the_annual_mean_values_are_not_in_the_series(self):
        # 202013 = 10.81 C and 202113 = 10.89 C. Left in, each is a spurious
        # point every thirteenth step, plausible enough to survive review.
        natives = [o.native_value for o in self.observations]
        self.assertNotIn(10.81, natives)
        self.assertNotIn(10.89, natives)

    def test_month_intervals_are_half_open_and_close_on_the_next_month(self):
        january = _by(self.observations, "T2M", _utc(2020, 1, 1))
        self.assertEqual(january.t_end, _utc(2020, 2, 1))
        december = _by(self.observations, "T2M", _utc(2020, 12, 1))
        self.assertEqual(december.t_end, _utc(2021, 1, 1))


class HourlyTimeStandardTest(unittest.TestCase):
    """The 7 h trap: identical values, different hours, no marker in the data."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.utc, cls.utc_facts = parse_point_response(
            load_bytes("point_hourly_utc.json"),
            temporal="hourly",
            requested=["ALLSKY_SFC_SW_DWN"],
            site="boulder",
            convert=True,
        )
        cls.lst, cls.lst_facts = parse_point_response(
            load_bytes("point_hourly_lst.json"),
            temporal="hourly",
            requested=["ALLSKY_SFC_SW_DWN"],
            site="boulder",
            convert=True,
        )

    @staticmethod
    def _peak(observations: list[Observation]) -> Observation:
        return max(observations, key=lambda o: o.value)

    def test_both_days_have_twenty_four_hours(self):
        self.assertEqual(len(self.utc), 24)
        self.assertEqual(len(self.lst), 24)
        for obs in self.utc + self.lst:
            self.assertEqual(obs.t_end - obs.t_start, timedelta(hours=1))

    def test_the_returned_time_standard_is_carried_not_the_requested_one(self):
        self.assertEqual(self.utc_facts.time_standard, "UTC")
        self.assertEqual(self.lst_facts.time_standard, "LST")

    def test_the_same_peak_value_sits_seven_hours_apart(self):
        # Measured 2026-09-07 at Boulder for 2024-06-01: peak 911.15 at
        # ...17 in UTC and at ...10 in LST. Same number, different timestamp --
        # nothing in the values distinguishes the two series.
        utc_peak = self._peak(self.utc)
        lst_peak = self._peak(self.lst)
        self.assertAlmostEqual(utc_peak.value, lst_peak.value, places=9)
        self.assertAlmostEqual(utc_peak.value, 911.15, places=9)
        self.assertEqual(utc_peak.t_start.hour, 17)
        self.assertEqual(lst_peak.t_start.hour, 10)
        self.assertEqual(utc_peak.t_start.hour - lst_peak.t_start.hour, 7)

    def test_hourly_irradiance_is_relabelled_not_rescaled(self):
        # Wh/m^2 -> W m-2 is x1: a watt-hour accumulated over one hour IS a
        # watt. x3600 here would put the peak at 3.28 MW/m2.
        peak = self._peak(self.utc)
        self.assertEqual(peak.native_units, "Wh/m^2")
        self.assertEqual(peak.units, "W m-2")
        self.assertAlmostEqual(peak.value, peak.native_value, places=9)

    def test_hourly_solar_is_not_the_daily_units_string(self):
        self.assertEqual(self.utc_facts.units["ALLSKY_SFC_SW_DWN"], "Wh/m^2")


class RegionalFeatureCollectionTest(unittest.TestCase):
    """``regional_daily_fc.json``: a FeatureCollection of cell centres."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.observations, cls.facts = parse_regional_response(
            load_bytes("regional_daily_fc.json"),
            temporal="daily",
            requested=["T2M"],
            convert=True,
        )

    def test_one_observation_per_cell(self):
        # 3 longitudes x 5 latitudes x 1 parameter x 1 day.
        self.assertEqual(len(self.observations), 15)
        self.assertEqual(len({(o.latitude, o.longitude) for o in self.observations}), 15)
        self.assertEqual(len({o.site for o in self.observations}), 15)

    def test_a_named_cell_converts_correctly(self):
        cell = [
            o for o in self.observations
            if (o.latitude, o.longitude) == (40.0, -105.625)
        ]
        self.assertEqual(len(cell), 1)
        self.assertEqual(cell[0].native_value, -0.6)
        self.assertEqual(cell[0].native_units, "C")
        self.assertAlmostEqual(cell[0].value, 272.55, places=9)
        self.assertEqual(cell[0].units, "K")

    def test_regional_coordinates_are_already_cell_centres(self):
        # Deriving a centre from a centre would only add rounding error, so the
        # derived fields stay empty and the distinction stays visible.
        for obs in self.observations:
            self.assertIsNone(obs.cell_latitude)
            self.assertIsNone(obs.cell_longitude)

    def test_point_mode_derives_a_centre_where_regional_does_not(self):
        point, _facts = parse_point_response(
            load_bytes("point_daily_2param.json"),
            temporal="daily",
            requested=["T2M"],
            site="boulder",
        )
        self.assertTrue(all(o.cell_latitude is not None for o in point))
        self.assertTrue(all(o.cell_latitude is None for o in self.observations))

    def test_a_single_parent_is_attributable_here(self):
        self.assertEqual(self.facts.sources, ("MERRA2",))
        self.assertEqual(self.facts.missing_parameters, ())


class MaskingTest(unittest.TestCase):
    """Fill is masked before scaling, not after.

    Derived from the real baseline capture by substituting the fill sentinel
    the header itself declares: -999.0 scaled by the kWh/m2/day factor becomes
    -41625 W/m2, which does not look wrong on a colour ramp.
    """

    def test_the_fill_sentinel_becomes_none_in_both_columns(self):
        payload = load_json("point_daily_2param.json")
        payload["properties"]["parameter"]["ALLSKY_SFC_SW_DWN"]["20240202"] = -999.0
        observations, facts = parse_point_response(
            payload, temporal="daily", requested=["ALLSKY_SFC_SW_DWN"], site="boulder"
        )
        masked = _by(observations, "ALLSKY_SFC_SW_DWN", _utc(2024, 2, 2))
        self.assertIsNone(masked.value)
        self.assertIsNone(masked.native_value)
        self.assertEqual(facts.fill_value, -999.0)
        # Its neighbours are untouched.
        self.assertIsNotNone(
            _by(observations, "ALLSKY_SFC_SW_DWN", _utc(2024, 2, 1)).value
        )


class ErrorPathTest(unittest.TestCase):
    """Bad bodies fail as PowerParseError, naming the request that produced them."""

    URL = "https://power.larc.nasa.gov/api/temporal/hourly/regional?parameters=T2M"

    def test_an_html_body_says_it_is_html(self):
        # hourly/regional does not exist and answers with a 19 KB HTML page,
        # not a JSON API error. "not valid JSON" alone would send the reader
        # looking for a parser bug.
        body = load_bytes("error_404_hourly_regional.html")
        with self.assertRaises(PowerParseError) as caught:
            parse_point_response(body, temporal="hourly", url=self.URL)
        self.assertIn("html", str(caught.exception).lower())

    def test_the_url_is_in_the_message_when_given(self):
        # Six tiled requests, one failure: without the URL the user cannot tell
        # which tile was malformed.
        body = load_bytes("error_404_hourly_regional.html")
        with self.assertRaises(PowerParseError) as caught:
            parse_point_response(body, temporal="hourly", url=self.URL)
        self.assertIn(self.URL, str(caught.exception))

    def test_a_json_object_without_properties_raises(self):
        payload = load_json("point_daily_2param.json")
        del payload["properties"]
        with self.assertRaises(PowerParseError) as caught:
            parse_point_response(payload, temporal="daily", url="https://example/u1")
        self.assertIn("properties", str(caught.exception))
        self.assertIn("https://example/u1", str(caught.exception))

    def test_a_response_without_a_parameter_block_raises(self):
        payload = load_json("point_daily_2param.json")
        payload["properties"] = {}
        with self.assertRaises(PowerParseError):
            parse_point_response(payload, temporal="daily")

    def test_a_truncated_body_raises_rather_than_parsing_short(self):
        body = load_bytes("point_daily_2param.json")[:200]
        with self.assertRaises(PowerParseError):
            parse_point_response(body, temporal="daily", url="https://example/u2")

    def test_a_json_array_is_not_a_response(self):
        with self.assertRaises(PowerParseError):
            parse_point_response(b"[1, 2, 3]", temporal="daily")

    def test_a_regional_feature_without_properties_raises(self):
        payload = load_json("regional_daily_fc.json")
        del payload["features"][3]["properties"]
        with self.assertRaises(PowerParseError):
            parse_regional_response(payload, temporal="daily", requested=["T2M"])

    def test_a_point_body_is_not_a_regional_body(self):
        # Dispatch is on the requested mode, so feeding a Feature to the
        # FeatureCollection parser must fail loudly rather than yield nothing.
        with self.assertRaises(PowerParseError):
            parse_regional_response(
                load_bytes("point_daily_2param.json"), temporal="daily"
            )


class ReadFactsTest(unittest.TestCase):
    """``read_facts`` never hands back ``None`` where a sequence is promised."""

    def test_a_response_with_no_messages_gives_an_empty_tuple(self):
        payload = load_json("point_daily_2param.json")
        del payload["messages"]
        facts = read_facts(payload, ["T2M"])
        self.assertEqual(facts.messages, ())
        self.assertIsInstance(facts.messages, tuple)

    def test_an_empty_messages_list_gives_an_empty_tuple(self):
        facts = read_facts(load_json("point_daily_2param.json"), ["T2M"])
        self.assertEqual(facts.messages, ())

    def test_a_bare_string_source_is_still_a_tuple(self):
        payload = load_json("point_daily_2param.json")
        payload["header"]["sources"] = "MERRA2"
        self.assertEqual(read_facts(payload).sources, ("MERRA2",))

    def test_requested_but_unasked_parameters_are_reported(self):
        # POWER substituting a parameter shows up here, not as a silent extra
        # column in the layer.
        facts = read_facts(load_json("point_daily_2param.json"), ["T2M"])
        self.assertEqual(facts.unexpected_parameters, ("ALLSKY_SFC_SW_DWN",))
        self.assertEqual(facts.missing_parameters, ())

    def test_a_header_that_is_not_an_object_raises(self):
        payload = load_json("point_daily_2param.json")
        payload["header"] = "NASA/POWER"
        with self.assertRaises(PowerParseError):
            read_facts(payload, ["T2M"])


class FixtureIntegrityTest(unittest.TestCase):
    """The captures these tests reason about are the ones on disk."""

    def test_every_fixture_this_module_reads_exists(self):
        for name in (
            "point_daily_2param.json",
            "point_1983_partial.json",
            "point_monthly_yyyy13.json",
            "point_hourly_utc.json",
            "point_hourly_lst.json",
            "regional_daily_fc.json",
            "error_404_hourly_regional.html",
        ):
            self.assertTrue((FIXTURES / name).is_file(), name)


if __name__ == "__main__":
    unittest.main()


#: The first day of the baseline capture, as key and as decoded instant.
FIRST_KEY = "20240201"
FIRST_START = _utc(2024, 2, 1)


class FillValueIsReadNotAssumedTest(unittest.TestCase):
    """``header.fill_value`` must come from the response, not from a constant.

    Every committed fixture declares -999.0, so a module that hardcoded -999.0
    would satisfy every other test in this file. The only way to tell the two
    apart offline is to hand the parser a response that declares something else
    -- which is exactly what would happen the day POWER changes it.
    """

    def setUp(self):
        self.raw = load_json("point_daily_2param.json")
        self.assertEqual(sorted(self.raw["properties"]["parameter"]["T2M"])[0], FIRST_KEY)

    def _parse(self, payload):
        return parse_point_response(
            payload, temporal="daily", requested=["T2M"], site="boulder", convert=True
        )

    def test_the_committed_fixtures_all_declare_minus_999(self):
        # The premise of the tests below: -999.0 really is what POWER sends, so
        # a hardcoded -999.0 is invisible everywhere else.
        for name in (
            "point_daily_2param.json",
            "point_monthly_yyyy13.json",
            "point_hourly_utc.json",
            "point_1983_partial.json",
        ):
            with self.subTest(fixture=name):
                self.assertEqual(load_json(name)["header"]["fill_value"], -999.0)

    def test_a_different_declared_fill_is_the_one_that_gets_masked(self):
        payload = json.loads(json.dumps(self.raw))
        payload["header"]["fill_value"] = -8888.0
        series = payload["properties"]["parameter"]["T2M"]
        series[FIRST_KEY] = -8888.0

        observations, facts = self._parse(payload)
        self.assertEqual(facts.fill_value, -8888.0)
        masked = _by(observations, "T2M", FIRST_START)
        self.assertIsNone(masked.value)
        self.assertIsNone(masked.native_value)

    def test_minus_999_is_left_alone_when_it_is_not_the_declared_fill(self):
        # The other half, and the one a hardcoded sentinel gets wrong: with a
        # different fill declared, -999.0 is an ordinary (if implausible)
        # reading and must survive conversion rather than vanish.
        payload = json.loads(json.dumps(self.raw))
        payload["header"]["fill_value"] = -8888.0
        series = payload["properties"]["parameter"]["T2M"]
        series[FIRST_KEY] = -999.0

        observations, _facts = self._parse(payload)
        kept = _by(observations, "T2M", FIRST_START)
        self.assertEqual(kept.native_value, -999.0)
        # C -> K, so the value is carried through the offset rather than dropped.
        self.assertAlmostEqual(kept.value, -999.0 + 273.15, places=6)

    def test_a_response_declaring_no_fill_masks_nothing(self):
        payload = json.loads(json.dumps(self.raw))
        del payload["header"]["fill_value"]
        series = payload["properties"]["parameter"]["T2M"]
        series[FIRST_KEY] = -999.0

        observations, facts = self._parse(payload)
        self.assertIsNone(facts.fill_value)
        self.assertEqual(_by(observations, "T2M", FIRST_START).native_value, -999.0)


class NativeUnitsAreTheDefaultTest(unittest.TestCase):
    """Values arrive in POWER's own units unless conversion is asked for.

    A deliberate choice, and the opposite of DAVINCI's always-to-SI behaviour:
    a layer's numbers should match what the POWER website shows, so nothing is
    silently rescaled underneath a user comparing the two. SI is one checkbox
    away, and the conversion is recorded in the layer's history when it happens.

    Guarded because it is exactly the kind of default a later edit "tidies"
    back to always-convert.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = load_json("point_daily_2param.json")

    def _parse(self, **kwargs):
        return parse_point_response(
            self.raw,
            temporal="daily",
            requested=["T2M", "ALLSKY_SFC_SW_DWN"],
            site="boulder",
            **kwargs,
        )

    def test_no_conversion_by_default(self):
        observations, _facts = self._parse()
        temperature = _by(observations, "T2M", _utc(2024, 2, 1))
        self.assertEqual(temperature.value, 7.25)
        self.assertEqual(temperature.units, "C")

        irradiance = _by(observations, "ALLSKY_SFC_SW_DWN", _utc(2024, 2, 1))
        self.assertEqual(irradiance.value, 3.5455)
        self.assertEqual(irradiance.units, "kW-hr/m^2/day")

    def test_value_equals_native_value_when_not_converting(self):
        observations, _facts = self._parse()
        for observation in observations:
            self.assertEqual(observation.value, observation.native_value)
            self.assertEqual(observation.units, observation.native_units)

    def test_convert_true_still_converts(self):
        # The opt-in must remain wired: the same fixture, one flag apart.
        observations, _facts = self._parse(convert=True)
        temperature = _by(observations, "T2M", _utc(2024, 2, 1))
        self.assertAlmostEqual(temperature.value, 280.4, places=9)
        self.assertEqual(temperature.units, "K")

    def test_fill_is_masked_even_without_conversion(self):
        # Masking is not a unit choice. -999 is the difference between a
        # missing value and a reading, and it must never reach a colour ramp
        # whichever units the layer carries.
        payload = json.loads(json.dumps(self.raw))
        payload["properties"]["parameter"]["T2M"][FIRST_KEY] = -999.0
        observations, _facts = parse_point_response(
            payload, temporal="daily", requested=["T2M"], site="boulder"
        )
        masked = _by(observations, "T2M", FIRST_START)
        self.assertIsNone(masked.value)
        self.assertIsNone(masked.native_value)
