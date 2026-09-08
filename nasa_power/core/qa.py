"""Findings that stop the plugin putting a wrong number on the map.

Two passes, both pure functions over plain data:

* :func:`preflight` runs on the request *before* any HTTP happens. A
  ``BLOCKING`` finding refuses the fetch outright -- these are asks the API
  would reject anyway, and one (``hourly/regional``) fails as a wall of HTML
  with no clue what went wrong.
* :func:`postfetch` runs on the parsed response. Nothing here can refuse
  anything -- the data has arrived -- so these annotate: which timesteps were
  dropped, which parameter POWER quietly did not send, whether the series
  straddles a change of parent dataset.

The report goes to exactly three places, so the dock, the Processing algorithms
and the layer metadata cannot disagree about what happened:

1. ``qgis_bridge.layer_metadata`` -- one ``QgsLayerMetadata.Constraint`` per
   finding, plus links, history and rights.
2. ``gui.qa_panel`` -- a table, with the offending URL beside each row.
3. ``processing.*`` -- ``feedback.pushWarning()`` per finding.

Severity is a judgement about consequence, not about tidiness. The one worth
arguing over is ``RECORD_START``: radiation does not exist before 1984, but
POWER accepts such a request when a meteorology parameter rides along and
answers 200 with fill. Blocking it would refuse a request the API honours, so
it is a WARNING and the fill is caught separately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import IntEnum
from typing import Iterable, Mapping, Sequence

from nasa_power.core.api import (
    POINT_MAX_PARAMS,
    REGIONAL_MAX_PARAMS,
    REGIONAL_MAX_SPAN_DEGREES,
    REGIONAL_MIN_SPAN_DEGREES,
    UNSUPPORTED_ENDPOINTS,
    PowerRequest,
    count_requests,
    span_below_minimum,
)
from nasa_power.core.decode import Observation, ResponseFacts
from nasa_power.core.dictionary import unavailable_at
from nasa_power.core.provenance import (
    RADIATION_RECORD_START,
    SRB_SYN1DEG_TRANSITION,
    Family,
    family_groups,
    max_offset,
    spans_provenance_seam,
)
from nasa_power.core.timeaxis import dropped_keys
from nasa_power.core.units import rule_for


class Level(IntEnum):
    """Ordered so ``max()`` over findings gives the worst one."""

    INFO = 10
    WARNING = 20
    ERROR = 30
    #: Refuses the request. Only for asks the API itself would reject.
    BLOCKING = 40

    @property
    def label(self) -> str:
        return self.name.title()


@dataclass(frozen=True)
class QaFinding:
    """One thing worth telling the user about a request or its response."""

    level: Level
    #: Stable machine-readable identifier, e.g. ``BBOX_SPAN``. Tests assert on
    #: this, never on the message text.
    code: str
    #: One line, written for someone who did not read this module.
    message: str
    #: Optional elaboration -- measurements, affected ranges, what to do.
    detail: str = ""
    #: Parameters, sites, bands or timesteps this applies to.
    affected: tuple[str, ...] = ()
    #: The request URL, when there is one. A POWER 422 is unactionable without
    #: it: the user cannot tell which of six tiled requests was malformed.
    url: str = ""

    def __str__(self) -> str:
        return f"[{self.level.label}] {self.code}: {self.message}"


@dataclass
class QaReport:
    """Every finding from one fetch."""

    findings: list[QaFinding] = field(default_factory=list)

    def add(self, *findings: QaFinding) -> None:
        self.findings.extend(findings)

    def extend(self, other: "QaReport") -> None:
        self.findings.extend(other.findings)

    def at_least(self, level: Level) -> list[QaFinding]:
        return [f for f in self.findings if f.level >= level]

    @property
    def blocking(self) -> list[QaFinding]:
        return [f for f in self.findings if f.level is Level.BLOCKING]

    @property
    def worst(self) -> Level | None:
        return max((f.level for f in self.findings), default=None)

    @property
    def is_blocked(self) -> bool:
        """Whether the request must not be sent."""
        return bool(self.blocking)

    @property
    def has_error(self) -> bool:
        """Whether a layer built from this should carry the warning marker."""
        return any(f.level >= Level.ERROR for f in self.findings)

    def codes(self) -> list[str]:
        return [f.code for f in self.findings]

    def summary(self) -> str:
        if not self.findings:
            return "No issues found."
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding.level.label] = counts.get(finding.level.label, 0) + 1
        return ", ".join(f"{n} {label.lower()}" for label, n in counts.items())


# --------------------------------------------------------------------------- #
# Pre-flight
# --------------------------------------------------------------------------- #

#: Above this many features, building the layer on the main thread in
#: ``finished()`` is noticeable. A year of hourly data at four sites for two
#: parameters is 70,080 features, and the long form is the only layout the
#: temporal controller can animate, so the count cannot simply be reduced.
FEATURE_COUNT_LIMIT = 25_000


def _span(bbox: Mapping[str, float], lo: str, hi: str) -> float:
    return bbox[hi] - bbox[lo]


def preflight(
    *,
    temporal: str,
    mode: str,
    parameters: Sequence[str],
    start: date,
    end: date,
    sites: Sequence[Mapping[str, object]] | None = None,
    bbox: Mapping[str, float] | None = None,
    time_standard: str = "UTC",
    community: str = "RE",
    dictionary_temporals: Mapping[str, Iterable[str]] | None = None,
) -> QaReport:
    """Check an ask before it is sent. Blocking findings refuse the fetch.

    ``dictionary_temporals`` maps a parameter to the temporal levels the fetched
    dictionary says it supports. When absent, a static fallback covers the
    daily-aggregate parameters most likely to be picked by hand.
    """
    report = QaReport()

    if not parameters:
        report.add(
            QaFinding(Level.BLOCKING, "NO_PARAMETERS", "Choose at least one parameter.")
        )
        return report

    # -- endpoint exists ------------------------------------------------- #
    if (temporal, mode) in UNSUPPORTED_ENDPOINTS:
        report.add(
            QaFinding(
                Level.BLOCKING,
                "HOURLY_REGIONAL",
                f"POWER has no {temporal} gridded endpoint.",
                detail=(
                    f"The {temporal}/{mode} combination returns a 404 HTML page rather "
                    f"than an API error. Use daily for a gridded area, or switch to "
                    f"point mode to keep {temporal} resolution at specific sites."
                ),
            )
        )

    # -- parameter counts ------------------------------------------------ #
    cap = POINT_MAX_PARAMS if mode == "point" else REGIONAL_MAX_PARAMS
    if mode == "regional" and len(parameters) > REGIONAL_MAX_PARAMS:
        report.add(
            QaFinding(
                Level.BLOCKING,
                "PARAM_COUNT",
                f"Gridded requests take one parameter at a time; {len(parameters)} selected.",
                detail=(
                    "POWER's regional endpoint serves exactly one parameter per "
                    "request. Fetch them one at a time -- each arrives on its own "
                    "parent grid anyway, so they could not share a raster."
                ),
                affected=tuple(parameters),
            )
        )
    elif mode == "point" and len(parameters) > cap:
        # Not blocking: the planner chunks point requests at 20 automatically.
        report.add(
            QaFinding(
                Level.INFO,
                "PARAM_CHUNKED",
                f"{len(parameters)} parameters will be split across several requests.",
                detail=f"POWER accepts at most {cap} parameters per point request.",
                affected=tuple(parameters),
            )
        )

    # -- parameter availability at this level ---------------------------- #
    if dictionary_temporals is not None:
        missing = [
            p
            for p in parameters
            if p in dictionary_temporals
            and temporal not in {t.lower() for t in dictionary_temporals[p]}
        ]
    else:
        missing = unavailable_at(parameters, temporal)
    if missing:
        report.add(
            QaFinding(
                Level.BLOCKING,
                "PARAM_NOT_AT_TEMPORAL",
                f"{', '.join(missing)} is not served at {temporal} resolution.",
                detail=(
                    "These are daily aggregates; POWER answers an hourly request for "
                    "them with a 422. Switch to daily, or deselect them."
                ),
                affected=tuple(missing),
            )
        )

    # -- dates ----------------------------------------------------------- #
    if end < start:
        report.add(
            QaFinding(
                Level.BLOCKING,
                "DATE_ORDER",
                f"End date {end.isoformat()} is before start date {start.isoformat()}.",
            )
        )

    # -- bounding box ---------------------------------------------------- #
    if mode == "regional":
        if bbox is None:
            report.add(
                QaFinding(Level.BLOCKING, "NO_BBOX", "Gridded mode needs an extent.")
            )
        else:
            # A bbox missing a corner is a caller bug, but pre-flight's contract
            # is to *return* findings -- a KeyError here would surface in the
            # dock as a traceback instead of a refusal, which is the same defect
            # the spans_legal guard below exists to avoid.
            corners = ("lat_min", "lat_max", "lon_min", "lon_max")
            absent = [c for c in corners if c not in bbox]
            if absent:
                report.add(
                    QaFinding(
                        Level.BLOCKING,
                        "BBOX_INCOMPLETE",
                        f"The extent is missing {', '.join(absent)}.",
                        detail="A gridded request needs all four corners in EPSG:4326 degrees.",
                        affected=tuple(absent),
                    )
                )

            # Falls through rather than returning: the caller still deserves to
            # hear about the time standard and the provenance seam in one pass.
            axes = (
                ()
                if absent
                else (("latitude", "lat_min", "lat_max"), ("longitude", "lon_min", "lon_max"))
            )
            spans_legal = not absent
            for axis, lo, hi in axes:
                span = _span(bbox, lo, hi)
                # Same predicate the planner uses, so the badge and the fetch
                # cannot disagree about whether an extent is legal.
                if span_below_minimum(span):
                    spans_legal = False
                    report.add(
                        QaFinding(
                            Level.BLOCKING,
                            "BBOX_SPAN",
                            f"The extent spans {span:.3g}° of {axis}; POWER needs at "
                            f"least {REGIONAL_MIN_SPAN_DEGREES:g}°.",
                            detail=(
                                "POWER's own message is 'otherwise use the point "
                                "endpoint' -- for an area this small, point mode gives "
                                "the same cells with more control."
                            ),
                        )
                    )
            # Only ask for the tile count once both spans are legal:
            # count_requests tiles the bbox, and tile_bbox itself raises on a
            # sub-2-degree span. Pre-flight must report that as a finding, not
            # raise out of a function whose whole job is to return findings.
            n = count_requests(temporal, mode, parameters, bbox=bbox) if spans_legal else 0
            if n > 1:
                report.add(
                    QaFinding(
                        Level.INFO,
                        "TILED",
                        f"This extent needs {n} requests.",
                        detail=(
                            f"POWER caps a gridded request at "
                            f"{REGIONAL_MAX_SPAN_DEGREES:g}° per axis, so the extent is "
                            f"tiled and the tiles mosaicked back together."
                        ),
                    )
                )

    # -- time standard --------------------------------------------------- #
    if time_standard.upper() == "LST":
        report.add(
            QaFinding(
                Level.WARNING,
                "LST_REQUESTED",
                "Timestamps will be Local Solar Time, not UTC.",
                detail=(
                    "At mid-latitudes this is roughly a seven-hour offset from UTC "
                    "(measured at Boulder: the same hourly irradiance peak appears at "
                    "17:00 UTC and 10:00 LST). Correct for solar-resource work, wrong "
                    "for anything compared against a UTC model."
                ),
            )
        )

    # -- provenance ------------------------------------------------------ #
    groups = family_groups(parameters)
    for family, names in groups.items():
        if spans_provenance_seam(family, start, end):
            report.add(
                QaFinding(
                    Level.ERROR,
                    "MIXED_PROVENANCE",
                    "This window crosses the change of solar parent dataset "
                    f"on {SRB_SYN1DEG_TRANSITION.isoformat()}.",
                    detail=(
                        "Before that date POWER's solar parameters come from NASA/GEWEX "
                        "SRB; from it onward, CERES SYN1deg. The series will contain "
                        "both with nothing in the data marking the seam. Split the "
                        "request at the boundary if the two must not be mixed."
                    ),
                    affected=tuple(names),
                )
            )
        if family is Family.RADIATION and start < RADIATION_RECORD_START:
            report.add(
                QaFinding(
                    Level.WARNING,
                    "RECORD_START",
                    "Radiation data does not exist before "
                    f"{RADIATION_RECORD_START.isoformat()}.",
                    detail=(
                        "POWER refuses a radiation-only request for this window, but "
                        "accepts it when a meteorology parameter is also selected -- "
                        "answering 200 and silently dropping the radiation parameter. "
                        "Either way there is no radiation data before 1984."
                    ),
                    affected=tuple(names),
                )
            )

    if len(groups) > 1 and mode == "point":
        report.add(
            QaFinding(
                Level.INFO,
                "FAMILY_SPLIT",
                f"Parameters span {len(groups)} parent datasets, so each gets its own request.",
                detail=(
                    "POWER reports its sources per request rather than per parameter, "
                    "so a mixed request cannot say which dataset produced which value. "
                    "Splitting costs an extra request and makes every value "
                    "attributable."
                ),
                affected=tuple(f.label for f in groups),
            )
        )

    # -- output size ----------------------------------------------------- #
    estimate = estimate_feature_count(
        temporal=temporal, parameters=parameters, start=start, end=end,
        n_sites=max(len(sites or ()), 1) if mode == "point" else 0,
    )
    if mode == "point" and estimate > FEATURE_COUNT_LIMIT:
        report.add(
            QaFinding(
                Level.WARNING,
                "FEATURE_COUNT_LIMIT",
                f"This will build about {estimate:,} features.",
                detail=(
                    "Every timestep is its own feature -- the only layout the Temporal "
                    "Controller can animate. The layer is assembled on the main thread, "
                    "so expect QGIS to pause while it is built. Shorten the window, or "
                    "use a coarser temporal level."
                ),
            )
        )

    return report


def estimate_feature_count(
    *,
    temporal: str,
    parameters: Sequence[str],
    start: date,
    end: date,
    n_sites: int,
) -> int:
    """Roughly how many long-form features an ask would produce."""
    if n_sites <= 0:
        return 0
    days = max((end - start).days + 1, 1)
    steps = {
        "hourly": days * 24,
        "daily": days,
        "monthly": max((end.year - start.year + 1) * 12, 1),
        "climatology": 12,
    }.get(temporal, days)
    return steps * len(parameters) * n_sites


# --------------------------------------------------------------------------- #
# Post-fetch
# --------------------------------------------------------------------------- #


def postfetch(
    request: PowerRequest,
    facts: ResponseFacts,
    observations: Sequence[Observation],
    *,
    raw_time_keys: Sequence[str] = (),
    was_cached: bool = False,
) -> QaReport:
    """Check what actually came back against what was asked for."""
    report = QaReport()
    url = request.url

    if was_cached:
        report.add(
            QaFinding(
                Level.INFO,
                "CACHE_HIT",
                "Served from the local cache; no request was made.",
                url=url,
            )
        )

    # -- the response's own notes ---------------------------------------- #
    if facts.messages:
        report.add(
            QaFinding(
                Level.WARNING,
                "SOFT_MESSAGES",
                "POWER returned notes alongside the data.",
                detail="\n".join(facts.messages),
                url=url,
            )
        )

    # -- did we get what we asked for? ----------------------------------- #
    if facts.missing_parameters:
        report.add(
            QaFinding(
                Level.WARNING,
                "MISSING_PARAMETER",
                f"POWER did not return {', '.join(facts.missing_parameters)}.",
                detail=(
                    "The request succeeded (HTTP 200) but the response omits this "
                    "parameter. POWER does this for data that does not exist in the "
                    "requested window, explaining itself only in its messages."
                ),
                affected=facts.missing_parameters,
                url=url,
            )
        )
    if facts.unexpected_parameters:
        report.add(
            QaFinding(
                Level.WARNING,
                "SUBSTITUTED_PARAMETER",
                f"POWER returned {', '.join(facts.unexpected_parameters)}, which was "
                f"not requested.",
                detail="POWER silently substitutes some parameters, e.g. PRECTOT for "
                "PRECTOTCORR.",
                affected=facts.unexpected_parameters,
                url=url,
            )
        )

    # -- time standard actually served ----------------------------------- #
    if facts.time_standard and facts.time_standard.upper() != request.time_standard.upper():
        report.add(
            QaFinding(
                Level.ERROR,
                "TIME_STANDARD_DRIFT",
                f"Asked for {request.time_standard.upper()} timestamps but POWER "
                f"returned {facts.time_standard.upper()}.",
                detail=(
                    "The difference is roughly seven hours at mid-latitudes. Anything "
                    "compared against a UTC model will be out of phase."
                ),
                url=url,
            )
        )

    # -- annual means dropped -------------------------------------------- #
    if raw_time_keys:
        dropped = dropped_keys(list(raw_time_keys), request.temporal)
        if dropped:
            report.add(
                QaFinding(
                    Level.INFO,
                    "YYYY13_DROPPED",
                    f"Dropped {len(dropped)} annual-mean value(s) from the series.",
                    detail=(
                        f"POWER returns {', '.join(dropped)} alongside the months. "
                        f"These are annual means, not timesteps; left in they would be "
                        f"a spurious point every thirteenth step."
                    ),
                    affected=tuple(dropped),
                    url=url,
                )
            )

    # -- provenance actually reported ------------------------------------ #
    if len(facts.sources) > 1:
        report.add(
            QaFinding(
                Level.ERROR,
                "MIXED_SOURCES",
                f"This response covers more than one parent dataset "
                f"({', '.join(facts.sources)}).",
                detail=(
                    "POWER reports sources per request, so no individual value here "
                    "can be attributed to a dataset. Either the request was not split "
                    "by parent, or the window crosses a change of parent."
                ),
                affected=facts.sources,
                url=url,
            )
        )

    # -- units drift ------------------------------------------------------ #
    unknown = [p for p, u in facts.units.items() if u and rule_for(u) is None]
    if unknown:
        report.add(
            QaFinding(
                Level.WARNING,
                "UNIT_DRIFT",
                "Unrecognised units; values were left unconverted.",
                detail="\n".join(f"{p}: {facts.units[p]!r}" for p in unknown),
                affected=tuple(unknown),
                url=url,
            )
        )

    # -- fill --------------------------------------------------------------#
    report.extend(_fill_findings(observations, url))

    # -- cell snapping ----------------------------------------------------- #
    if request.mode == "point" and observations:
        snapped = observations[0]
        if snapped.cell_latitude is not None:
            dlat, dlon = max_offset(snapped.family)
            report.add(
                QaFinding(
                    Level.INFO,
                    "CELL_SNAP_DERIVED",
                    f"Values come from the grid cell centred near "
                    f"{snapped.cell_latitude:g}, {snapped.cell_longitude:g}.",
                    detail=(
                        f"POWER returns the nearest grid cell but echoes back the "
                        f"coordinate you asked for, so the cell centre here is derived "
                        f"locally from the {snapped.family.label} grid. A requested "
                        f"point can sit up to {dlat:g}° of latitude and {dlon:g}° of "
                        f"longitude from the cell that answered it."
                    ),
                    url=url,
                )
            )

    return report


def _fill_findings(observations: Sequence[Observation], url: str) -> QaReport:
    """All-fill and partial-fill, per parameter."""
    report = QaReport()
    by_parameter: dict[str, list[Observation]] = {}
    for observation in observations:
        by_parameter.setdefault(observation.parameter, []).append(observation)

    for parameter, series in by_parameter.items():
        total = len(series)
        missing = sum(1 for o in series if o.value is None)
        if not total or not missing:
            continue
        if missing == total:
            report.add(
                QaFinding(
                    Level.WARNING,
                    "ALL_FILL",
                    f"Every value for {parameter} is missing.",
                    detail=(
                        "POWER returned its fill value for the whole series. This is "
                        "legal -- an ocean cell for a land-only parameter, or a window "
                        "before the record starts -- but there is nothing to plot."
                    ),
                    affected=(parameter,),
                    url=url,
                )
            )
        else:
            report.add(
                QaFinding(
                    Level.INFO,
                    "PARTIAL_FILL",
                    f"{missing} of {total} values for {parameter} are missing.",
                    detail="Missing values are stored as NULL, never as -999.",
                    affected=(parameter,),
                    url=url,
                )
            )
    return report


def check_valid_range(
    observations: Sequence[Observation],
    valid_min: float | None,
    valid_max: float | None,
    parameter: str,
    url: str = "",
) -> QaReport:
    """Flag values outside the range the response declared for itself.

    POWER ships ``valid_min``/``valid_max`` in its response attributes, in
    native units, so this costs nothing and catches a scaling error that would
    otherwise render as a merely odd-looking map.
    """
    report = QaReport()
    if valid_min is None and valid_max is None:
        return report

    outside = [
        o
        for o in observations
        if o.parameter == parameter
        and o.native_value is not None
        and (
            (valid_min is not None and o.native_value < valid_min)
            or (valid_max is not None and o.native_value > valid_max)
        )
    ]
    if outside:
        report.add(
            QaFinding(
                Level.ERROR,
                "VALID_RANGE",
                f"{len(outside)} value(s) for {parameter} fall outside the range POWER "
                f"declared ({valid_min} to {valid_max}).",
                detail=(
                    "The response declares this range itself, so values outside it "
                    "point at a decoding or scaling error rather than at unusual weather."
                ),
                affected=(parameter,),
                url=url,
            )
        )
    return report
