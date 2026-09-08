"""Guards :mod:`nasa_power.core.units`.

The whole point of that module is that conversion is keyed on the **returned
units string**, never on ``(parameter, temporal)``. The first test class here is
the one that would have caught the ``POWER_CATALOG`` keying bug: daily
``ALLSKY_SFC_SW_DWN`` arrives in three different units depending on community,
and all three must land on the same number of watts per square metre.

Everything asserted below is either arithmetic that can be checked by hand or a
units string measured in a committed fixture; where it is the latter the comment
says which fixture.
"""

from __future__ import annotations

import unittest

from nasa_power.core.units import (
    KWH_M2_DAY_TO_W_M2,
    MJ_M2_DAY_TO_W_M2,
    STEP_HOURS,
    UNIT_RULES,
    canonical_units,
    convert_series,
    describe_conversion,
    known_units,
    rule_for,
    to_canonical,
)
from tests.unit.support import load_json

#: The three community dictionaries, all daily, all fetched 2026-09-07.
DICTIONARY_FIXTURES = (
    ("RE", "dict_daily_RE.json"),
    ("AG", "dict_daily_AG.json"),
    ("SB", "dict_daily_SB.json"),
)


class TestCommunitiesAgree(unittest.TestCase):
    """One irradiance, three native units, one answer.

    Measured 2026-09-07 for daily ``ALLSKY_SFC_SW_DWN`` at Boulder: community RE
    serves ``kW-hr/m^2/day``, AG serves ``MJ/m^2/day``, SB serves ``W m-2``. The
    three sample values below are the same physical flux expressed each way.
    """

    #: Same flux, three wire formats. 3.5455 kWh/m2/day = 12.76 MJ/m2/day
    #: = 147.7 W/m2.
    SAMPLES = (
        ("RE", 3.5455, "kW-hr/m^2/day", 147.73),
        ("AG", 12.76, "MJ/m^2/day", 147.69),
        ("SB", 147.73, "W m-2", 147.73),
    )

    def test_each_community_converts_to_the_expected_flux(self):
        for community, value, native, expected in self.SAMPLES:
            with self.subTest(community=community):
                got = to_canonical(value, native, "daily")
                self.assertIsNotNone(got)
                self.assertAlmostEqual(got, expected, delta=0.01)
                self.assertEqual(canonical_units(native), "W m-2")

    def test_the_three_agree_within_a_tenth_of_a_percent(self):
        # The failure mode this guards is a table keyed on (parameter, temporal):
        # it gets one community right and is out by 41.7x or 11.6x for the others.
        converted = [
            to_canonical(value, native, "daily")
            for _community, value, native, _expected in self.SAMPLES
        ]
        spread = (max(converted) - min(converted)) / (sum(converted) / len(converted))
        self.assertLess(spread, 0.001, f"communities disagree: {converted}")

    def test_the_dictionaries_really_do_disagree(self):
        # If this ever stops failing to be true, the premise of the whole module
        # is gone and the keying could be simplified. Measured 2026-09-07.
        solar_units = {}
        t2m_units = {}
        for community, filename in DICTIONARY_FIXTURES:
            entries = load_json(filename)
            solar_units[community] = entries["ALLSKY_SFC_SW_DWN"]["units"]
            t2m_units[community] = entries["T2M"]["units"]

        self.assertEqual(
            solar_units,
            {"RE": "kW-hr/m^2/day", "AG": "MJ/m^2/day", "SB": "W m-2"},
        )
        # ...while temperature is identical in all three, which is exactly why
        # a parameter-keyed table looks correct right up until it is not.
        self.assertEqual(set(t2m_units.values()), {"C"})
        for native in solar_units.values():
            self.assertEqual(canonical_units(native), "W m-2")

    def test_the_documented_factors_are_the_arithmetic(self):
        self.assertAlmostEqual(KWH_M2_DAY_TO_W_M2, 1000.0 / 24.0, places=12)
        self.assertAlmostEqual(MJ_M2_DAY_TO_W_M2, 1.0e6 / 86400.0, places=12)


class TestHourlyAccumulation(unittest.TestCase):
    """``Wh/m^2`` is a watt-hour over one hour, which is a watt: x1, not x3600."""

    def test_hourly_wh_per_m2_is_multiplied_by_one(self):
        self.assertAlmostEqual(to_canonical(911.15, "Wh/m^2", "hourly"), 911.15, places=9)

    def test_hourly_wh_per_m2_is_not_multiplied_by_3600(self):
        got = to_canonical(911.15, "Wh/m^2", "hourly")
        self.assertNotAlmostEqual(got, 911.15 * 3600.0, delta=1.0)
        self.assertNotAlmostEqual(got, 911.15 / 3600.0, delta=1e-6)

    def test_hourly_fixture_peak_survives_conversion_unchanged(self):
        # point_hourly_utc.json: units 'Wh/m^2', peak 911.15 at 2024060117 UTC.
        payload = load_json("point_hourly_utc.json")
        native = payload["parameters"]["ALLSKY_SFC_SW_DWN"]["units"]
        self.assertEqual(native, "Wh/m^2")

        series = payload["properties"]["parameter"]["ALLSKY_SFC_SW_DWN"]
        keys = sorted(series)
        converted = convert_series(
            [series[k] for k in keys],
            native,
            "hourly",
            fill_value=payload["header"]["fill_value"],
        )
        self.assertEqual(len(converted), 24)
        peak = converted[keys.index("2024060117")]
        self.assertAlmostEqual(peak, 911.15, places=9)
        # A plausible-looking 911150 W/m2 is above the solar constant; the whole
        # daylit series must stay inside physical bounds.
        self.assertLess(max(v for v in converted if v is not None), 1400.0)

    def test_step_length_is_only_defined_for_hourly(self):
        self.assertEqual(dict(STEP_HOURS), {"hourly": 1.0})

    def test_accumulated_units_at_a_temporal_with_no_step_raise(self):
        # Guessing a step here would produce a wrong number that looks right.
        for temporal in ("daily", "monthly", "climatology"):
            with self.subTest(temporal=temporal):
                with self.assertRaises(ValueError) as caught:
                    to_canonical(911.15, "Wh/m^2", temporal)
                self.assertIn(temporal, str(caught.exception))
                with self.assertRaises(ValueError):
                    convert_series([911.15], "Wh/m^2", temporal)


class TestScalarRules(unittest.TestCase):
    """The offsets and factors, each checkable by hand."""

    def test_celsius_to_kelvin_is_an_offset(self):
        self.assertAlmostEqual(to_canonical(0.0, "C", "daily"), 273.15, places=9)
        self.assertAlmostEqual(to_canonical(-0.04, "C", "monthly"), 273.11, places=9)
        self.assertEqual(canonical_units("C"), "K")

    def test_celsius_is_not_scaled(self):
        # +273.15, not x273.15: a scale would put 20 C at 5463 K.
        self.assertAlmostEqual(to_canonical(20.0, "C", "daily"), 293.15, places=9)

    def test_kilopascals_to_pascals(self):
        self.assertAlmostEqual(to_canonical(83.1, "kPa", "daily"), 83100.0, places=6)
        self.assertEqual(canonical_units("kPa"), "Pa")

    def test_centimetres_to_metres(self):
        self.assertAlmostEqual(to_canonical(2.5, "cm", "daily"), 0.025, places=12)
        self.assertEqual(canonical_units("cm"), "m")

    def test_grams_per_kilogram_to_kilograms_per_kilogram(self):
        self.assertAlmostEqual(to_canonical(4.2, "g/kg", "daily"), 0.0042, places=12)
        self.assertEqual(canonical_units("g/kg"), "kg kg-1")

    def test_relabelling_rules_do_no_arithmetic(self):
        for native, canonical in (
            ("m/s", "m s-1"),
            ("mm/day", "mm day-1"),
            ("mm/hour", "mm hr-1"),
            ("%", "%"),
            ("Degrees", "degree"),
            ("Dobsons", "DU"),
            ("count", "1"),
        ):
            with self.subTest(native=native):
                self.assertEqual(canonical_units(native), canonical)
                self.assertAlmostEqual(to_canonical(7.5, native, "daily"), 7.5, places=12)
                self.assertTrue(rule_for(native).is_identity)

    def test_units_strings_are_stripped_before_lookup(self):
        self.assertEqual(canonical_units("  C "), "K")
        self.assertAlmostEqual(to_canonical(0.0, " C", "daily"), 273.15, places=9)


class TestUvIndexIsLeftAlone(unittest.TestCase):
    """``'W m-2 x 40'`` describes the index; the served value *is* the index."""

    def test_uv_index_is_not_divided_by_forty(self):
        # dict_daily_RE.json: ALLSKY_SFC_UV_INDEX units are 'W m-2 x 40'.
        got = to_canonical(8.0, "W m-2 x 40", "daily")
        self.assertAlmostEqual(got, 8.0, places=12)
        self.assertNotAlmostEqual(got, 0.2, delta=1e-6)

    def test_uv_index_is_relabelled_not_called_watts(self):
        self.assertEqual(canonical_units("W m-2 x 40"), "UV index")
        self.assertNotEqual(canonical_units("W m-2 x 40"), "W m-2")

    def test_degree_days_are_left_alone_too(self):
        self.assertEqual(canonical_units("degree-day-c"), "degree-day-c")
        self.assertAlmostEqual(to_canonical(120.0, "degree-day-c", "daily"), 120.0, places=12)


class TestMaskBeforeScale(unittest.TestCase):
    """POWER's -999.0 must be masked *before* any factor touches it."""

    def test_fill_value_becomes_none_not_minus_41625(self):
        got = to_canonical(-999.0, "kW-hr/m^2/day", "daily", fill_value=-999.0)
        self.assertIsNone(got)

    def test_the_number_that_would_appear_if_the_order_were_wrong(self):
        # -999.0 x (1000/24) = -41625.0, which is not obviously wrong on a ramp.
        self.assertAlmostEqual(-999.0 * KWH_M2_DAY_TO_W_M2, -41625.0, places=6)

    def test_neighbours_in_a_series_survive_the_mask(self):
        values = [3.5455, -999.0, 3.6]
        got = convert_series(values, "kW-hr/m^2/day", "daily", fill_value=-999.0)
        self.assertIsNone(got[1])
        self.assertAlmostEqual(got[0], 147.729166, places=5)
        self.assertAlmostEqual(got[2], 150.0, places=9)

    def test_masking_applies_to_offset_rules_as_well(self):
        # -999 C would otherwise read as a perfectly plausible -725.85 K.
        got = convert_series([-0.04, -999.0], "C", "monthly", fill_value=-999.0)
        self.assertAlmostEqual(got[0], 273.11, places=9)
        self.assertIsNone(got[1])

    def test_masking_applies_to_unknown_units_too(self):
        got = convert_series([1.0, -999.0], "furlongs/fortnight", "daily", fill_value=-999.0)
        self.assertEqual(got[0], 1.0)
        self.assertIsNone(got[1])

    def test_none_stays_none(self):
        self.assertIsNone(to_canonical(None, "C", "daily"))
        self.assertEqual(convert_series([None, 0.0], "C", "daily"), [None, 273.15])

    def test_without_a_declared_fill_value_nothing_is_masked(self):
        # The sentinel comes from header.fill_value, never from a hardcoded
        # -999 guess: a legitimate -999 would otherwise be silently dropped.
        got = to_canonical(-999.0, "C", "daily")
        self.assertAlmostEqual(got, -725.85, places=9)


class TestUnknownUnitsPassThrough(unittest.TestCase):
    """POWER serves 150+ parameters per level and adds more. Never guess."""

    def test_unknown_units_leave_the_value_alone(self):
        self.assertEqual(to_canonical(42.0, "sverdrups", "daily"), 42.0)
        self.assertEqual(convert_series([1.0, 2.0], "sverdrups", "daily"), [1.0, 2.0])

    def test_canonical_units_echoes_an_unknown_string(self):
        self.assertEqual(canonical_units("sverdrups"), "sverdrups")
        self.assertIsNone(rule_for("sverdrups"))

    def test_a_near_miss_is_not_matched_to_a_known_rule(self):
        # 'kW-hr/m^2' is not 'kW-hr/m^2/day'; a fuzzy match would scale by 41.7.
        self.assertIsNone(rule_for("kW-hr/m^2"))
        self.assertEqual(to_canonical(3.5455, "kW-hr/m^2", "daily"), 3.5455)

    def test_empty_units_are_unknown_rather_than_dimensionless(self):
        self.assertIsNone(rule_for(""))
        self.assertEqual(to_canonical(1.0, "", "daily"), 1.0)

    def test_unknown_units_at_an_accumulating_temporal_do_not_raise(self):
        self.assertEqual(to_canonical(1.0, "sverdrups", "monthly"), 1.0)


class TestDictionaryCoverage(unittest.TestCase):
    """Every units string POWER serves daily, in all three communities.

    This is the test that catches a new POWER unit: refresh the dictionary
    fixtures, and a units string that :data:`UNIT_RULES` has never seen fails
    here by name instead of silently passing through unconverted in the field.
    """

    def _all_units(self):
        seen: dict[str, set[str]] = {}
        for community, filename in DICTIONARY_FIXTURES:
            for name, entry in load_json(filename).items():
                seen.setdefault(entry["units"], set()).add(f"{community}:{name}")
        return seen

    def test_no_dictionary_units_string_is_unhandled(self):
        unknown = {u: sorted(w)[:3] for u, w in self._all_units().items() if rule_for(u) is None}
        self.assertEqual(unknown, {}, f"units strings absent from UNIT_RULES: {unknown}")

    def test_no_dictionary_units_string_raises_on_conversion(self):
        for native in sorted(self._all_units()):
            with self.subTest(units=native):
                # 'daily' is the temporal all three fixtures were fetched at, so
                # nothing here should need a step length.
                value = to_canonical(1.0, native, "daily", fill_value=-999.0)
                self.assertIsNotNone(value)
                self.assertIsInstance(value, float)
                self.assertTrue(canonical_units(native))
                self.assertIsInstance(describe_conversion(native, "daily"), str)

    def test_the_fixtures_cover_the_units_the_module_claims_to_know(self):
        # Every rule must be justified by a measurement. 'Wh/m^2' is hourly-only
        # and so cannot appear in a daily dictionary -- point_hourly_utc.json is
        # its evidence instead; everything else has to come from these three.
        hourly_only = {"Wh/m^2"}
        covered = set(self._all_units())
        self.assertIn("W m-2", covered)  # SB only, but present
        unjustified = set(known_units()) - covered - hourly_only
        self.assertEqual(unjustified, set(), f"rules with no fixture behind them: {unjustified}")

    def test_all_three_communities_serve_the_same_parameter_count(self):
        # 152 daily parameters each, measured 2026-09-07. If this drifts, the
        # unit coverage above was checked against a stale snapshot.
        for _community, filename in DICTIONARY_FIXTURES:
            with self.subTest(fixture=filename):
                self.assertEqual(len(load_json(filename)), 152)


class TestDescribeConversion(unittest.TestCase):
    """The line written into layer metadata: what was done to these numbers."""

    def test_a_scaled_rule_names_its_factor(self):
        text = describe_conversion("kW-hr/m^2/day", "daily")
        self.assertIn("kW-hr/m^2/day", text)
        self.assertIn("W m-2", text)
        self.assertIn("41.6", text)  # 1000/24 = 41.6667

    def test_the_megajoule_rule_names_its_own_factor(self):
        text = describe_conversion("MJ/m^2/day", "daily")
        self.assertIn("11.57", text)  # 1e6/86400 = 11.5741
        self.assertNotIn("41.6", text)

    def test_an_offset_rule_names_its_offset(self):
        text = describe_conversion("C", "daily")
        self.assertIn("273.15", text)
        self.assertIn("K", text)

    def test_an_identity_rule_says_no_arithmetic_was_done(self):
        text = describe_conversion("m/s", "daily")
        self.assertIn("m s-1", text)
        self.assertNotIn("x ", text)

    def test_an_unknown_units_string_is_reported_as_unconverted(self):
        text = describe_conversion("sverdrups", "daily")
        self.assertIn("sverdrups", text)

    def test_the_hourly_accumulation_factor_is_one_and_reported_as_such(self):
        text = describe_conversion("Wh/m^2", "hourly")
        self.assertIn("Wh/m^2", text)
        self.assertIn("W m-2", text)
        self.assertNotIn("3600", text)


class TestRuleTable(unittest.TestCase):
    """Invariants of the table itself."""

    def test_known_units_is_sorted_and_matches_the_table(self):
        self.assertEqual(list(known_units()), sorted(UNIT_RULES))

    def test_every_rule_declares_a_canonical_string(self):
        for native, rule in UNIT_RULES.items():
            with self.subTest(units=native):
                self.assertTrue(rule.canonical.strip())

    def test_only_the_hourly_accumulation_rule_accumulates(self):
        accumulating = {n for n, r in UNIT_RULES.items() if r.per_accumulation}
        self.assertEqual(accumulating, {"Wh/m^2"})

    def test_is_identity_is_false_when_arithmetic_happens(self):
        self.assertFalse(UNIT_RULES["C"].is_identity)
        self.assertFalse(UNIT_RULES["kW-hr/m^2/day"].is_identity)
        self.assertFalse(UNIT_RULES["Wh/m^2"].is_identity)
        self.assertTrue(UNIT_RULES["W m-2"].is_identity)


if __name__ == "__main__":
    unittest.main()
