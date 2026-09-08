"""The acknowledgement and provenance note that travel with every layer.

Nothing here is decoration. The citation carries the API version, and that
version is **read from each response** because it drifts per endpoint --
measured in one session, daily answered ``v2.9.7`` while monthly answered
``v2.9.8`` (see the two point fixtures' ``header.api``). So the test that
matters is that a different version produces a different string: a hardcoded
version would pass every "does it mention POWER" assertion and still be wrong.

The provenance note exists because "NASA POWER" does not tell a reader whether a
number is a reanalysis or a satellite retrieval, and the answer differs per
parameter inside one layer set.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from nasa_power.core.citation import (
    ACKNOWLEDGEMENT,
    HOMEPAGE,
    build_citation,
    build_provenance_note,
)
from nasa_power.core.decode import read_facts
from tests.unit.support import load_json

ACCESSED = date(2026, 9, 7)


class BuildCitationTests(unittest.TestCase):
    def test_the_live_api_name_and_version_are_interpolated(self) -> None:
        # Read straight off the daily fixture's header.api rather than typed in,
        # so this is the same path the plugin takes.
        facts = read_facts(load_json("point_daily_2param.json"), ("T2M",))
        self.assertEqual("POWER Daily API", facts.api_name)
        self.assertEqual("v2.9.7", facts.api_version)

        citation = build_citation(facts.api_name, facts.api_version, accessed=ACCESSED)
        self.assertIn("POWER Daily API", citation)
        self.assertIn("v2.9.7", citation)
        self.assertIn(ACKNOWLEDGEMENT, citation)
        self.assertIn(HOMEPAGE, citation)

    def test_a_different_version_changes_the_string(self) -> None:
        # The monthly endpoint answered v2.9.8 in the same session the daily
        # endpoint answered v2.9.7. If this assertion can be satisfied by a
        # constant, the version is not really being recorded.
        daily = build_citation("POWER Daily API", "v2.9.7", accessed=ACCESSED)
        monthly = build_citation("POWER Monthly and Annual API", "v2.9.8", accessed=ACCESSED)
        self.assertNotEqual(daily, monthly)
        self.assertIn("v2.9.8", monthly)
        self.assertNotIn("v2.9.7", monthly)

    def test_the_monthly_fixture_reports_a_different_version_than_the_daily_one(
        self,
    ) -> None:
        daily = read_facts(load_json("point_daily_2param.json"))
        monthly = read_facts(load_json("point_monthly_yyyy13.json"))
        self.assertNotEqual(daily.api_version, monthly.api_version)
        self.assertNotEqual(
            build_citation(daily.api_name, daily.api_version, accessed=ACCESSED),
            build_citation(monthly.api_name, monthly.api_version, accessed=ACCESSED),
        )

    def test_the_access_date_is_interpolated(self) -> None:
        # POWER is updated to within days of the present, so *when* a series was
        # pulled is part of identifying it.
        self.assertIn("2026-09-07", build_citation("POWER Daily API", "v2.9.7", accessed=ACCESSED))
        self.assertIn(
            "2001-01-01",
            build_citation("POWER Daily API", "v2.9.7", accessed=date(2001, 1, 1)),
        )

    def test_a_datetime_access_stamp_is_reduced_to_its_date(self) -> None:
        stamp = datetime(2026, 9, 7, 18, 30, tzinfo=timezone.utc)
        self.assertIn("2026-09-07", build_citation("A", "v1", accessed=stamp))
        self.assertNotIn("18:30", build_citation("A", "v1", accessed=stamp))

    def test_the_access_date_defaults_to_today(self) -> None:
        citation = build_citation("POWER Daily API", "v2.9.7")
        self.assertIn(datetime.now(timezone.utc).date().isoformat(), citation)

    def test_parent_datasets_are_named_when_the_response_reported_them(self) -> None:
        facts = read_facts(load_json("point_daily_2param.json"))
        citation = build_citation(
            facts.api_name, facts.api_version, accessed=ACCESSED, sources=facts.sources
        )
        # Rendered as names, not POWER's codes: 'SYN1DEG' means nothing to a
        # reader of a project file.
        self.assertIn("MERRA-2", citation)
        self.assertIn("CERES SYN1deg", citation)

    def test_the_pre_2001_source_is_named_as_srb(self) -> None:
        facts = read_facts(load_json("point_solar_2000.json"))
        self.assertEqual(("SRB",), facts.sources)
        citation = build_citation("POWER Daily API", "v2.9.7", accessed=ACCESSED, sources=facts.sources)
        self.assertIn("NASA/GEWEX SRB", citation)
        self.assertNotIn("CERES SYN1deg", citation)

    def test_the_acknowledgement_survives_a_bare_call(self) -> None:
        # Nothing known about the response: POWER still asks to be acknowledged.
        citation = build_citation(accessed=ACCESSED)
        self.assertIn(ACKNOWLEDGEMENT, citation)
        self.assertIn(HOMEPAGE, citation)
        self.assertNotIn("Served by", citation)


class BuildProvenanceNoteTests(unittest.TestCase):
    def test_both_parent_datasets_are_named(self) -> None:
        note = build_provenance_note()
        self.assertIn("CERES SYN1deg", note)
        self.assertIn("SRB", note)
        self.assertIn("MERRA-2", note)
        self.assertIn("2001-01-01", note)

    def test_it_says_power_is_not_ground_truth(self) -> None:
        # The point of the note: a reanalysis quoted as an observation is a
        # circular model evaluation, and nothing in the numbers says so.
        note = build_provenance_note(("MERRA2",))
        self.assertIn("not ground truth", note)
        self.assertIn("reanalysis", note)

    def test_lst_carries_the_offset_warning(self) -> None:
        # Measured at Boulder: the same irradiance peak sits at ...17 in UTC and
        # ...10 in LST.
        note = build_provenance_note(("SYN1DEG",), time_standard="LST")
        self.assertIn("LST", note)
        self.assertIn("Local Solar Time", note)
        self.assertIn("seven-hour", note)

    def test_utc_does_not(self) -> None:
        note = build_provenance_note(("SYN1DEG",), time_standard="UTC")
        self.assertIn("UTC", note)
        self.assertNotIn("seven-hour", note)
        self.assertNotIn("Local Solar Time", note)

    def test_the_time_standard_is_omitted_when_unknown(self) -> None:
        note = build_provenance_note(("SYN1DEG",))
        self.assertNotIn("Timestamps are", note)

    def test_the_grid_is_named_and_declared_unresampled(self) -> None:
        # POWER does not regrid: solar arrives 1.0 deg and meteorology
        # 0.5 x 0.625 deg for the identical bounding box, so the two are not
        # co-registered and the layer must say which grid it is on.
        note = build_provenance_note(("MERRA2",), grid_label="0.5° x 0.625°")
        self.assertIn("0.5° x 0.625°", note)
        self.assertIn("not resampled", note)

    def test_the_reported_sources_are_echoed(self) -> None:
        note = build_provenance_note(("SRB",))
        self.assertIn("NASA/GEWEX SRB", note)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
