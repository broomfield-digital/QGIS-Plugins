"""Guard the provenance model: which parent produced a number, and on what grid.

Three facts this module has to keep straight, all of them measured against the
live API on 2026-09-07 and all of them invisible in the data itself:

* **Family membership is by parameter name, not by POWER's dictionary type.**
  ``CDD0`` is typed ``RADIATION`` in the parameter dictionary and is plainly
  temperature-derived; the dictionary ``type`` field is a UI category.
* **Solar changes parent on 2001-01-01**, SRB to CERES SYN1deg. A window either
  side of it returns two sources with nothing in the values marking the seam.
  The seam date is cross-checked here against the two solar captures.
* **The two families are not co-registered.** MERRA-2 has a node exactly on
  latitude 40.0; CERES 1.0 deg is centred on half-degrees, so an integer tile
  boundary falls between its cells. That single difference decides whether
  adjacent request tiles share an edge row.
"""

from __future__ import annotations

import math
import unittest
from datetime import date, datetime

from nasa_power.core.provenance import (
    GRIDS,
    MERRA2_GRID,
    RADIATION_RECORD_START,
    SRB_SYN1DEG_TRANSITION,
    SYN1DEG_GRID,
    Family,
    expected_sources,
    family_groups,
    family_of,
    grid_for,
    max_offset,
    snap_to_grid,
    spans_provenance_seam,
)
from tests.unit.support import load_json


def _header_window(payload) -> tuple[date, date]:
    """``header.start``/``header.end`` as dates. Daily headers are YYYYMMDD."""
    header = payload["header"]
    parse = lambda s: datetime.strptime(s, "%Y%m%d").date()  # noqa: E731
    return parse(header["start"]), parse(header["end"])


class FamilyOfTest(unittest.TestCase):
    """Which parent a parameter name predicts."""

    def test_radiative_flux_prefixes_are_radiation(self):
        for parameter in (
            "ALLSKY_SFC_SW_DWN",
            "ALLSKY_SFC_LW_DWN",
            "CLRSKY_SFC_SW_DWN",
            "TOA_SW_DWN",
            "AOD_55",
        ):
            with self.subTest(parameter=parameter):
                self.assertIs(family_of(parameter), Family.RADIATION)

    def test_the_usual_met_parameters_are_meteorology(self):
        for parameter in ("T2M", "RH2M", "WS10M", "PS", "PRECTOTCORR"):
            with self.subTest(parameter=parameter):
                self.assertIs(family_of(parameter), Family.METEOROLOGY)

    def test_sg_parameters_are_solar_geometry(self):
        for parameter in ("SG_DAY_HOURS", "SG_DAY_LENGTH", "SG_NOON", "SG_DEC"):
            with self.subTest(parameter=parameter):
                self.assertIs(family_of(parameter), Family.SOLAR_GEOMETRY)

    def test_degree_day_indices_are_meteorology_despite_powers_own_type(self):
        # POWER's dictionary types CDD0/CDD10/CDD18_3 as RADIATION -- asserted
        # against the real dictionary below -- but their definition is "the
        # daily accumulation of degrees above a threshold when the daily mean
        # TEMPERATURE is above it", so their parent is MERRA-2. Believing the
        # dictionary here would split a request the wrong way and then
        # mislabel the result as CERES.
        for parameter in ("CDD0", "CDD10", "CDD18_3"):
            with self.subTest(parameter=parameter):
                self.assertIs(family_of(parameter), Family.METEOROLOGY)

    def test_the_dictionary_really_does_type_them_radiation(self):
        dictionary = load_json("dict_daily_RE.json")
        for parameter in ("CDD0", "CDD10", "CDD18_3"):
            with self.subTest(parameter=parameter):
                self.assertEqual(dictionary[parameter]["type"], "RADIATION")
                self.assertEqual(dictionary[parameter]["units"], "degree-day-c")
        # ...and is inconsistent about it: the heating counterpart is typed
        # METEOROLOGY for an identically shaped quantity.
        self.assertEqual(dictionary["HDD0"]["type"], "METEOROLOGY")

    def test_solar_geometry_wins_over_a_radiation_prefix(self):
        # SZA matches the radiation prefix list but is computed from geometry.
        self.assertIs(family_of("SZA"), Family.SOLAR_GEOMETRY)

    def test_names_are_normalised_before_matching(self):
        self.assertIs(family_of("  t2m "), Family.METEOROLOGY)
        self.assertIs(family_of("allsky_sfc_sw_dwn"), Family.RADIATION)
        self.assertIs(family_of("cdd18_3"), Family.METEOROLOGY)

    def test_an_unknown_parameter_falls_back_to_meteorology(self):
        # A wrong guess costs a mixed-source response and a QA finding, never a
        # mislabelled layer -- so the fallback is the larger family.
        self.assertIs(family_of("QV2M_SOMETHING_NEW"), Family.METEOROLOGY)


class FamilyGroupsTest(unittest.TestCase):
    """Splitting a mixed request into one request per parent."""

    def test_a_mixed_list_splits_into_exactly_two_groups(self):
        groups = family_groups(
            ["T2M", "ALLSKY_SFC_SW_DWN", "RH2M", "CLRSKY_SFC_SW_DWN"]
        )
        self.assertEqual(len(groups), 2)
        self.assertEqual(set(groups), {Family.METEOROLOGY, Family.RADIATION})

    def test_order_is_preserved_within_each_group(self):
        # The user's parameter order survives the split, so field order in the
        # resulting layer matches what was asked for.
        groups = family_groups(
            ["WS10M", "ALLSKY_SFC_SW_DWN", "T2M", "CLRSKY_SFC_SW_DWN", "RH2M"]
        )
        self.assertEqual(groups[Family.METEOROLOGY], ["WS10M", "T2M", "RH2M"])
        self.assertEqual(
            groups[Family.RADIATION], ["ALLSKY_SFC_SW_DWN", "CLRSKY_SFC_SW_DWN"]
        )

    def test_a_single_family_stays_one_request(self):
        groups = family_groups(["T2M", "RH2M"])
        self.assertEqual(list(groups), [Family.METEOROLOGY])

    def test_all_three_families_split_three_ways(self):
        groups = family_groups(["T2M", "ALLSKY_SFC_SW_DWN", "SG_DAY_LENGTH"])
        self.assertEqual(len(groups), 3)

    def test_nothing_in_nothing_out(self):
        self.assertEqual(family_groups([]), {})


class ProvenanceSeamTest(unittest.TestCase):
    """The SRB-to-CERES boundary, bisected against the live API."""

    def test_the_transition_date(self):
        self.assertEqual(SRB_SYN1DEG_TRANSITION, date(2001, 1, 1))

    def test_the_last_day_before_the_seam_is_srb_only(self):
        self.assertEqual(
            expected_sources(Family.RADIATION, date(2000, 12, 31), date(2000, 12, 31)),
            ["SRB"],
        )

    def test_the_first_day_after_the_seam_is_ceres_only(self):
        self.assertEqual(
            expected_sources(Family.RADIATION, date(2001, 1, 1), date(2001, 1, 1)),
            ["SYN1DEG"],
        )

    def test_a_window_spanning_the_seam_returns_both(self):
        sources = expected_sources(
            Family.RADIATION, date(1998, 1, 1), date(2003, 12, 31)
        )
        self.assertEqual(sources, ["SRB", "SYN1DEG"])
        self.assertTrue(
            spans_provenance_seam(
                Family.RADIATION, date(1998, 1, 1), date(2003, 12, 31)
            )
        )

    def test_windows_entirely_on_one_side_do_not_span(self):
        self.assertFalse(
            spans_provenance_seam(
                Family.RADIATION, date(1990, 1, 1), date(2000, 12, 31)
            )
        )
        self.assertFalse(
            spans_provenance_seam(
                Family.RADIATION, date(2001, 1, 1), date(2024, 1, 1)
            )
        )

    def test_the_prediction_matches_the_captured_pre_seam_response(self):
        # point_solar_2000.json: 2000-12-30..31, header.sources == ['SRB'].
        payload = load_json("point_solar_2000.json")
        start, end = _header_window(payload)
        self.assertEqual(
            expected_sources(Family.RADIATION, start, end), payload["header"]["sources"]
        )
        self.assertEqual(payload["header"]["sources"], ["SRB"])
        self.assertLess(end, SRB_SYN1DEG_TRANSITION)

    def test_the_prediction_matches_the_captured_post_seam_response(self):
        # point_solar_2001.json: 2001-01-01..02, header.sources == ['SYN1DEG'].
        # These two captures are two days apart and disagree about the parent,
        # which is what bisects the seam to 2001-01-01.
        payload = load_json("point_solar_2001.json")
        start, end = _header_window(payload)
        self.assertEqual(
            expected_sources(Family.RADIATION, start, end), payload["header"]["sources"]
        )
        self.assertEqual(payload["header"]["sources"], ["SYN1DEG"])
        self.assertGreaterEqual(start, SRB_SYN1DEG_TRANSITION)

    def test_the_two_captures_request_the_same_parameter_at_the_same_place(self):
        # Otherwise the source difference could be about the parameter or the
        # coordinate rather than about the date.
        before = load_json("point_solar_2000.json")
        after = load_json("point_solar_2001.json")
        self.assertEqual(
            list(before["properties"]["parameter"]),
            list(after["properties"]["parameter"]),
        )
        self.assertEqual(before["geometry"]["coordinates"], after["geometry"]["coordinates"])

    def test_meteorology_never_spans_a_seam(self):
        for start, end in (
            (date(1981, 1, 1), date(2024, 12, 31)),
            (date(2000, 12, 31), date(2001, 1, 1)),
            (date(2010, 6, 1), date(2010, 6, 2)),
        ):
            with self.subTest(start=start):
                self.assertEqual(
                    expected_sources(Family.METEOROLOGY, start, end), ["MERRA2"]
                )
                self.assertFalse(spans_provenance_seam(Family.METEOROLOGY, start, end))

    def test_solar_geometry_is_computed_and_never_spans(self):
        self.assertEqual(
            expected_sources(Family.SOLAR_GEOMETRY, date(1981, 1, 1), date(2024, 1, 1)),
            ["POWER"],
        )
        self.assertFalse(
            spans_provenance_seam(
                Family.SOLAR_GEOMETRY, date(1981, 1, 1), date(2024, 1, 1)
            )
        )

    def test_the_record_start_is_a_separate_fact_from_the_seam(self):
        # Radiation not existing before 1984 and radiation changing parent in
        # 2001 are two different boundaries; merging them loses both.
        self.assertEqual(RADIATION_RECORD_START, date(1984, 1, 1))
        self.assertNotEqual(RADIATION_RECORD_START, SRB_SYN1DEG_TRANSITION)


class GridSnappingTest(unittest.TestCase):
    """Locating the cell a point request is actually answered from."""

    def test_merra2_snaps_boulder_to_its_node(self):
        # 40.02 / 0.5 -> 80.04 -> 80 -> 40.0; -105.27 / 0.625 -> -168.432 ->
        # -168 -> -105.0.
        self.assertEqual(snap_to_grid(Family.METEOROLOGY, 40.02, -105.27), (40.0, -105.0))

    def test_the_snapped_merra2_centre_is_a_cell_the_api_really_returns(self):
        # regional_daily_fc.json is the same MERRA-2 grid served as cell
        # centres. The derived centre must be one of them, or the local
        # arithmetic and POWER's grid disagree.
        collection = load_json("regional_daily_fc.json")
        centres = {
            (feature["geometry"]["coordinates"][1], feature["geometry"]["coordinates"][0])
            for feature in collection["features"]
        }
        self.assertIn(snap_to_grid(Family.METEOROLOGY, 40.02, -105.27), centres)

    def test_the_captured_cell_centres_lie_on_the_merra2_grid(self):
        collection = load_json("regional_daily_fc.json")
        for feature in collection["features"]:
            lon, lat = feature["geometry"]["coordinates"][:2]
            with self.subTest(lat=lat, lon=lon):
                # Every centre is a multiple of the spacing, i.e. the grid is
                # centres_on_multiples, and snapping a centre is a no-op.
                self.assertEqual(snap_to_grid(Family.METEOROLOGY, lat, lon), (lat, lon))

    def test_merra2_has_a_node_exactly_on_latitude_forty(self):
        # This is why the two daily T2M tiles meeting at lat 40.0 both return
        # that row, and a naive mosaic gets 10 rows where there are 9.
        self.assertTrue(MERRA2_GRID.centres_on_multiples)
        self.assertEqual(snap_to_grid(Family.METEOROLOGY, 40.0, -105.0)[0], 40.0)

    def test_ceres_centres_sit_on_half_degrees(self):
        # centres_on_multiples is False, so the rule is
        # (floor(v / step) + 0.5) * step: 40.02 -> (40 + 0.5) * 1.0 = 40.5, and
        # -105.27 -> (-106 + 0.5) * 1.0 = -105.5. Not 39.5, and not 40.0.
        self.assertFalse(SYN1DEG_GRID.centres_on_multiples)
        step = SYN1DEG_GRID.dlat
        self.assertEqual((math.floor(40.02 / step) + 0.5) * step, 40.5)
        self.assertEqual(snap_to_grid(Family.RADIATION, 40.02, -105.27), (40.5, -105.5))

    def test_an_integer_tile_boundary_falls_between_ceres_cells(self):
        # 39.9 and 40.1 land in different cells and neither centre is 40.0, so
        # tiles meeting at lat 40.0 duplicate nothing -- the counter-example to
        # the MERRA-2 pair.
        south = snap_to_grid(Family.RADIATION, 39.9, -105.0)[0]
        north = snap_to_grid(Family.RADIATION, 40.1, -105.0)[0]
        self.assertEqual((south, north), (39.5, 40.5))
        self.assertNotEqual(south, north)

    def test_the_two_families_are_not_co_registered(self):
        # The same click resolves to two different points, one per parent.
        self.assertNotEqual(
            snap_to_grid(Family.METEOROLOGY, 40.02, -105.27),
            snap_to_grid(Family.RADIATION, 40.02, -105.27),
        )
        self.assertNotEqual(MERRA2_GRID.dlon, SYN1DEG_GRID.dlon)

    def test_the_offset_never_exceeds_half_a_cell(self):
        # The marker-vs-data displacement the UI has to disclose: 0.25 deg lat
        # and 0.3125 deg lon for MERRA-2, 0.5 deg for CERES.
        for family in (Family.METEOROLOGY, Family.RADIATION):
            dlat, dlon = max_offset(family)
            for lat_step in range(-360, 361):
                lat = lat_step / 4.0
                lon = -180.0 + (lat_step + 360) * 0.4993
                cell_lat, cell_lon = snap_to_grid(family, lat, lon)
                with self.subTest(family=family, lat=lat):
                    self.assertLessEqual(abs(cell_lat - lat), dlat + 1e-9)
                    self.assertLessEqual(abs(cell_lon - lon), dlon + 1e-9)

    def test_max_offset_is_half_the_spacing(self):
        self.assertEqual(max_offset(Family.METEOROLOGY), (0.25, 0.3125))
        self.assertEqual(max_offset(Family.RADIATION), (0.5, 0.5))

    def test_snapping_is_idempotent(self):
        for family in (Family.METEOROLOGY, Family.RADIATION):
            once = snap_to_grid(family, 40.02, -105.27)
            with self.subTest(family=family):
                self.assertEqual(snap_to_grid(family, *once), once)

    def test_solar_geometry_has_no_grid_and_is_returned_unchanged(self):
        # Computed from the coordinate itself, so there is no cell to snap to
        # and rounding one in would invent a displacement that does not exist.
        self.assertIsNone(grid_for(Family.SOLAR_GEOMETRY))
        self.assertIsNone(GRIDS[Family.SOLAR_GEOMETRY])
        self.assertEqual(snap_to_grid(Family.SOLAR_GEOMETRY, 40.02, -105.27), (40.02, -105.27))
        self.assertEqual(max_offset(Family.SOLAR_GEOMETRY), (0.0, 0.0))

    def test_every_family_has_an_entry_in_the_grid_table(self):
        for family in Family:
            with self.subTest(family=family):
                self.assertIn(family, GRIDS)


if __name__ == "__main__":
    unittest.main()


class SeamBoundaryTest(unittest.TestCase):
    """The exact-boundary cases the two committed captures cannot reach.

    ``point_solar_2000.json`` covers 2000-12-30..31 and ``point_solar_2001.json``
    covers 2001-01-01..02, so both sit wholly on one side. A window whose *end*
    lands exactly on the transition day is the case that distinguishes a correct
    ``end >= SRB_SYN1DEG_TRANSITION`` from an off-by-one ``end >``, and neither
    fixture exercises it.
    """

    def test_a_window_ending_exactly_on_the_transition_day_spans_the_seam(self):
        # 2000-12-31 is SRB and 2001-01-01 is SYN1DEG, so this two-day window
        # genuinely contains both parents. With `end >` it would report SRB
        # alone and the series would be mislabelled rather than flagged.
        start, end = date(2000, 12, 31), date(2001, 1, 1)
        self.assertEqual(
            expected_sources(Family.RADIATION, start, end), ["SRB", "SYN1DEG"]
        )
        self.assertTrue(spans_provenance_seam(Family.RADIATION, start, end))

    def test_a_window_ending_the_day_before_the_transition_does_not(self):
        start, end = date(2000, 12, 30), date(2000, 12, 31)
        self.assertEqual(expected_sources(Family.RADIATION, start, end), ["SRB"])
        self.assertFalse(spans_provenance_seam(Family.RADIATION, start, end))

    def test_a_window_starting_exactly_on_the_transition_day_does_not(self):
        # The mirror image, and the one the `or ["SYN1DEG"]` fallback hides:
        # a single-day window on the seam returns SYN1DEG either way, so it
        # cannot stand in for the test above.
        start, end = date(2001, 1, 1), date(2001, 12, 31)
        self.assertEqual(expected_sources(Family.RADIATION, start, end), ["SYN1DEG"])
        self.assertFalse(spans_provenance_seam(Family.RADIATION, start, end))

    def test_the_seam_day_belongs_to_ceres_not_to_srb(self):
        # Which side the boundary day itself falls on, stated once.
        self.assertEqual(
            expected_sources(
                Family.RADIATION, SRB_SYN1DEG_TRANSITION, SRB_SYN1DEG_TRANSITION
            ),
            ["SYN1DEG"],
        )
