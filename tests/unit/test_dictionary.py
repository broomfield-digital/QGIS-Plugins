"""The parameter dictionary, against the three real community fixtures.

The fact this file exists to protect is the one that rules out a hardcoded
catalogue: **units depend on the community, not only on (parameter, temporal)**.
Daily ``ALLSKY_SFC_SW_DWN`` is ``kW-hr/m^2/day`` under RE, ``MJ/m^2/day`` under
AG and ``W m-2`` under SB, while ``T2M`` is ``C`` under all three -- so a static
table keyed on (parameter, temporal) is wrong the first time a user changes
community, and wrong by a factor of 3.6 rather than visibly.

The rest guards the cache: a dictionary fetch is a 150 KB request that must
happen once a month, not once a dialog, and a half-written cache file must never
be readable as a whole one.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from nasa_power.core.api import build_dictionary_url
from nasa_power.core.dictionary import (
    CACHE_MAX_AGE_SECONDS,
    ParameterInfo,
    dictionary_cache_path,
    filter_parameters,
    group_by_category,
    load_dictionary,
    parse_dictionary,
    unavailable_at,
)
from nasa_power.core.errors import PowerHTTPError, PowerValidationError
from nasa_power.core.fetcher import StubFetcher
from tests.unit.support import load_bytes

RE_URL = build_dictionary_url("RE", "daily")


def _parsed(community: str) -> dict[str, ParameterInfo]:
    return parse_dictionary(load_bytes(f"dict_daily_{community}.json"))


class ParseTests(unittest.TestCase):
    def test_daily_re_dictionary_shape(self) -> None:
        # Measured: 152 entries for daily. Monthly and climatology are 1388 and
        # 1634, because those levels carry _00.._23 hour-of-day variants.
        parameters = _parsed("RE")
        self.assertEqual(152, len(parameters))
        self.assertEqual("C", parameters["T2M"].units)
        self.assertEqual("kW-hr/m^2/day", parameters["ALLSKY_SFC_SW_DWN"].units)
        self.assertEqual("Temperature at 2 Meters", parameters["T2M"].long_name)
        self.assertEqual("METEOROLOGY", parameters["T2M"].category)
        self.assertEqual("RADIATION", parameters["ALLSKY_SFC_SW_DWN"].category)
        self.assertTrue(parameters["T2M"].definition)

    def test_category_is_a_ui_grouping_not_provenance(self) -> None:
        # CDD0 is "Cooling Degree Days Above 0 C" -- typed RADIATION by POWER
        # but plainly derived from temperature. Anything that needs a parent
        # dataset must ask provenance.family_of, not this field.
        parameters = _parsed("RE")
        self.assertEqual("RADIATION", parameters["CDD0"].category)

    def test_non_json_body_is_rejected(self) -> None:
        with self.assertRaises(PowerValidationError):
            parse_dictionary(b"<!DOCTYPE html><html></html>")

    def test_empty_dictionary_is_rejected(self) -> None:
        with self.assertRaises(PowerValidationError):
            parse_dictionary(b"{}")


class CommunityUnitsTests(unittest.TestCase):
    """The measured fact that forces a fetched, community-keyed dictionary."""

    def test_radiation_units_differ_per_community(self) -> None:
        units = {c: _parsed(c)["ALLSKY_SFC_SW_DWN"].units for c in ("RE", "AG", "SB")}
        self.assertEqual(
            {"RE": "kW-hr/m^2/day", "AG": "MJ/m^2/day", "SB": "W m-2"}, units
        )
        self.assertEqual(3, len(set(units.values())))

    def test_temperature_units_do_not(self) -> None:
        units = {c: _parsed(c)["T2M"].units for c in ("RE", "AG", "SB")}
        self.assertEqual({"RE": "C", "AG": "C", "SB": "C"}, units)

    def test_the_three_communities_serve_the_same_parameters(self) -> None:
        # Same names, different units: the difference is entirely in the units
        # column, which is exactly what makes it easy to miss.
        names = [set(_parsed(c)) for c in ("RE", "AG", "SB")]
        self.assertEqual(names[0], names[1])
        self.assertEqual(names[0], names[2])


class HourVariantTests(unittest.TestCase):
    def test_hour_variants_are_recognised(self) -> None:
        for name in ("AIRMASS_00", "AIRMASS_12", "AIRMASS_23", "T2M_00"):
            with self.subTest(name=name):
                info = ParameterInfo(name=name, units="", long_name="")
                self.assertTrue(info.is_hour_variant)
                self.assertEqual(name.rsplit("_", 1)[0], info.base_name)

    def test_a_two_digit_suffix_above_23_is_not_an_hour(self) -> None:
        # AOD_55 and AOD_84 are aerosol optical depth at 0.55 and 0.84 um, real
        # daily RADIATION parameters. A bare `_\d\d$` rule reads them as hours
        # 55 and 84 and hides them from the default parameter list.
        parameters = _parsed("RE")
        for name in ("AOD_55", "AOD_84"):
            with self.subTest(name=name):
                self.assertFalse(parameters[name].is_hour_variant)
                self.assertEqual(name, parameters[name].base_name)
        self.assertIn("AOD_55", {i.name for i in filter_parameters(parameters)})

    def test_a_base_parameter_is_its_own_base_name(self) -> None:
        info = ParameterInfo(name="AIRMASS", units="", long_name="")
        self.assertFalse(info.is_hour_variant)
        self.assertEqual("AIRMASS", info.base_name)


class FilterTests(unittest.TestCase):
    def _synthetic(self) -> dict[str, ParameterInfo]:
        return {
            name: ParameterInfo(
                name=name, units="dimensionless", long_name="Airmass", category="RADIATION"
            )
            for name in ("AIRMASS", "AIRMASS_00", "AIRMASS_12", "AIRMASS_23")
        }

    def test_hour_variants_are_hidden_by_default(self) -> None:
        # Unfiltered, the monthly list is 1388 entries and the dropdown is
        # unusable; the variants are what the extra 1200 are.
        self.assertEqual(["AIRMASS"], [i.name for i in filter_parameters(self._synthetic())])

    def test_hour_variants_are_available_on_request(self) -> None:
        shown = filter_parameters(self._synthetic(), include_hour_variants=True)
        self.assertEqual(
            ["AIRMASS", "AIRMASS_00", "AIRMASS_12", "AIRMASS_23"], [i.name for i in shown]
        )

    def test_filter_by_category(self) -> None:
        parameters = _parsed("RE")
        radiation = filter_parameters(parameters, categories=["RADIATION"])
        self.assertEqual(60, len(radiation))  # measured against dict_daily_RE
        self.assertEqual({"RADIATION"}, {i.category for i in radiation})
        self.assertIn("ALLSKY_SFC_SW_DWN", {i.name for i in radiation})

    def test_filter_by_category_is_case_insensitive(self) -> None:
        parameters = _parsed("RE")
        self.assertEqual(
            len(filter_parameters(parameters, categories=["RADIATION"])),
            len(filter_parameters(parameters, categories=["radiation"])),
        )

    def test_filter_by_search_matches_code_and_long_name(self) -> None:
        parameters = _parsed("RE")
        by_long_name = filter_parameters(parameters, search="albedo")
        self.assertIn("ALLSKY_SRF_ALB", {i.name for i in by_long_name})
        for info in by_long_name:
            self.assertTrue(
                "albedo" in info.name.lower() or "albedo" in info.long_name.lower()
            )

        by_code = filter_parameters(parameters, search="prectot")
        self.assertIn("PRECTOTCORR", {i.name for i in by_code})
        self.assertLess(len(by_code), len(parameters))

    def test_filters_combine(self) -> None:
        parameters = _parsed("RE")
        both = filter_parameters(parameters, categories=["METEOROLOGY"], search="wind")
        self.assertTrue(both)
        for info in both:
            self.assertEqual("METEOROLOGY", info.category)

    def test_results_are_sorted_by_category_then_name(self) -> None:
        shown = filter_parameters(_parsed("RE"))
        self.assertEqual(sorted(shown, key=lambda i: (i.category, i.name)), shown)

    def test_group_by_category_partitions_the_list(self) -> None:
        shown = filter_parameters(_parsed("RE"))
        groups = group_by_category(shown)
        self.assertEqual(
            {"RADIATION", "METEOROLOGY", "HYDROLOGY", "SOLAR-GEOMETRY"}, set(groups)
        )
        self.assertEqual(len(shown), sum(len(v) for v in groups.values()))
        for category, members in groups.items():
            self.assertEqual({category}, {i.category for i in members})

    def test_group_by_category_names_the_uncategorised(self) -> None:
        groups = group_by_category([ParameterInfo(name="X", units="", long_name="")])
        self.assertEqual(["OTHER"], list(groups))


class LoadDictionaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache_dir = Path(self._tmp.name)
        self.body = load_bytes("dict_daily_RE.json")
        self.fetcher = StubFetcher({RE_URL: self.body})

    @property
    def path(self) -> Path:
        return dictionary_cache_path(self.cache_dir, "RE", "daily")

    def test_first_call_fetches_and_writes_the_cache(self) -> None:
        parameters, from_cache = load_dictionary("RE", "daily", self.cache_dir, self.fetcher)
        self.assertFalse(from_cache)
        self.assertEqual(152, len(parameters))
        self.assertEqual([RE_URL], self.fetcher.calls)
        self.assertTrue(self.path.exists())
        self.assertEqual(self.body, self.path.read_bytes())

    def test_second_call_is_served_from_cache_without_the_network(self) -> None:
        load_dictionary("RE", "daily", self.cache_dir, self.fetcher)
        parameters, from_cache = load_dictionary("RE", "daily", self.cache_dir, self.fetcher)
        self.assertTrue(from_cache)
        self.assertEqual(152, len(parameters))
        # StubFetcher records every call, so "no request was made" is testable
        # rather than merely intended.
        self.assertEqual([RE_URL], self.fetcher.calls)

    def test_force_refetches_a_fresh_cache(self) -> None:
        load_dictionary("RE", "daily", self.cache_dir, self.fetcher)
        _parameters, from_cache = load_dictionary(
            "RE", "daily", self.cache_dir, self.fetcher, force=True
        )
        self.assertFalse(from_cache)
        self.assertEqual([RE_URL, RE_URL], self.fetcher.calls)

    def test_a_stale_cache_still_loads_when_there_is_no_fetcher(self) -> None:
        # Offline with a month-old cache is the normal case for a plugin that
        # opens without a network; a stale list beats an empty combo box.
        load_dictionary("RE", "daily", self.cache_dir, self.fetcher)
        stamp = self.path.stat().st_mtime + CACHE_MAX_AGE_SECONDS * 10
        parameters, from_cache = load_dictionary(
            "RE", "daily", self.cache_dir, fetcher=None, now=stamp
        )
        self.assertTrue(from_cache)
        self.assertEqual(152, len(parameters))

    def test_a_stale_cache_is_refetched_when_there_is_a_fetcher(self) -> None:
        load_dictionary("RE", "daily", self.cache_dir, self.fetcher)
        stamp = self.path.stat().st_mtime + CACHE_MAX_AGE_SECONDS * 10
        _parameters, from_cache = load_dictionary(
            "RE", "daily", self.cache_dir, self.fetcher, now=stamp
        )
        self.assertFalse(from_cache)
        self.assertEqual([RE_URL, RE_URL], self.fetcher.calls)

    def test_no_cache_and_no_fetcher_raises(self) -> None:
        with self.assertRaises(PowerValidationError):
            load_dictionary("RE", "daily", self.cache_dir, fetcher=None)

    def test_a_failed_fetch_leaves_no_partial_and_keeps_the_old_cache(self) -> None:
        # The write is temp-file-then-rename, so a failure mid-refresh must
        # neither truncate the good cache nor leave a .partial that a later
        # glob could pick up as a dictionary.
        load_dictionary("RE", "daily", self.cache_dir, self.fetcher)

        class FailingFetcher:
            def fetch(self, url: str, timeout: float = 60.0) -> bytes:
                raise PowerHTTPError(503, "", url)

        with self.assertRaises(PowerHTTPError):
            load_dictionary("RE", "daily", self.cache_dir, FailingFetcher(), force=True)
        self.assertEqual([], list(self.cache_dir.rglob("*.partial")))
        self.assertEqual(self.body, self.path.read_bytes())

    def test_an_unparseable_response_leaves_no_partial(self) -> None:
        load_dictionary("RE", "daily", self.cache_dir, self.fetcher)
        garbage = StubFetcher({RE_URL: b"<!DOCTYPE html><html>404</html>"})
        with self.assertRaises(PowerValidationError):
            load_dictionary("RE", "daily", self.cache_dir, garbage, force=True)
        self.assertEqual([], list(self.cache_dir.rglob("*.partial")))
        self.assertEqual(self.body, self.path.read_bytes())

    def test_a_corrupt_cache_is_replaced_when_a_fetcher_is_available(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"{ truncated")
        parameters, from_cache = load_dictionary(
            "RE", "daily", self.cache_dir, self.fetcher
        )
        self.assertFalse(from_cache)
        self.assertEqual(152, len(parameters))

    def test_a_corrupt_cache_with_no_fetcher_raises(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"{ truncated")
        with self.assertRaises(PowerValidationError):
            load_dictionary("RE", "daily", self.cache_dir, fetcher=None)

    def test_communities_get_separate_cache_files(self) -> None:
        # One file per (community, temporal): the units differ, so sharing one
        # would serve AG units to an RE request.
        self.assertNotEqual(
            dictionary_cache_path(self.cache_dir, "RE", "daily"),
            dictionary_cache_path(self.cache_dir, "AG", "daily"),
        )
        self.assertNotEqual(
            dictionary_cache_path(self.cache_dir, "RE", "daily"),
            dictionary_cache_path(self.cache_dir, "RE", "monthly"),
        )

    def test_the_cached_bytes_are_the_response_verbatim(self) -> None:
        load_dictionary("RE", "daily", self.cache_dir, self.fetcher)
        self.assertEqual(json.loads(self.body), json.loads(self.path.read_bytes()))


class UnavailableAtTests(unittest.TestCase):
    def test_daily_aggregates_do_not_exist_hourly(self) -> None:
        # Measured: an hourly request for T2M_MAX is a 422.
        self.assertEqual(["T2M_MAX"], unavailable_at(["T2M_MAX"], "hourly"))
        self.assertEqual([], unavailable_at(["T2M_MAX"], "daily"))

    def test_ordinary_parameters_are_available_hourly(self) -> None:
        self.assertEqual([], unavailable_at(["T2M", "ALLSKY_SFC_SW_DWN"], "hourly"))

    def test_the_check_is_case_insensitive(self) -> None:
        self.assertEqual(["t2m_max"], unavailable_at(["t2m_max"], "hourly"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
