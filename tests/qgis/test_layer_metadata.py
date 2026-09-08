"""Guard what a POWER layer can say about itself once the plugin is gone.

A layer outlives the fetch: it gets saved into a project, exported, mailed to a
colleague. By then the dock that explained it is closed, so everything a reader
needs has to be inside ``QgsLayerMetadata``. These tests build that record from
the committed captures in ``tests/fixtures`` -- parsed through
:mod:`nasa_power.core.decode` and judged by :mod:`nasa_power.core.qa`, never
hand-written -- and then assert the four things a reader actually uses:

* **every QA finding is a Constraint whose ``type`` is the severity label**, so
  the metadata panel groups Errors above Warnings above Infos;
* **every request URL is a Link, verbatim**, because a paraphrase of a query is
  not a query -- this is what makes the layer reproducible six months later;
* **history carries the numbers**: the API version *read from the response*,
  the fill value, and each unit conversion **with its factor**. "Converted to
  W m-2" is useless to a reader re-deriving the values; "x 41.6667" is not;
* **rights carries the live API version**, not a hardcoded one. POWER's version
  drifts per endpoint -- measured 2026-09-07, daily answered v2.9.7 while
  monthly answered v2.9.8 in the same session.

Nothing here asserts on message prose. The assertions are counts, enum labels,
URLs and numbers.
"""

from __future__ import annotations

import tempfile
from dataclasses import replace
from datetime import date
from pathlib import Path

from qgis.core import QgsLayerMetadata, QgsVectorLayer

from nasa_power.core.api import PowerRequest
from nasa_power.core.citation import build_citation, build_provenance_note
from nasa_power.core.decode import ResponseFacts, parse_point_response
from nasa_power.core.qa import Level, QaFinding, QaReport, postfetch, preflight
from nasa_power.qgis_bridge.layer_metadata import apply_metadata, build_metadata
from tests.qgis.qgis_case import QgisTestCase, load_json

DAILY_2PARAM = "point_daily_2param.json"
PARTIAL_1983 = "point_1983_partial.json"
MONTHLY = "point_monthly_yyyy13.json"
HOURLY_UTC = "point_hourly_utc.json"

#: The URL each fixture was captured from, so a link assertion compares against
#: the real query rather than against something invented here.
_MANIFEST = load_json("MANIFEST.json")


def url_of(fixture: str) -> str:
    return _MANIFEST[fixture]["url"]


def parsed(
    fixture: str,
    *,
    requested: tuple[str, ...],
    temporal: str,
    start: str,
    end: str,
    site: str = "boulder",
) -> tuple[ResponseFacts, QaReport]:
    """``(facts, report)`` for one captured response, the way a fetch makes them."""
    url = url_of(fixture)
    observations, facts = parse_point_response(
        load_json(fixture), temporal=temporal, requested=requested, site=site, url=url
    )
    request = PowerRequest(
        url=url,
        temporal=temporal,
        mode="point",
        params=requested,
        community="RE",
        start=start,
        end=end,
        site=site,
    )
    return facts, postfetch(request, facts, observations)


def daily_2param() -> tuple[ResponseFacts, QaReport]:
    return parsed(
        DAILY_2PARAM,
        requested=("T2M", "ALLSKY_SFC_SW_DWN"),
        temporal="daily",
        start="20240201",
        end="20240203",
    )


def partial_1983() -> tuple[ResponseFacts, QaReport]:
    return parsed(
        PARTIAL_1983,
        requested=("T2M", "ALLSKY_SFC_SW_DWN"),
        temporal="daily",
        start="19830601",
        end="19830603",
    )


def monthly() -> tuple[ResponseFacts, QaReport]:
    return parsed(MONTHLY, requested=("T2M",), temporal="monthly", start="2020", end="2021")


def hourly() -> tuple[ResponseFacts, QaReport]:
    return parsed(
        HOURLY_UTC,
        requested=("ALLSKY_SFC_SW_DWN",),
        temporal="hourly",
        start="20240601",
        end="20240601",
    )


def history_for(md: QgsLayerMetadata, parameter: str) -> str:
    """The one history item describing what was done to ``parameter``."""
    prefix = f"{parameter}:"
    lines = [h for h in md.history() if h.startswith(prefix)]
    return lines[0] if lines else ""


class RecordFieldsTests(QgisTestCase):
    """The Dublin-Core-shaped fields a metadata panel shows first."""

    def test_core_fields(self) -> None:
        facts, report = daily_2param()
        md = build_metadata(
            facts,
            report,
            title="ALLSKY_SFC_SW_DWN (W m-2) daily UTC",
            parameters=["T2M", "ALLSKY_SFC_SW_DWN"],
            temporal="daily",
            urls=[url_of(DAILY_2PARAM)],
        )

        self.assertEqual(md.title(), "ALLSKY_SFC_SW_DWN (W m-2) daily UTC")
        self.assertEqual(md.identifier(), "nasa-power:daily:T2M,ALLSKY_SFC_SW_DWN")
        self.assertEqual(md.type(), "dataset")
        self.assertEqual(md.language(), "ENG")
        self.assertEqual(md.encoding(), "UTF-8")

    def test_crs_is_wgs84(self) -> None:
        # POWER serves lat/lon degrees and nothing here reprojects, so the
        # record must say EPSG:4326 rather than inherit the project CRS.
        facts, report = daily_2param()
        md = build_metadata(
            facts, report, title="t", parameters=["T2M"], temporal="daily"
        )
        self.assertTrue(md.crs().isValid())
        self.assertEqual(md.crs().authid(), "EPSG:4326")

    def test_abstract_is_the_provenance_note_for_these_facts(self) -> None:
        # Equality against the ring-1 function rather than "is not empty":
        # the abstract is where "POWER is not ground truth" and the parent
        # datasets live, and any constant string satisfies non-emptiness while
        # telling the reader nothing about this particular fetch.
        facts, report = daily_2param()
        md = build_metadata(
            facts, report, title="t", parameters=["T2M"], temporal="daily"
        )
        self.assertEqual(
            md.abstract(),
            build_provenance_note(facts.sources, time_standard=facts.time_standard),
        )
        # And the facts really are distinctive: the same note for a response
        # with no sources and no time standard is a different string.
        self.assertNotEqual(md.abstract(), build_provenance_note())

    def test_the_grid_label_reaches_the_abstract(self) -> None:
        # Regional fetches serve a 0.5 x 0.625 deg grid and the plugin does not
        # resample; a reader who cannot see the native grid in the record will
        # read the cell centres as measurement locations.
        facts, report = daily_2param()
        plain = build_metadata(
            facts, report, title="t", parameters=["T2M"], temporal="daily"
        )
        labelled = build_metadata(
            facts,
            report,
            title="t",
            parameters=["T2M"],
            temporal="daily",
            grid_label="0.5 x 0.625 deg",
        )
        self.assertIn("0.5 x 0.625 deg", labelled.abstract())
        self.assertNotIn("0.5 x 0.625 deg", plain.abstract())

    def test_keywords_carry_the_iso_topic_category(self) -> None:
        # gmd:topicCategory is what a catalogue harvests. Without it the layer
        # is undiscoverable in exactly the systems metadata exists for.
        facts, report = daily_2param()
        md = build_metadata(
            facts, report, title="t", parameters=["T2M"], temporal="daily"
        )
        self.assertEqual(
            md.keywords(),
            {"gmd:topicCategory": ["climatologyMeteorologyAtmosphere"]},
        )


class ConstraintTests(QgisTestCase):
    """One Constraint per finding, typed by severity."""

    def assert_findings_map_to_constraints(
        self, md: QgsLayerMetadata, report: QaReport
    ) -> None:
        constraints = md.constraints()
        self.assertEqual(len(constraints), len(report.findings))
        for constraint, finding in zip(constraints, report.findings):
            # `type` is the severity label -- "Error"/"Warning"/"Info" -- which
            # is what the metadata panel groups on.
            self.assertEqual(constraint.type, finding.level.label)
            self.assertTrue(constraint.constraint.strip())

    def test_daily_findings_all_become_constraints(self) -> None:
        facts, report = daily_2param()
        # An unsplit two-family request: header.sources is ['MERRA2','SYN1DEG'],
        # so this report really does carry an ERROR.
        self.assertIn("MIXED_SOURCES", report.codes())
        md = build_metadata(
            facts,
            report,
            title="t",
            parameters=["T2M", "ALLSKY_SFC_SW_DWN"],
            temporal="daily",
        )
        self.assert_findings_map_to_constraints(md, report)

    def test_preflight_and_postfetch_findings_share_one_record(self) -> None:
        # The 1983 ask: radiation before the record starts, two parent
        # datasets, and a 200 that silently omits one parameter. Pre-flight and
        # post-fetch findings land in the same layer, so both must appear.
        facts, report = partial_1983()
        pre = preflight(
            temporal="daily",
            mode="point",
            parameters=["T2M", "ALLSKY_SFC_SW_DWN"],
            start=date(1983, 6, 1),
            end=date(1983, 6, 3),
            sites=[{"name": "boulder"}],
        )
        combined = QaReport(findings=pre.findings + report.findings)
        self.assertIn("RECORD_START", combined.codes())
        self.assertIn("MISSING_PARAMETER", combined.codes())

        md = build_metadata(
            facts,
            combined,
            title="t",
            parameters=["T2M", "ALLSKY_SFC_SW_DWN"],
            temporal="daily",
        )
        self.assert_findings_map_to_constraints(md, combined)

    def test_severity_labels_cover_the_three_levels(self) -> None:
        # Grouping is only worth anything if the types really do differ across
        # real reports: Error from the mixed-source daily fetch, Warning and
        # Info from the 1983 partial.
        seen: set[str] = set()
        for facts, report in (daily_2param(), partial_1983()):
            md = build_metadata(
                facts,
                report,
                title="t",
                parameters=["T2M", "ALLSKY_SFC_SW_DWN"],
                temporal="daily",
            )
            seen.update(c.type for c in md.constraints())
        self.assertEqual(seen, {"Error", "Warning", "Info"})

    def test_affected_parameters_reach_the_constraint_text(self) -> None:
        # A constraint that says a parameter is missing without naming it is
        # unactionable in a panel that shows no other context.
        facts, report = partial_1983()
        md = build_metadata(
            facts,
            report,
            title="t",
            parameters=["T2M", "ALLSKY_SFC_SW_DWN"],
            temporal="daily",
        )
        index = report.codes().index("MISSING_PARAMETER")
        self.assertIn("ALLSKY_SFC_SW_DWN", md.constraints()[index].constraint)

    def test_detail_and_affected_both_reach_the_constraint_text(self) -> None:
        # A synthetic finding, because in every real report the affected
        # parameter is already named in the message: the real reports cannot
        # tell "affected was appended" from "the message happened to say it".
        # Distinct tokens in each field are the only way to see all three
        # pieces arrive. A panel that shows the constraint and nothing else is
        # the whole audience for this.
        finding = QaFinding(
            level=Level.WARNING,
            code="SYNTHETIC_FOR_TEST",
            message="alpha-message",
            detail="bravo-detail",
            affected=("charlie-affected", "delta-affected"),
        )
        facts, _ = daily_2param()
        md = build_metadata(
            facts,
            QaReport(findings=[finding]),
            title="t",
            parameters=["T2M"],
            temporal="daily",
        )

        text = md.constraints()[0].constraint
        for token in ("alpha-message", "bravo-detail", "charlie-affected", "delta-affected"):
            with self.subTest(token=token):
                self.assertIn(token, text)
        self.assertEqual(md.constraints()[0].type, "Warning")

    def test_a_finding_with_neither_detail_nor_affected_is_still_a_constraint(self) -> None:
        # The optional fields are optional: a bare finding must not produce a
        # constraint with "None" or a trailing "Affects: ." in it.
        finding = QaFinding(level=Level.INFO, code="BARE", message="alpha-message")
        facts, _ = daily_2param()
        md = build_metadata(
            facts,
            QaReport(findings=[finding]),
            title="t",
            parameters=["T2M"],
            temporal="daily",
        )
        self.assertEqual(md.constraints()[0].constraint, "alpha-message")

    def test_clean_report_adds_no_constraints(self) -> None:
        facts, _ = daily_2param()
        md = build_metadata(
            facts, QaReport(), title="t", parameters=["T2M"], temporal="daily"
        )
        self.assertEqual(md.constraints(), [])


class LinkTests(QgisTestCase):
    """Every request URL, verbatim, plus the project homepage."""

    def test_request_url_appears_exactly(self) -> None:
        facts, report = daily_2param()
        url = url_of(DAILY_2PARAM)
        md = build_metadata(
            facts, report, title="t", parameters=["T2M"], temporal="daily", urls=[url]
        )
        urls = [link.url for link in md.links()]
        self.assertIn(url, urls)
        self.assertEqual(len(md.links()), 2)

    def test_every_url_gets_its_own_link(self) -> None:
        # A family-split or tiled fetch issues several requests; a layer built
        # from three of them has to list all three or it cannot be re-run.
        facts, report = daily_2param()
        urls = [
            url_of("point_solar_2000.json"),
            url_of("point_solar_2001.json"),
            url_of("regional_daily_fc.json"),
        ]
        md = build_metadata(
            facts, report, title="t", parameters=["T2M"], temporal="daily", urls=urls
        )
        self.assertEqual(len(md.links()), len(urls) + 1)
        recorded = [link.url for link in md.links()]
        for url in urls:
            self.assertIn(url, recorded)

    def test_homepage_link_is_always_present(self) -> None:
        facts, report = daily_2param()
        md = build_metadata(
            facts, report, title="t", parameters=["T2M"], temporal="daily", urls=[]
        )
        self.assertEqual(len(md.links()), 1)
        self.assertEqual(md.links()[0].url, "https://power.larc.nasa.gov/")


class HistoryTests(QgisTestCase):
    """What was done to the numbers, in numbers."""

    def test_api_version_is_read_from_the_response(self) -> None:
        # Measured 2026-09-07: the daily endpoint answered v2.9.7 while monthly
        # answered v2.9.8 in the same session. A hardcoded version would put the
        # same string in both records.
        daily_facts, daily_report = daily_2param()
        monthly_facts, monthly_report = monthly()

        daily_history = " ".join(
            build_metadata(
                daily_facts,
                daily_report,
                title="t",
                parameters=["T2M"],
                temporal="daily",
            ).history()
        )
        monthly_history = " ".join(
            build_metadata(
                monthly_facts,
                monthly_report,
                title="t",
                parameters=["T2M"],
                temporal="monthly",
            ).history()
        )

        self.assertIn("v2.9.7", daily_history)
        self.assertNotIn("v2.9.8", daily_history)
        self.assertIn("v2.9.8", monthly_history)
        self.assertNotIn("v2.9.7", monthly_history)

    def test_fill_value_is_recorded(self) -> None:
        facts, report = daily_2param()
        history = " ".join(
            build_metadata(
                facts, report, title="t", parameters=["T2M"], temporal="daily"
            ).history()
        )
        # Read from header.fill_value, never hardcoded -- so the record shows
        # the number the response actually declared.
        self.assertEqual(facts.fill_value, -999.0)
        self.assertIn("-999.0", history)

    def test_daily_irradiance_conversion_carries_its_factor(self) -> None:
        # 1 kW-hr/m^2/day = 1000 Wh spread over 24 h = 41.6667 W m-2. Without
        # the factor a reader cannot get back to POWER's own numbers.
        facts, report = daily_2param()
        md = build_metadata(
            facts,
            report,
            title="t",
            parameters=["T2M", "ALLSKY_SFC_SW_DWN"],
            temporal="daily",
            # Native units are the default now; this test is about the
            # conversion arithmetic, so it asks for it.
            converted=True,
        )
        line = history_for(md, "ALLSKY_SFC_SW_DWN")
        self.assertIn("kW-hr/m^2/day", line)
        self.assertIn("W m-2", line)
        self.assertIn("41.6667", line)

    def test_temperature_conversion_carries_its_offset(self) -> None:
        facts, report = daily_2param()
        md = build_metadata(
            facts,
            report,
            title="t",
            parameters=["T2M", "ALLSKY_SFC_SW_DWN"],
            temporal="daily",
            converted=True,
        )
        line = history_for(md, "T2M")
        self.assertIn("+273.15", line)
        self.assertIn("K", line)

    def test_hourly_accumulation_is_not_scaled_by_3600(self) -> None:
        # Hourly solar arrives as Wh/m^2, and a watt-hour accumulated over one
        # hour IS a watt: the conversion is x1. A history line quoting 3600
        # would mean the values on the map are wrong by that factor.
        facts, report = hourly()
        md = build_metadata(
            facts,
            report,
            title="t",
            parameters=["ALLSKY_SFC_SW_DWN"],
            temporal="hourly",
            converted=True,
        )
        line = history_for(md, "ALLSKY_SFC_SW_DWN")
        self.assertIn("Wh/m^2", line)
        self.assertIn("W m-2", line)
        self.assertNotIn("3600", line)

    def test_power_messages_are_carried(self) -> None:
        # point_1983_partial is a 200 whose messages[] is the only explanation
        # of the dropped radiation parameter. It has to survive into the layer.
        facts, report = partial_1983()
        self.assertEqual(len(facts.messages), 1)
        md = build_metadata(
            facts,
            report,
            title="t",
            parameters=["T2M", "ALLSKY_SFC_SW_DWN"],
            temporal="daily",
        )
        history = " ".join(md.history())
        self.assertIn(facts.messages[0], history)

    def test_omitted_parameter_gets_no_conversion_line(self) -> None:
        # History describes what was done, not what was asked for. The 1983
        # response contains no ALLSKY_SFC_SW_DWN, so claiming a conversion for
        # it would be a fabricated processing step.
        facts, report = partial_1983()
        md = build_metadata(
            facts,
            report,
            title="t",
            parameters=["T2M", "ALLSKY_SFC_SW_DWN"],
            temporal="daily",
        )
        self.assertEqual(facts.missing_parameters, ("ALLSKY_SFC_SW_DWN",))
        self.assertTrue(history_for(md, "T2M"))
        self.assertEqual(history_for(md, "ALLSKY_SFC_SW_DWN"), "")

    def test_sources_are_named(self) -> None:
        facts, report = daily_2param()
        history = " ".join(
            build_metadata(
                facts, report, title="t", parameters=["T2M"], temporal="daily"
            ).history()
        )
        self.assertEqual(facts.sources, ("MERRA2", "SYN1DEG"))
        # Expanded to their real names by core.provenance, so a reader who has
        # never seen POWER's abbreviations can still follow them.
        self.assertIn("MERRA-2", history)
        self.assertIn("CERES SYN1deg", history)


class RightsTests(QgisTestCase):
    """The citation, carrying the version that actually served the request."""

    def _rights(self, facts: ResponseFacts, report: QaReport, temporal: str) -> str:
        md = build_metadata(
            facts, report, title="t", parameters=["T2M"], temporal=temporal
        )
        self.assertEqual(len(md.rights()), 1)
        return md.rights()[0]

    def test_citation_differs_between_two_endpoint_versions(self) -> None:
        daily_facts, daily_report = daily_2param()
        monthly_facts, monthly_report = monthly()
        # The fixtures must actually disagree, or this test proves nothing.
        self.assertNotEqual(daily_facts.api_version, monthly_facts.api_version)

        daily_rights = self._rights(daily_facts, daily_report, "daily")
        monthly_rights = self._rights(monthly_facts, monthly_report, "monthly")

        self.assertNotEqual(daily_rights, monthly_rights)
        self.assertIn(daily_facts.api_version, daily_rights)
        self.assertIn(monthly_facts.api_version, monthly_rights)
        self.assertNotIn(monthly_facts.api_version, daily_rights)

    def test_citation_follows_the_version_and_nothing_else(self) -> None:
        # Everything but header.api.version held fixed, so a citation that
        # hardcoded the version would produce two identical strings here.
        facts, report = daily_2param()
        bumped = replace(facts, api_version="v9.9.9")

        first = self._rights(facts, report, "daily")
        second = self._rights(bumped, report, "daily")

        self.assertNotEqual(first, second)
        self.assertIn("v2.9.7", first)
        self.assertIn("v9.9.9", second)
        self.assertNotIn("v2.9.7", second)

    def test_the_citation_names_the_parent_datasets(self) -> None:
        # POWER's own citation guidance asks for the parent dataset alongside
        # POWER itself: meteorology from this request is MERRA-2, and citing
        # only "NASA POWER" drops the attribution the parent asks for.
        facts, report = daily_2param()
        self.assertEqual(facts.sources, ("MERRA2", "SYN1DEG"))

        rights = self._rights(facts, report, "daily")
        self.assertEqual(
            rights,
            build_citation(facts.api_name, facts.api_version, sources=facts.sources),
        )
        # Dropping the sources really does change the string, so the equality
        # above is not satisfied by any citation that ignores them.
        self.assertNotEqual(
            rights, build_citation(facts.api_name, facts.api_version)
        )

    def test_licence_and_fees_are_stated(self) -> None:
        # A rights statement with no licence beside it reads as a restriction.
        facts, report = daily_2param()
        md = build_metadata(
            facts, report, title="t", parameters=["T2M"], temporal="daily"
        )
        self.assertTrue(md.licenses())
        self.assertEqual(md.fees(), "None")


class ApplyMetadataTests(QgisTestCase):
    """The record, on a real layer."""

    def _layer(self, name: str = "T2M (K) daily UTC") -> QgsVectorLayer:
        layer = QgsVectorLayer(
            "Point?crs=EPSG:4326&field=value:double", name, "memory"
        )
        self.assertTrue(layer.isValid())
        return layer

    def test_round_trips_onto_a_vector_layer(self) -> None:
        facts, report = daily_2param()
        layer = self._layer()
        apply_metadata(
            layer,
            facts,
            report,
            parameters=["T2M", "ALLSKY_SFC_SW_DWN"],
            temporal="daily",
            urls=[url_of(DAILY_2PARAM)],
        )

        md = layer.metadata()
        self.assertEqual(len(md.constraints()), len(report.findings))
        self.assertTrue(md.constraints())
        self.assertEqual(md.crs().authid(), "EPSG:4326")
        self.assertIn(url_of(DAILY_2PARAM), [link.url for link in md.links()])

    def test_title_is_the_layer_name(self) -> None:
        facts, report = daily_2param()
        layer = self._layer("ALLSKY_SFC_SW_DWN (W m-2) daily UTC [check QA]")
        apply_metadata(
            layer, facts, report, parameters=["ALLSKY_SFC_SW_DWN"], temporal="daily"
        )
        self.assertEqual(layer.metadata().title(), layer.name())

    def test_record_survives_a_qmd_sidecar(self) -> None:
        # The whole point of putting this in QgsLayerMetadata rather than in the
        # dock: it has to survive leaving the session. A .qmd is the cheapest
        # proof, and it also proves the '&' in a query string is escaped and
        # unescaped rather than truncating the URL.
        facts, report = daily_2param()
        url = url_of(DAILY_2PARAM)
        layer = self._layer()
        apply_metadata(
            layer,
            facts,
            report,
            parameters=["T2M", "ALLSKY_SFC_SW_DWN"],
            temporal="daily",
            urls=[url],
            # The conversion factor is what this asserts survives the round
            # trip, so the conversion has to have happened.
            converted=True,
        )

        with tempfile.TemporaryDirectory() as tmp:
            sidecar = str(Path(tmp) / "layer.qmd")
            _message, saved = layer.saveNamedMetadata(sidecar)
            self.assertTrue(saved)

            reloaded = self._layer("reloaded")
            _message, loaded = reloaded.loadNamedMetadata(sidecar)
            self.assertTrue(loaded)

        md = reloaded.metadata()
        self.assertEqual(
            [(c.type, c.constraint) for c in md.constraints()],
            [(c.type, c.constraint) for c in layer.metadata().constraints()],
        )
        self.assertIn(url, [link.url for link in md.links()])
        self.assertIn("41.6667", " ".join(md.history()))


if __name__ == "__main__":
    import unittest

    unittest.main()
