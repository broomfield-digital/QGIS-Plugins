"""The machine-readable POWER parameter dictionary.

``GET /api/system/manager/parameters?community=RE&temporal=daily`` returns every
parameter that level serves, with its units, display name, definition and
category. Both query parameters are **required** -- the bare path is a 422.

Reading this beats hardcoding a catalogue for one measured reason: units depend
on the **community**, not only on the parameter and temporal level. Daily
``ALLSKY_SFC_SW_DWN`` is ``kW-hr/m^2/day`` under RE, ``MJ/m^2/day`` under AG and
``W m-2`` under SB, while ``T2M`` is ``C`` under all three. A static table keyed
on ``(parameter, temporal)`` is wrong the first time a user changes community.

Two things to know before wiring this to a combo box:

* **Monthly and climatology are enormous** -- 1388 and 1634 entries against 152
  for daily -- because they include ``_00``...``_23`` hour-of-day variants of
  most parameters. Unfiltered, the dropdown is unusable, so
  :func:`filter_parameters` hides them behind an "advanced" flag by default.
* **The ``type`` field is a UI category, not provenance.** ``CDD0`` ("Cooling
  Degree Days Above 0 C") is typed ``RADIATION`` but is derived from
  temperature. Use :mod:`nasa_power.core.provenance` for parentage.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from nasa_power.core.api import build_dictionary_url
from nasa_power.core.errors import PowerValidationError
from nasa_power.core.fetcher import DEFAULT_TIMEOUT, Fetcher

#: Trailing ``_00``..``_23``: an hour-of-day variant of a base parameter.
#: The hour range is spelled out rather than written ``_\d{2}$``, because the
#: loose form also matches ``AOD_55`` and ``AOD_84`` -- aerosol optical depth at
#: 0.55 and 0.84 um, two real daily RADIATION parameters -- and would hide them
#: from the default parameter list.
HOUR_VARIANT_RE = re.compile(r"_(?:[01][0-9]|2[0-3])$")

#: How long a cached dictionary stays fresh. POWER adds parameters occasionally
#: and never in a hurry; a month keeps the combo box instant without pinning a
#: stale list forever.
CACHE_MAX_AGE_SECONDS = 30 * 24 * 3600


@dataclass(frozen=True)
class ParameterInfo:
    """One entry from the dictionary."""

    name: str
    #: Native units at this (community, temporal). The key every conversion
    #: is keyed on.
    units: str
    #: Human-readable name, e.g. "All Sky Surface Shortwave Downward Irradiance".
    long_name: str
    #: Prose definition. Long -- good for a tooltip, useless as a label.
    definition: str = ""
    #: POWER's own category: RADIATION, METEOROLOGY, HYDROLOGY, SOLAR-GEOMETRY.
    #: A grouping for the UI, **not** a statement of parent dataset.
    category: str = ""
    #: ``SOURCE`` when served straight from the parent, ``POWER`` when POWER
    #: computed it (aggregates like ``T2M_MAX``, derived fields like ``RH2M``).
    source: str = ""

    @property
    def is_hour_variant(self) -> bool:
        """Whether this is an ``_00``..``_23`` hour-of-day variant."""
        return bool(HOUR_VARIANT_RE.search(self.name))

    @property
    def base_name(self) -> str:
        """The parameter this is an hour-of-day variant of, or itself."""
        return HOUR_VARIANT_RE.sub("", self.name)


def parse_dictionary(body: bytes | str | Mapping[str, object]) -> dict[str, ParameterInfo]:
    """Parse a dictionary response into :class:`ParameterInfo` by name."""
    if isinstance(body, (bytes, str)):
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise PowerValidationError(f"Parameter dictionary is not JSON: {exc}") from exc
    else:
        payload = body

    if not isinstance(payload, Mapping):
        raise PowerValidationError("Parameter dictionary is not a JSON object.")

    out: dict[str, ParameterInfo] = {}
    for name, entry in payload.items():
        if not isinstance(entry, Mapping):
            continue
        out[name] = ParameterInfo(
            name=name,
            units=str(entry.get("units", "")),
            long_name=str(entry.get("name", "")),
            definition=str(entry.get("definition", "")),
            category=str(entry.get("type", "")),
            source=str(entry.get("source", "")),
        )
    if not out:
        raise PowerValidationError("Parameter dictionary was empty.")
    return out


def dictionary_cache_path(cache_dir: str | Path, community: str, temporal: str) -> Path:
    """Where a fetched dictionary is kept. One file per (community, temporal)."""
    return Path(cache_dir) / "dictionary" / f"{community.upper()}-{temporal.lower()}.json"


def load_dictionary(
    community: str,
    temporal: str,
    cache_dir: str | Path,
    fetcher: Fetcher | None = None,
    *,
    force: bool = False,
    max_age: float = CACHE_MAX_AGE_SECONDS,
    timeout: float = DEFAULT_TIMEOUT,
    now: float | None = None,
) -> tuple[dict[str, ParameterInfo], bool]:
    """Return ``(parameters, from_cache)`` for one community and temporal level.

    A stale or missing cache with no fetcher is not an error: the caller falls
    back to :data:`CURATED_PARAMETERS` so the dock still works offline. Only a
    corrupt cache with no way to replace it raises.
    """
    path = dictionary_cache_path(cache_dir, community, temporal)
    stamp = time.time() if now is None else now

    if path.exists() and not force:
        fresh = (stamp - path.stat().st_mtime) < max_age
        if fresh or fetcher is None:
            try:
                return parse_dictionary(path.read_bytes()), True
            except PowerValidationError:
                if fetcher is None:
                    raise
                # Corrupt cache and a way to replace it: fall through and refetch.

    if fetcher is None:
        raise PowerValidationError(
            f"No cached parameter dictionary for {community}/{temporal} and no "
            f"fetcher to get one. Use the curated parameter list instead."
        )

    body = fetcher.fetch(build_dictionary_url(community, temporal), timeout)
    parsed = parse_dictionary(body)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".partial")
    try:
        tmp.write_bytes(body)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return parsed, False


def filter_parameters(
    parameters: Mapping[str, ParameterInfo],
    *,
    include_hour_variants: bool = False,
    categories: Iterable[str] | None = None,
    search: str = "",
) -> list[ParameterInfo]:
    """Narrow the dictionary down to something a combo box can show.

    Hour-of-day variants are excluded by default: they are what takes the
    monthly list from ~150 useful entries to 1388.
    """
    wanted = {c.upper() for c in categories} if categories else None
    needle = search.strip().lower()

    out: list[ParameterInfo] = []
    for info in parameters.values():
        if info.is_hour_variant and not include_hour_variants:
            continue
        if wanted is not None and info.category.upper() not in wanted:
            continue
        if needle and needle not in info.name.lower() and needle not in info.long_name.lower():
            continue
        out.append(info)

    out.sort(key=lambda i: (i.category, i.name))
    return out


def group_by_category(parameters: Sequence[ParameterInfo]) -> dict[str, list[ParameterInfo]]:
    """Group for a sectioned dropdown, preserving the sort within each group."""
    groups: dict[str, list[ParameterInfo]] = {}
    for info in parameters:
        groups.setdefault(info.category or "OTHER", []).append(info)
    return groups


#: The starting selection, used before a dictionary has been fetched and as the
#: offline fallback. Chosen to cover both halves of POWER -- CERES-derived
#: radiation and MERRA-2 meteorology -- so the provenance split is visible from
#: the first fetch rather than being an advanced topic.
CURATED_PARAMETERS: tuple[str, ...] = (
    # Radiation (CERES SYN1deg from 2001; GEWEX SRB before)
    "ALLSKY_SFC_SW_DWN",
    "CLRSKY_SFC_SW_DWN",
    "ALLSKY_SFC_LW_DWN",
    "ALLSKY_SFC_SW_DNI",
    "ALLSKY_SFC_SW_DIFF",
    "ALLSKY_KT",
    "ALLSKY_SRF_ALB",
    "TOA_SW_DWN",
    # Meteorology (MERRA-2 / GEOS)
    "T2M",
    "T2M_MAX",
    "T2M_MIN",
    "T2MDEW",
    "RH2M",
    "QV2M",
    "PS",
    "WS10M",
    "WS50M",
    "WD10M",
    "PRECTOTCORR",
    "CLOUD_AMT",
)

#: Parameters that are daily aggregates and therefore do not exist hourly.
#: Verified: an hourly request for ``T2M_MAX`` is a 422 ("One of your parameters
#: is incorrect"). Used for a local pre-flight refusal so the user is not sent
#: to the API to be told.
DAILY_ONLY_PARAMETERS = frozenset({"T2M_MAX", "T2M_MIN", "T2M_RANGE", "TS_MAX", "TS_MIN"})


def unavailable_at(parameters: Iterable[str], temporal: str) -> list[str]:
    """Which of ``parameters`` the API will reject at ``temporal``.

    Uses the fetched dictionary when one is available; this static list is the
    offline fallback and covers the cases most likely to be picked by hand.
    """
    if temporal != "hourly":
        return []
    return [p for p in parameters if p.upper() in DAILY_ONLY_PARAMETERS]
