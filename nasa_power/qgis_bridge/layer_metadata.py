"""Attach provenance, QA findings and the citation to a layer.

A layer that leaves this module can answer, on its own, without the plugin and
without the session that made it: where did these numbers come from, what was
done to them, what should I be careful about, and how do I cite them. That
matters because a QGIS layer outlives the fetch -- it gets saved into a
project, exported to GeoPackage, handed to a colleague -- and by then the dock
that explained it is gone.

The mapping is deliberately boring:

* every QA finding becomes a ``Constraint`` (its ``type`` is the severity, so
  the metadata panel groups them);
* every request URL becomes a ``Link``, so the exact query is reproducible;
* every processing step becomes a history item, including unit conversions
  **with their factor** -- "×41.6667" is the difference between a reader
  trusting the numbers and re-deriving them;
* the citation becomes the rights statement.
"""

from __future__ import annotations

from typing import Sequence

from qgis.core import (
    QgsAbstractMetadataBase,
    QgsCoordinateReferenceSystem,
    QgsLayerMetadata,
    QgsMapLayer,
)

from nasa_power.core.citation import HOMEPAGE, build_citation, build_provenance_note
from nasa_power.core.decode import ResponseFacts
from nasa_power.core.qa import QaReport
from nasa_power.core.units import describe_conversion

#: What the layer is, in Dublin Core terms. POWER is a modelled/retrieved
#: dataset, not a survey product.
METADATA_TYPE = "dataset"
METADATA_LANGUAGE = "ENG"


def build_metadata(
    facts: ResponseFacts,
    report: QaReport,
    *,
    title: str,
    parameters: Sequence[str],
    temporal: str,
    urls: Sequence[str] = (),
    grid_label: str = "",
) -> QgsLayerMetadata:
    """Assemble the metadata record for one fetch."""
    metadata = QgsLayerMetadata()
    metadata.setType(METADATA_TYPE)
    metadata.setLanguage(METADATA_LANGUAGE)
    metadata.setTitle(title)
    metadata.setIdentifier(f"nasa-power:{temporal}:{','.join(parameters)}")
    metadata.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
    metadata.setEncoding("UTF-8")

    metadata.setAbstract(
        build_provenance_note(
            facts.sources,
            time_standard=facts.time_standard,
            grid_label=grid_label,
        )
    )
    citation = build_citation(
        facts.api_name, facts.api_version, sources=facts.sources
    )
    metadata.setRights([citation])
    # POWER is free and open; saying so explicitly stops a reader assuming
    # otherwise from the presence of a rights statement.
    metadata.setLicenses(["Public domain (NASA POWER; no restrictions on use)"])
    metadata.setFees("None")
    metadata.setKeywords({"gmd:topicCategory": ["climatologyMeteorologyAtmosphere"]})

    _add_links(metadata, urls)
    _add_history(metadata, facts, parameters, temporal)
    _add_constraints(metadata, report)
    return metadata


def _add_links(metadata: QgsLayerMetadata, urls: Sequence[str]) -> None:
    home = QgsAbstractMetadataBase.Link("NASA POWER", "WWW:LINK", HOMEPAGE)
    home.description = "The POWER project's own documentation and data access."
    metadata.addLink(home)

    for index, url in enumerate(urls, start=1):
        # The exact query, not a description of it: this is what makes a layer
        # reproducible six months later, and what a bug report needs.
        link = QgsAbstractMetadataBase.Link(f"Request {index}", "WWW:LINK", url)
        link.description = "The API request this layer was built from."
        metadata.addLink(link)


def _add_history(
    metadata: QgsLayerMetadata,
    facts: ResponseFacts,
    parameters: Sequence[str],
    temporal: str,
) -> None:
    api = " ".join(p for p in (facts.api_name, facts.api_version) if p)
    metadata.addHistoryItem(
        f"Fetched from the NASA POWER REST API ({api or 'version not reported'}) "
        f"at {temporal} resolution, time standard {facts.time_standard or 'unreported'}."
    )
    if facts.sources:
        from nasa_power.core.provenance import describe_sources

        metadata.addHistoryItem(
            f"Parent dataset(s) reported by the response: {describe_sources(facts.sources)}."
        )
    if facts.fill_value is not None:
        metadata.addHistoryItem(
            f"Fill value {facts.fill_value} (from the response header) converted to NULL "
            f"before any unit scaling."
        )
    for parameter in parameters:
        native = facts.units.get(parameter, "")
        if native:
            metadata.addHistoryItem(f"{parameter}: {describe_conversion(native, temporal)}")
    for message in facts.messages:
        metadata.addHistoryItem(f"POWER message: {message}")


def _add_constraints(metadata: QgsLayerMetadata, report: QaReport) -> None:
    for finding in report.findings:
        text = finding.message
        if finding.detail:
            text = f"{text} {finding.detail}"
        if finding.affected:
            text = f"{text} Affects: {', '.join(finding.affected)}."
        # `type` is the severity, so the metadata panel groups Errors together
        # and a reader sees the serious ones first.
        metadata.addConstraint(QgsLayerMetadata.Constraint(text, finding.level.label))


def apply_metadata(
    layer: QgsMapLayer,
    facts: ResponseFacts,
    report: QaReport,
    *,
    parameters: Sequence[str],
    temporal: str,
    urls: Sequence[str] = (),
    grid_label: str = "",
) -> None:
    """Build and attach the metadata record to ``layer``."""
    layer.setMetadata(
        build_metadata(
            facts,
            report,
            title=layer.name(),
            parameters=parameters,
            temporal=temporal,
            urls=urls,
            grid_label=grid_label,
        )
    )
