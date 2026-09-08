"""Build the acknowledgement POWER asks users to carry.

The POWER project asks that its data be acknowledged in any publication. The
sentence itself is fixed; what is not fixed is the API that served the request.
Version drifts, and it drifts **per endpoint** -- daily answered v2.9.7 while
monthly answered v2.9.8 in the same session, and both were v2.9.4/v2.9.5 two
months earlier. So the version is read from each response's
``header.api.version`` and never hardcoded.

The result is attached to every layer as its ``QgsLayerMetadata`` rights
statement, so it travels with the data into a project file, a GeoPackage or a
``.qmd`` sidecar rather than living only in the dock.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

#: POWER's requested acknowledgement.
ACKNOWLEDGEMENT = (
    "These data were obtained from the NASA Langley Research Center (LaRC) POWER "
    "Project funded through the NASA Earth Science/Applied Science Program."
)

HOMEPAGE = "https://power.larc.nasa.gov/"
DOCS = "https://power.larc.nasa.gov/docs/"

#: Where each half of POWER actually comes from. Stated because "NASA POWER"
#: alone does not tell a reader whether a number is a reanalysis or a satellite
#: retrieval, and the answer differs per parameter within one layer set.
PARENT_NOTE = (
    "POWER solar parameters derive from CERES SYN1deg (and NASA/GEWEX SRB before "
    "2001-01-01); POWER meteorology derives from MERRA-2/GEOS and is served on "
    "MERRA-2's native grid."
)


def build_citation(
    api_name: str = "",
    api_version: str = "",
    *,
    accessed: date | datetime | None = None,
    sources: tuple[str, ...] = (),
) -> str:
    """Render the citation for one fetch.

    Parameters
    ----------
    api_name, api_version
        From the response's ``header.api``. Both are per-endpoint and change
        over time, so they are recorded rather than assumed.
    accessed
        Access date. Defaults to today in UTC -- POWER is updated to within
        days of the present, so *when* a series was pulled is part of
        identifying it.
    sources
        The response's ``header.sources``, naming the parent datasets.
    """
    when = accessed or datetime.now(timezone.utc).date()
    if isinstance(when, datetime):
        when = when.date()

    api = " ".join(p for p in (api_name, api_version) if p)
    served = f" Served by {api}." if api else ""
    parents = ""
    if sources:
        from nasa_power.core.provenance import describe_sources

        parents = f" Parent dataset(s): {describe_sources(sources)}."

    return (
        f"{ACKNOWLEDGEMENT}{served}{parents} "
        f"Accessed {when.isoformat()} via {HOMEPAGE}"
    )


def build_provenance_note(
    sources: tuple[str, ...] = (),
    *,
    time_standard: str = "",
    grid_label: str = "",
) -> str:
    """A longer note for the layer abstract, spelling out what the data is not.

    POWER is not ground truth: its meteorology *is* a reanalysis and its solar
    half *is* a satellite-derived analysis. Evaluating a model against POWER
    meteorology is evaluating it against MERRA-2, which may be exactly the
    intent or may be circular. Saying so in the layer costs nothing and stops
    the layer being quoted as an observation.
    """
    lines = [PARENT_NOTE]
    if sources:
        from nasa_power.core.provenance import describe_sources

        lines.append(f"This layer's request reported: {describe_sources(sources)}.")
    if grid_label:
        lines.append(f"Served on the {grid_label} grid; not resampled by this plugin.")
    if time_standard:
        lines.append(
            f"Timestamps are {time_standard.upper()}."
            + (
                " POWER's own default is Local Solar Time, which is roughly a "
                "seven-hour offset at mid-latitudes."
                if time_standard.upper() == "LST"
                else ""
            )
        )
    lines.append(
        "POWER is not ground truth: it is reanalysis (meteorology) and "
        "satellite-derived analysis (solar), not station measurement."
    )
    return " ".join(lines)
