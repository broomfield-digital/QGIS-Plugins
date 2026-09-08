"""Which parent dataset produced a number, and on what grid.

This is the module that keeps the plugin honest, and the reason it needs to
exist is that POWER's own answers are contradictory:

* ``header.sources`` is **per request, not per parameter**. Asking for ``T2M``
  and ``ALLSKY_SFC_SW_DWN`` together returns ``['MERRA2', 'SYN1DEG']``,
  attributable to neither. Verified 2026-09-07.
* POWER's **CSV header contradicts its own JSON header**: for a 1985 daily
  solar request the CSV parameter line names ``CERES SYN1deg`` while
  ``header.sources`` for the identical request says ``['SRB']``.
* Solar is only CERES from **2001-01-01**. Bisected against the live API:
  2000-12-31 returns ``['SRB']`` (NASA/GEWEX Surface Radiation Budget),
  2001-01-01 returns ``['SYN1DEG']``. A 1998-2003 layer is half one and half
  the other with nothing in the data marking the seam.

So the strategy is: **split requests so each one has a single parent**, then
believe that request's ``header.sources``. The family prediction here only has
to be good enough to split; if it is wrong, the response comes back with two
sources and QA says so rather than mislabelling anything.

Note that POWER's dictionary ``type`` field is a UI category and not a
provenance signal -- ``CDD0`` ("Cooling Degree Days Above 0 C") is typed
``RADIATION`` but is plainly derived from temperature. Family membership here
is by parameter name.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Iterable, Mapping, Sequence

#: Solar switches parent here. Before this date POWER solar is NASA/GEWEX SRB;
#: from it onwards, CERES SYN1deg (with FLASHFlux in the near-real-time tail).
#: Bisected against the live API on 2026-09-07.
SRB_SYN1DEG_TRANSITION = date(2001, 1, 1)

#: Radiation parameters do not exist before this date at all. Distinct from
#: :data:`SRB_SYN1DEG_TRANSITION` -- they are two different facts and merging
#: them into one "era boundary" loses both.
#:
#: The failure mode is asymmetric and worth stating: a request for radiation
#: *alone* before 1984 is a 422, but the same request **with a meteorology
#: parameter alongside** returns HTTP 200, silently drops the radiation
#: parameter, and says so only in ``messages[]``. Verified 2026-09-07.
RADIATION_RECORD_START = date(1984, 1, 1)


class Family(str, Enum):
    """Which parent dataset a parameter comes from."""

    RADIATION = "radiation"
    METEOROLOGY = "meteorology"
    #: Computed from geometry alone (solar zenith angle, day length, ...).
    #: No parent dataset and no grid of its own.
    SOLAR_GEOMETRY = "solar-geometry"

    @property
    def label(self) -> str:
        return {
            Family.RADIATION: "radiation (CERES SYN1deg / GEWEX SRB)",
            Family.METEOROLOGY: "meteorology (MERRA-2 / GEOS)",
            Family.SOLAR_GEOMETRY: "solar geometry (computed by POWER)",
        }[self]


@dataclass(frozen=True)
class GridSpec:
    """A parent dataset's native grid.

    POWER does **not** regrid to a common resolution: each parameter arrives on
    its parent's grid. Measured for an identical bounding box, solar came back
    1.0 deg x 1.0 deg and meteorology 0.5 deg x 0.625 deg -- so the two are not
    co-registered and must never be mosaicked together.
    """

    #: Degrees of latitude per cell.
    dlat: float
    #: Degrees of longitude per cell.
    dlon: float
    #: True when cell centres sit at multiples of the spacing (MERRA-2, which
    #: has a node at lat 40.0); False when they sit at half-multiples (CERES
    #: 1.0 deg, centred on half-degrees). This is what decides whether adjacent
    #: request tiles share an edge row.
    centres_on_multiples: bool
    label: str


#: MERRA-2 / GEOS. A node sits exactly on integer degrees of latitude, which is
#: why two tiles meeting at lat 40.0 both return that row.
MERRA2_GRID = GridSpec(0.5, 0.625, True, "0.5° x 0.625°")
#: CERES SYN1deg. Cell centres are on half-degrees, so an integer tile boundary
#: falls between cells and nothing is duplicated.
SYN1DEG_GRID = GridSpec(1.0, 1.0, False, "1.0°")

GRIDS: Mapping[Family, GridSpec | None] = {
    Family.RADIATION: SYN1DEG_GRID,
    Family.METEOROLOGY: MERRA2_GRID,
    Family.SOLAR_GEOMETRY: None,
}

#: Parameter-name patterns that mark a genuine radiative-flux quantity, i.e.
#: one whose values come from CERES/SRB rather than from MERRA-2.
_RADIATION_PREFIXES = (
    "ALLSKY_",
    "CLRSKY_",
    "AOD_",
    "CLOUD_",
    "SRF_ALB",
    "TOA_",
    "DIFFUSE_",
    "DIRECT_",
    "MIDDAY_",
    "SI_",
    "SZA",
)

#: ...minus the ones whose names match the prefixes above but which POWER
#: computes from geometry, not from a radiance product.
_SOLAR_GEOMETRY = frozenset(
    {
        "SG_DAY_HOURS",
        "SG_DAY_LENGTH",
        "SG_NOON",
        "SG_DEC",
        "SZA",
        "SOLAR_ZENITH_ANGLE",
    }
)

#: Parameters POWER's dictionary types ``RADIATION`` whose values are actually
#: temperature-derived, so their parent is MERRA-2: ``CDD0``, ``CDD10``,
#: ``CDD18_3`` (cooling degree days). They are listed here only as the evidence
#: for reading POWER's ``type`` field as a UI category rather than as
#: provenance -- :func:`family_of` already classifies them correctly, because
#: none of them matches a radiation prefix and meteorology is the fallthrough.
#: An explicit override set would be inert, and inert code that looks
#: load-bearing is worse than a comment.
_DICTIONARY_TYPE_IS_NOT_PROVENANCE = ("CDD0", "CDD10", "CDD18_3")


def family_of(parameter: str) -> Family:
    """Predict which parent dataset serves ``parameter``.

    A prediction, not an assertion: it exists to split a request so that the
    response's own ``header.sources`` becomes attributable. If it guesses
    wrong the response carries two sources and QA reports ``MIXED_SOURCES``,
    which is a visible failure rather than a mislabelled layer.
    """
    name = parameter.strip().upper()
    if name in _SOLAR_GEOMETRY or name.startswith("SG_"):
        return Family.SOLAR_GEOMETRY
    if any(name.startswith(prefix) for prefix in _RADIATION_PREFIXES):
        return Family.RADIATION
    return Family.METEOROLOGY


def family_groups(parameters: Iterable[str]) -> dict[Family, list[str]]:
    """Group parameters by parent dataset, preserving order within each group.

    A point request for two families becomes two requests. That doubles the
    request count for a mixed ask, which is the price of every value on the map
    being attributable to a named dataset.
    """
    groups: dict[Family, list[str]] = {}
    for parameter in parameters:
        groups.setdefault(family_of(parameter), []).append(parameter)
    return groups


def expected_sources(family: Family, start: date, end: date) -> list[str]:
    """The parent datasets that should appear for ``family`` over a window.

    Returns more than one entry when the window straddles
    :data:`SRB_SYN1DEG_TRANSITION` -- the case that most needs saying out loud,
    because a mixed-provenance series looks completely normal on a chart.
    """
    if family is Family.SOLAR_GEOMETRY:
        return ["POWER"]
    if family is Family.METEOROLOGY:
        return ["MERRA2"]

    sources: list[str] = []
    if start < SRB_SYN1DEG_TRANSITION:
        sources.append("SRB")
    if end >= SRB_SYN1DEG_TRANSITION:
        sources.append("SYN1DEG")
    return sources or ["SYN1DEG"]


def spans_provenance_seam(family: Family, start: date, end: date) -> bool:
    """Whether a window crosses the SRB-to-CERES boundary for ``family``."""
    return family is Family.RADIATION and len(expected_sources(family, start, end)) > 1


def grid_for(family: Family) -> GridSpec | None:
    """The native grid ``family`` is served on, or ``None`` if it has none."""
    return GRIDS[family]


def snap_to_grid(family: Family, latitude: float, longitude: float) -> tuple[float, float]:
    """Locate the grid cell centre a point request will actually be answered from.

    POWER returns the **nearest grid cell**, not an interpolation -- but the
    response echoes back the coordinate that was *requested*, so nothing in it
    says which cell the number came from. A marker drawn at the click point can
    therefore sit up to half a cell from the data's real location: 0.25 deg of
    latitude and 0.3125 deg of longitude for MERRA-2, 0.5 deg for CERES.

    This derives the centre locally from the family's grid geometry. It is
    labelled as derived wherever it is stored, because it is our arithmetic
    rather than POWER's statement.
    """
    grid = grid_for(family)
    if grid is None:
        return latitude, longitude

    def snap(value: float, step: float, on_multiples: bool) -> float:
        if on_multiples:
            return round(value / step) * step
        # Centres at half-multiples: floor to the cell, then take its middle.
        import math

        return (math.floor(value / step) + 0.5) * step

    lat = snap(latitude, grid.dlat, grid.centres_on_multiples)
    lon = snap(longitude, grid.dlon, grid.centres_on_multiples)
    # Rounding keeps 40.000000000000004 out of layer attributes.
    return round(lat, 6), round(lon, 6)


def max_offset(family: Family) -> tuple[float, float]:
    """Worst-case (lat, lon) distance between a click and its answering cell."""
    grid = grid_for(family)
    if grid is None:
        return 0.0, 0.0
    return grid.dlat / 2.0, grid.dlon / 2.0


def describe_sources(sources: Sequence[str]) -> str:
    """Render POWER's source codes as something a person can read."""
    names = {
        "MERRA2": "MERRA-2",
        "SYN1DEG": "CERES SYN1deg",
        "SRB": "NASA/GEWEX SRB",
        "FLASHFLUX": "CERES FLASHFlux",
        "POWER": "POWER (computed)",
        "IMERG": "GPM IMERG",
        "SOURCE": "source native",
    }
    return ", ".join(names.get(s.upper(), s) for s in sources)
