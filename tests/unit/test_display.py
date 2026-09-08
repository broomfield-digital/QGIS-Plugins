"""Layer names that survive the QGIS layer tree.

The failure this file guards against is silent and specific: POWER's
``longname`` for ``ALLSKY_SFC_SW_DWN`` is "All Sky Surface Shortwave Downward
Irradiance" (45 characters) and for ``CLRSKY_SFC_SW_DWN`` it is the same with
"Clear" in front. A layer tree truncates both to "All Sky Surface Shortwave
Downwa..." and "Clear Sky Surface Shortwave Down..." -- and the part it keeps is
the part they share. Two layers that differ by a factor of two in value then
look like the same layer twice.

So the tests are: nothing in the table is too long to display, and all-sky and
clear-sky stay distinguishable in the first characters a user actually sees --
checked against every such pair in the real daily dictionary, not just the
curated ones.
"""

from __future__ import annotations

import unittest

from nasa_power.core.dictionary import parse_dictionary
from nasa_power.core.display import (
    DISPLAY_NAMES,
    MAX_DISPLAY_NAME,
    VARIABLE_CLASSES,
    display_name,
    family_label,
    layer_name,
    shorten,
    variable_class,
)
from tests.unit.support import load_bytes

#: POWER's own longname for ALLSKY_SFC_SW_DWN: 45 characters, against a
#: MAX_DISPLAY_NAME of 32.
ALLSKY_LONGNAME = "All Sky Surface Shortwave Downward Irradiance"

#: How much of a layer name is legible in the tree at a normal panel width.
#: Names that agree over this many characters are the same name to a user.
VISIBLE_PREFIX = 16


class DisplayNameTableTests(unittest.TestCase):
    def test_every_curated_name_fits_and_is_not_empty(self) -> None:
        for parameter, name in DISPLAY_NAMES.items():
            with self.subTest(parameter=parameter):
                self.assertTrue(name.strip(), parameter)
                self.assertLessEqual(len(name), MAX_DISPLAY_NAME, name)

    def test_curated_names_are_unique(self) -> None:
        # Two parameters sharing a display name would produce two layers with
        # the same tree entry, which is the failure this table exists to stop.
        self.assertEqual(len(DISPLAY_NAMES), len(set(DISPLAY_NAMES.values())))

    def test_curated_all_sky_and_clear_sky_stay_distinguishable(self) -> None:
        pairs = [
            (name, "CLRSKY" + name[6:])
            for name in DISPLAY_NAMES
            if name.startswith("ALLSKY_") and "CLRSKY" + name[6:] in DISPLAY_NAMES
        ]
        self.assertTrue(pairs)
        for all_sky, clear_sky in pairs:
            with self.subTest(pair=(all_sky, clear_sky)):
                self.assertNotEqual(
                    display_name(all_sky)[:VISIBLE_PREFIX],
                    display_name(clear_sky)[:VISIBLE_PREFIX],
                )

    def test_shortened_longnames_stay_distinguishable(self) -> None:
        # The harder case: parameters with no curated entry, shortened
        # mechanically. Checked over all 13 ALLSKY/CLRSKY pairs in the real
        # daily dictionary.
        parameters = parse_dictionary(load_bytes("dict_daily_RE.json"))
        pairs = [
            (name, "CLRSKY" + name[6:])
            for name in parameters
            if name.startswith("ALLSKY_") and "CLRSKY" + name[6:] in parameters
        ]
        self.assertEqual(13, len(pairs))
        for all_sky, clear_sky in pairs:
            with self.subTest(pair=(all_sky, clear_sky)):
                short_all = shorten(parameters[all_sky].long_name)
                short_clear = shorten(parameters[clear_sky].long_name)
                self.assertNotEqual(short_all[:VISIBLE_PREFIX], short_clear[:VISIBLE_PREFIX])

    def test_shortening_the_whole_dictionary_fits(self) -> None:
        parameters = parse_dictionary(load_bytes("dict_daily_RE.json"))
        for info in parameters.values():
            with self.subTest(parameter=info.name):
                self.assertLessEqual(len(shorten(info.long_name)), MAX_DISPLAY_NAME)


class ShortenTests(unittest.TestCase):
    def test_the_full_length_longname_is_abbreviated_not_clipped(self) -> None:
        self.assertEqual(45, len(ALLSKY_LONGNAME))
        short = shorten(ALLSKY_LONGNAME)
        self.assertLessEqual(len(short), MAX_DISPLAY_NAME)
        # A bare truncation loses "Shortwave", which is the word that
        # distinguishes it from the longwave parameter beside it.
        self.assertIn("Shortwave", short)
        self.assertNotIn("…", short)
        self.assertFalse(ALLSKY_LONGNAME.startswith(short))

    def test_a_short_name_is_returned_unchanged(self) -> None:
        self.assertEqual("Cloud Amount", shorten("Cloud Amount"))

    def test_whitespace_is_normalised(self) -> None:
        self.assertEqual("Cloud Amount", shorten("  Cloud   Amount\n"))

    def test_an_unabbreviable_name_is_truncated_with_an_ellipsis(self) -> None:
        long = "Zeta Quux Frobnicator Widget Assembly Indicator"
        short = shorten(long)
        self.assertLessEqual(len(short), MAX_DISPLAY_NAME)
        self.assertTrue(short.endswith("…"))

    def test_the_limit_is_honoured(self) -> None:
        self.assertLessEqual(len(shorten(ALLSKY_LONGNAME, limit=20)), 20)


class DisplayNameTests(unittest.TestCase):
    def test_the_curated_table_wins(self) -> None:
        self.assertEqual(
            "Surface Downwelling Shortwave",
            display_name("ALLSKY_SFC_SW_DWN", ALLSKY_LONGNAME),
        )

    def test_an_unknown_parameter_falls_back_to_its_shortened_longname(self) -> None:
        name = display_name("ZZZ_MADE_UP", "All Sky Surface Shortwave Downward Irradiance")
        self.assertEqual(shorten(ALLSKY_LONGNAME), name)
        self.assertLessEqual(len(name), MAX_DISPLAY_NAME)

    def test_an_unknown_parameter_with_no_longname_falls_back_to_its_code(self) -> None:
        self.assertEqual("ZZZ_MADE_UP", display_name("ZZZ_MADE_UP"))

    def test_a_name_is_never_empty(self) -> None:
        # A layer with no name is unusable in the tree, so every fallback path
        # must produce something.
        for parameter, long_name in (
            ("T2M", ""),
            ("ZZZ", ""),
            ("ZZZ", "Some Long Name"),
            (" t2m ", ""),
        ):
            with self.subTest(parameter=parameter, long_name=long_name):
                self.assertTrue(display_name(parameter, long_name).strip())

    def test_the_lookup_is_case_and_whitespace_insensitive(self) -> None:
        self.assertEqual(display_name("T2M"), display_name(" t2m "))


class VariableClassTests(unittest.TestCase):
    def test_known_parameters(self) -> None:
        cases = {
            ("ALLSKY_SFC_SW_DWN", "kW-hr/m^2/day"): "irradiance",
            ("T2M", "C"): "temperature",
            ("PRECTOTCORR", "mm/day"): "precipitation",
            ("WS10M", "m/s"): "wind",
            ("PS", "kPa"): "pressure",
            ("RH2M", "%"): "humidity",
            ("WD10M", "Degrees"): "direction",
            ("GWETTOP", "1"): "fraction",
        }
        for (parameter, units), expected in cases.items():
            with self.subTest(parameter=parameter):
                self.assertEqual(expected, variable_class(parameter, units))

    def test_units_decide_when_the_name_says_nothing(self) -> None:
        # The dictionary may not have been fetched, but a response always
        # carries units -- so an unrecognised code still styles correctly.
        cases = {
            "W m-2": "irradiance",
            "MJ/m^2/day": "irradiance",
            "Wh/m^2": "irradiance",
            "C": "temperature",
            "K": "temperature",
            "mm/day": "precipitation",
            "m/s": "wind",
            "kPa": "pressure",
            "%": "humidity",
            "Degrees": "direction",
        }
        for units, expected in cases.items():
            with self.subTest(units=units):
                self.assertEqual(expected, variable_class("ZZZ_MADE_UP", units))

    def test_hourly_irradiance_units_are_still_irradiance(self) -> None:
        # Hourly ALLSKY_SFC_SW_DWN comes back as Wh/m^2, not kW-hr/m^2/day.
        self.assertEqual("irradiance", variable_class("ALLSKY_SFC_SW_DWN", "Wh/m^2"))

    def test_an_unclassifiable_parameter_is_other(self) -> None:
        self.assertEqual("other", variable_class("ZZZ_MADE_UP", ""))

    def test_every_result_is_a_declared_class(self) -> None:
        parameters = parse_dictionary(load_bytes("dict_daily_RE.json"))
        for info in parameters.values():
            with self.subTest(parameter=info.name):
                self.assertIn(variable_class(info.name, info.units), VARIABLE_CLASSES)


class LayerNameTests(unittest.TestCase):
    def test_the_name_carries_what_the_metadata_panel_never_gets_read_for(self) -> None:
        name = layer_name("T2M", "C", "daily", "utc")
        self.assertIn(display_name("T2M"), name)
        # "[C]", not "C": a bare "C" is satisfied by the C in "UTC", so this
        # assertion used to pass with the units dropped altogether.
        self.assertIn("[C]", name)
        self.assertIn("daily", name)
        self.assertIn("UTC", name)  # upper-cased, so LST cannot be mistaken for lst

    def test_the_time_standard_is_always_visible(self) -> None:
        # The whole point: a ~7 h phase error must be readable off the tree.
        self.assertIn("LST", layer_name("T2M", "C", "hourly", "LST"))
        self.assertNotIn("LST", layer_name("T2M", "C", "hourly", "UTC"))

    def test_sources_and_grid_are_appended_when_known(self) -> None:
        name = layer_name(
            "T2M",
            "C",
            "daily",
            "UTC",
            sources=("MERRA2",),
            grid_label="0.5° x 0.625°",
        )
        self.assertIn("MERRA-2", name)
        self.assertIn("0.5°", name)

    def test_the_warning_marker_appears_only_when_asked(self) -> None:
        warned = layer_name("T2M", "C", "daily", "UTC", warn=True)
        plain = layer_name("T2M", "C", "daily", "UTC")
        self.assertTrue(warned.startswith("⚠ "))
        self.assertFalse(plain.startswith("⚠"))
        self.assertNotIn("⚠", plain)
        self.assertEqual(plain, warned[2:])

    def test_an_unknown_parameter_still_gets_a_layer_name(self) -> None:
        self.assertTrue(layer_name("ZZZ_MADE_UP", "", "", "").strip())


class FamilyLabelTests(unittest.TestCase):
    def test_the_two_halves_of_power_are_named(self) -> None:
        self.assertIn("CERES", family_label("ALLSKY_SFC_SW_DWN"))
        self.assertIn("MERRA-2", family_label("T2M"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class CuratedTableIsActuallyConsultedTest(unittest.TestCase):
    """``display_name`` must read the curated table, not just possess one.

    The tests above check that DISPLAY_NAMES holds distinguishable strings by
    reading the dict directly. That leaves the lookup itself unguarded: a
    ``display_name`` that ignored the table entirely and always fell through to
    ``shorten(long_name)`` would satisfy every one of them.
    """

    def test_a_curated_parameter_returns_its_curated_name(self):
        self.assertEqual(display_name("T2M"), "2 m Temperature")
        self.assertEqual(
            display_name("ALLSKY_SFC_SW_DWN"), "Surface Downwelling Shortwave"
        )

    def test_the_curated_name_beats_the_longname_from_the_response(self):
        # POWER's own longname for this parameter is the 45-character title the
        # module exists to avoid, so the curated entry has to win.
        self.assertEqual(
            display_name("ALLSKY_SFC_SW_DWN", ALLSKY_LONGNAME),
            DISPLAY_NAMES["ALLSKY_SFC_SW_DWN"],
        )
        self.assertNotEqual(display_name("ALLSKY_SFC_SW_DWN", ALLSKY_LONGNAME), ALLSKY_LONGNAME)

    def test_the_lookup_is_case_and_whitespace_insensitive(self):
        for key in ("t2m", " T2M ", "T2m"):
            with self.subTest(key=key):
                self.assertEqual(display_name(key), DISPLAY_NAMES["T2M"])

    def test_every_curated_name_is_reachable_through_the_lookup(self):
        for parameter, expected in DISPLAY_NAMES.items():
            with self.subTest(parameter=parameter):
                self.assertEqual(display_name(parameter), expected)

    def test_the_curated_pair_stays_distinct_through_the_lookup(self):
        # The whole point, asserted on the path the layer tree actually uses.
        self.assertNotEqual(
            display_name("ALLSKY_SFC_SW_DWN", ALLSKY_LONGNAME),
            display_name("CLRSKY_SFC_SW_DWN", "Clear Sky Surface Shortwave Downward Irradiance"),
        )


class LayerNameCarriesUnitsTest(unittest.TestCase):
    """Units are in the layer name because the metadata panel is never read.

    Asserted on the bracketed token: a bare ``assertIn("C", name)`` passes on
    the "C" in "UTC", so dropping units entirely would go unnoticed.
    """

    def test_the_units_appear_bracketed(self):
        self.assertIn("[C]", layer_name("T2M", "C", "daily", "UTC"))
        self.assertIn(
            "[W m-2]", layer_name("ALLSKY_SFC_SW_DWN", "W m-2", "daily", "UTC")
        )

    def test_a_layer_with_no_units_gets_no_empty_brackets(self):
        self.assertNotIn("[", layer_name("T2M", "", "daily", "UTC"))

    def test_the_units_shown_are_the_ones_passed_in(self):
        # Native or canonical is the caller's choice; the name must not
        # second-guess it, or the label will contradict the values.
        self.assertIn("[kW-hr/m^2/day]", layer_name("ALLSKY_SFC_SW_DWN", "kW-hr/m^2/day", "daily", "UTC"))
        self.assertNotIn("[W m-2]", layer_name("ALLSKY_SFC_SW_DWN", "kW-hr/m^2/day", "daily", "UTC"))
