"""Build, plan, cache and fetch NASA POWER requests.

Ported from ``davinci_monet/io/download/power.py`` in the DAVINCI project,
which was itself verified against the live API. Re-verified here on
2026-09-07 against API v2.9.7/v2.9.8, and changed in five places -- see
``docs/POWER-API-FACTS.md`` section A for the reasoning behind each:

1. The 10-degree regional **maximum** is now enforced. DAVINCI checked only
   the 2-degree minimum; the maximum was referenced solely by the tiler, so a
   hand-built 12-degree request reached the API and 422'd.
2. :class:`PowerRequest` carries ``fmt``, and the cache filename derives its
   suffix from it. DAVINCI hardcoded ``.nc``, which would have written JSON
   point responses under a NetCDF name.
3. ``_split_span``'s docstring claimed a rebalancing the code does not do.
4. No DAVINCI-specific exceptions, staging-command strings or cache paths.
5. A ``User-Agent``, and a delay between requests.

The load-bearing API facts, all measured:

* **``hourly/regional`` does not exist.** It returns a 19 KB HTML 404, not a
  JSON error, so it must be refused locally.
* Point requests take at most 20 parameters; regional requests take **exactly
  one**.
* A regional bounding box must span **at least 2 and at most 10 degrees** on
  both axes, so any real domain has to be tiled.
* Monthly dates are **year only**; ``YYYYMMDD`` on the monthly endpoint is a
  422.
* ``time-standard`` defaults to **LST**, not UTC -- a ~7 hour phase error at
  Boulder. Every request here asks for UTC unless told otherwise.
"""

from __future__ import annotations

import hashlib
import logging
import os
import math
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlencode

from nasa_power.core.errors import PowerCacheMiss, PowerHTTPError, PowerValidationError
from nasa_power.core.fetcher import DEFAULT_TIMEOUT, Fetcher
from nasa_power.core.provenance import Family, family_groups, family_of

logger = logging.getLogger(__name__)

BASE_URL = "https://power.larc.nasa.gov/api/temporal"
PARAMETER_DICTIONARY_URL = "https://power.larc.nasa.gov/api/system/manager/parameters"

#: Verified: 21 parameters on the point endpoint -> 422.
POINT_MAX_PARAMS = 20
#: Verified: 2 parameters on the regional endpoint -> 422 ("A maximum of 1
#: parameters are can currently be requested", sic).
REGIONAL_MAX_PARAMS = 1

#: Verified: 1.9 degrees -> 422 "Please provide at least a 2 degree range in
#: latitude; otherwise use the point endpoint." Exactly 2.0 is accepted.
REGIONAL_MIN_SPAN_DEGREES = 2.0
#: Verified: 12 degrees -> 422 "Please provide a maximum of 10 degree range in
#: latitude." Exactly 10.0 is accepted, and the tiler emits exactly-10.0 tiles,
#: so they must pass -- see :data:`SPAN_TOLERANCE`.
REGIONAL_MAX_SPAN_DEGREES = 10.0

#: Slack allowed when measuring a span against the two limits above.
#:
#: ``_split_span`` divides a span into equal pieces in binary floating point,
#: so a tile that is exactly 10 degrees wide does not always subtract to 10.0:
#: tiling latitude -29.8..50.2 produces a 30.2..40.2 tile whose span evaluates
#: to 10.000000000000004, and an exact ``>`` comparison refused it. That made
#: ``count_requests`` promise 8 requests and ``plan_requests`` then raise
#: "accepts at most a 10 degree range ...; got 10" -- a refusal whose own
#: message shows the value passing. 1e-9 degrees is about 0.1 mm on the ground,
#: far below any grid POWER serves and far above the ~1e-14 drift being
#: absorbed, so nothing a user can express is affected.
SPAN_TOLERANCE = 1e-9


def span_below_minimum(span: float) -> bool:
    """Whether ``span`` is under the regional minimum, allowing for FP drift."""
    return span < REGIONAL_MIN_SPAN_DEGREES - SPAN_TOLERANCE


def span_above_maximum(span: float) -> bool:
    """Whether ``span`` is over the regional maximum, allowing for FP drift."""
    return span > REGIONAL_MAX_SPAN_DEGREES + SPAN_TOLERANCE


TEMPORAL_LEVELS = ("hourly", "daily", "monthly")
MODES = ("point", "regional")
COMMUNITIES = ("RE", "AG", "SB")

#: Response formats the API accepts. The enum is closed -- asking for
#: ``GEOJSON`` returns a 422 that helpfully enumerates the real list.
FORMATS = ("JSON", "CSV", "ASCII", "NETCDF", "ICASA", "XARRAY")

#: Filename suffix per format, so a cache entry is openable by name.
_FORMAT_SUFFIX = {
    "JSON": ".json",
    "XARRAY": ".json",
    "NETCDF": ".nc",
    "CSV": ".csv",
    "ASCII": ".txt",
    "ICASA": ".txt",
}

#: (temporal, mode) pairs the API does not serve. Probed across the full
#: matrix: every combination returns 200 except this one, which 404s with an
#: HTML page rather than a JSON API error -- so the user would otherwise see a
#: wall of markup with no clue the combination is simply unsupported.
UNSUPPORTED_ENDPOINTS = frozenset({("hourly", "regional")})

#: Seconds to wait between consecutive requests. POWER publishes no rate limit,
#: but its docs warn that a client which "persists in requesting the same
#: relative location" may be blocked.
INTER_REQUEST_DELAY = 0.25

DEFAULT_MAX_TRIES = 3


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #


def _coerce_date(value: str | date | datetime) -> date:
    """Coerce an ISO string, ``date`` or ``datetime`` into a ``date``."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        raise PowerValidationError(
            f"Could not read {value!r} as a date. Use ISO format, e.g. 2024-02-01."
        ) from None


def format_power_date(value: str | date | datetime, temporal: str) -> str:
    """Format a date the way ``temporal``'s endpoint requires.

    Monthly takes a bare year; hourly and daily take ``YYYYMMDD``. Sending
    ``YYYYMMDD`` to the monthly endpoint is a 422 ("Please provide a correct
    start date formatting"), which is why this is not one shared format.
    """
    parsed = _coerce_date(value)
    if temporal == "monthly":
        return f"{parsed.year:04d}"
    return parsed.strftime("%Y%m%d")


# --------------------------------------------------------------------------- #
# URL construction
# --------------------------------------------------------------------------- #


def _check_bbox(bbox: Mapping[str, float]) -> None:
    """Validate a regional bounding box against both API limits."""
    missing = {"lat_min", "lat_max", "lon_min", "lon_max"} - set(bbox)
    if missing:
        raise PowerValidationError(f"bbox is missing {', '.join(sorted(missing))}.")

    # On the globe, and the right way round. POWER answers an off-globe or
    # inverted box with a 422, and an extent crossing the antimeridian arrives
    # here as lon_min > lon_max -- which subtracts to a negative span and would
    # otherwise be reported as "needs at least a 2 degree range".
    for axis, lo_key, hi_key, limit in (
        ("latitude", "lat_min", "lat_max", 90.0),
        ("longitude", "lon_min", "lon_max", 180.0),
    ):
        lo, hi = bbox[lo_key], bbox[hi_key]
        if hi <= lo:
            raise PowerValidationError(
                f"The extent's {axis} range runs backwards ({lo:g} to {hi:g}). "
                f"POWER takes a plain box in degrees; an extent crossing the "
                f"antimeridian has to be fetched as two."
            )
        if lo < -limit or hi > limit:
            raise PowerValidationError(
                f"The extent's {axis} range ({lo:g} to {hi:g}) is off the globe; "
                f"POWER accepts -{limit:g} to {limit:g}."
            )

    for axis, lo_key, hi_key in (
        ("latitude", "lat_min", "lat_max"),
        ("longitude", "lon_min", "lon_max"),
    ):
        span = bbox[hi_key] - bbox[lo_key]
        if span_below_minimum(span):
            raise PowerValidationError(
                f"POWER regional requires at least a {REGIONAL_MIN_SPAN_DEGREES:g} degree "
                f"range in {axis}; got {span:g}. Use point mode for a smaller area."
            )
        # Compared at a tolerance: the tiler produces exactly-10.0 degree tiles,
        # the API accepts them, and binary floating point does not always agree
        # that they are 10.0.
        if span_above_maximum(span):
            raise PowerValidationError(
                f"POWER regional accepts at most a {REGIONAL_MAX_SPAN_DEGREES:g} degree "
                f"range in {axis}; got {span:g}. Tile the box first -- see tile_bbox()."
            )


def build_power_url(
    temporal: str,
    mode: str,
    params: Sequence[str],
    *,
    start: str | date | datetime,
    end: str | date | datetime,
    latitude: float | None = None,
    longitude: float | None = None,
    bbox: Mapping[str, float] | None = None,
    community: str = "RE",
    fmt: str = "JSON",
    time_standard: str = "UTC",
) -> str:
    """Build one POWER API URL.

    Query keys are emitted in a fixed order -- ``parameters``, ``community``,
    coordinates, ``start``, ``end``, ``format``, ``time-standard`` -- because
    the URL is the cache key. Reordering the query would change every hash and
    silently orphan an existing cache.

    Raises
    ------
    PowerValidationError
        For anything the API would reject: an unknown level or mode, the
        missing ``hourly/regional`` endpoint, too many parameters for the mode,
        missing coordinates, or a bounding box outside the 2-10 degree window.
    """
    if temporal not in TEMPORAL_LEVELS:
        raise PowerValidationError(
            f"Unknown temporal level {temporal!r}. Known: {', '.join(TEMPORAL_LEVELS)}"
        )
    if mode not in MODES:
        raise PowerValidationError(f"Unknown mode {mode!r}. Known: {', '.join(MODES)}")
    if fmt.upper() not in FORMATS:
        raise PowerValidationError(
            f"Unknown format {fmt!r}. Known: {', '.join(FORMATS)}"
        )
    if not params:
        raise PowerValidationError("At least one parameter is required.")
    if (temporal, mode) in UNSUPPORTED_ENDPOINTS:
        raise PowerValidationError(
            f"POWER has no {temporal}/{mode} endpoint -- it returns a 404 HTML page "
            f"rather than an API error. Every other temporal x mode combination "
            f"exists. Use daily/{mode} for an area, or {temporal}/point for "
            f"specific sites."
        )

    query: list[tuple[str, Any]] = [
        ("parameters", ",".join(params)),
        ("community", community),
    ]

    if mode == "point":
        if len(params) > POINT_MAX_PARAMS:
            raise PowerValidationError(
                f"POWER point requests accept at most {POINT_MAX_PARAMS} parameters; "
                f"got {len(params)}. Split the request -- see plan_requests()."
            )
        if latitude is None or longitude is None:
            raise PowerValidationError("point mode requires latitude and longitude.")
        if not -90.0 <= latitude <= 90.0:
            raise PowerValidationError(f"latitude {latitude} is outside -90..90.")
        if not -180.0 <= longitude <= 180.0:
            raise PowerValidationError(
                f"longitude {longitude} is outside -180..180. POWER takes degrees, so "
                f"reproject to EPSG:4326 before building a request."
            )
        query += [("latitude", latitude), ("longitude", longitude)]
    else:
        if len(params) > REGIONAL_MAX_PARAMS:
            raise PowerValidationError(
                f"POWER regional requests accept exactly {REGIONAL_MAX_PARAMS} parameter; "
                f"got {len(params)} ({', '.join(params)}). Issue one request per parameter."
            )
        if bbox is None:
            raise PowerValidationError("regional mode requires a bbox.")
        _check_bbox(bbox)
        query += [
            ("latitude-min", bbox["lat_min"]),
            ("latitude-max", bbox["lat_max"]),
            ("longitude-min", bbox["lon_min"]),
            ("longitude-max", bbox["lon_max"]),
        ]

    query += [
        ("start", format_power_date(start, temporal)),
        ("end", format_power_date(end, temporal)),
        ("format", fmt.upper()),
        ("time-standard", time_standard.upper()),
    ]

    return f"{BASE_URL}/{temporal}/{mode}?{urlencode(query)}"


def build_dictionary_url(community: str, temporal: str) -> str:
    """URL for the machine-readable parameter dictionary.

    Both query parameters are required -- the bare path is a 422.
    """
    if community not in COMMUNITIES:
        raise PowerValidationError(
            f"Unknown community {community!r}. Known: {', '.join(COMMUNITIES)}"
        )
    return f"{PARAMETER_DICTIONARY_URL}?{urlencode({'community': community, 'temporal': temporal})}"


# --------------------------------------------------------------------------- #
# Request planning
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PowerRequest:
    """One API-legal request, plus the identity needed to reassemble the result.

    ``site`` is ``None`` for regional requests. For point requests it carries
    the configured site name, because the response has **no site dimension** --
    it comes back as ``(time, lat=1, lon=1)``, structurally identical to a
    regional response -- so the caller must label it before concatenating.
    """

    url: str
    temporal: str
    mode: str
    params: tuple[str, ...]
    community: str
    start: str
    end: str
    fmt: str = "JSON"
    time_standard: str = "UTC"
    site: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    #: The tile this request covers (regional only), so the caller can see the
    #: tiling that was chosen without re-parsing the URL.
    bbox: Mapping[str, float] | None = None
    #: Which parent dataset this request's parameters belong to. Set when the
    #: planner split by family, so the response's ``header.sources`` is
    #: attributable to a single dataset.
    family: Family | None = None

    @property
    def suffix(self) -> str:
        """Filename suffix matching :attr:`fmt`."""
        return _FORMAT_SUFFIX.get(self.fmt.upper(), ".dat")


def _chunk(items: Sequence[str], size: int) -> list[tuple[str, ...]]:
    return [tuple(items[i : i + size]) for i in range(0, len(items), size)]


def _split_span(lo: float, hi: float) -> list[tuple[float, float]]:
    """Split ``[lo, hi]`` into pieces the regional endpoint accepts.

    Every piece is at most 10 degrees (the API maximum). The subtlety is the
    *minimum*: naively chunking 21 degrees into 10 + 10 + 1 produces a
    1-degree sliver that is itself a 422. An even split -- ``span / ceil(span
    / 10)`` -- cannot do that, because for any span over 2 degrees each equal
    piece is at least 5 degrees.
    """
    span = hi - lo
    if span <= REGIONAL_MAX_SPAN_DEGREES:
        return [(lo, hi)]

    count = math.ceil(span / REGIONAL_MAX_SPAN_DEGREES)
    step = span / count
    edges = [lo + i * step for i in range(count)] + [hi]
    return [(edges[i], edges[i + 1]) for i in range(count)]


def tile_bbox(bbox: Mapping[str, float]) -> list[dict[str, float]]:
    """Split ``bbox`` into API-legal tiles, each 2-10 degrees on both axes.

    Tiles are contiguous and non-overlapping *as requested*. Whether they
    overlap in the *response* depends on the parameter's parent grid: POWER's
    bounding box is inclusive at both ends, so MERRA-2 (whose 0.5 degree grid
    has a node on integer degrees) returns a shared row at a tile boundary,
    while CERES 1.0 degree (centred on half-degrees) does not. Mosaicking
    handles the duplicate geometrically; see ``nasa_power.gdalio.mosaic``.
    """
    missing = {"lat_min", "lat_max", "lon_min", "lon_max"} - set(bbox)
    if missing:
        raise PowerValidationError(f"bbox is missing {', '.join(sorted(missing))}.")
    for axis, lo_key, hi_key in (
        ("latitude", "lat_min", "lat_max"),
        ("longitude", "lon_min", "lon_max"),
    ):
        span = bbox[hi_key] - bbox[lo_key]
        if span_below_minimum(span):
            raise PowerValidationError(
                f"POWER regional requires at least a {REGIONAL_MIN_SPAN_DEGREES:g} degree "
                f"range in {axis}; got {span:g}. Use point mode for a smaller area."
            )

    lat_tiles = _split_span(bbox["lat_min"], bbox["lat_max"])
    lon_tiles = _split_span(bbox["lon_min"], bbox["lon_max"])
    return [
        {"lat_min": a, "lat_max": b, "lon_min": c, "lon_max": d}
        for a, b in lat_tiles
        for c, d in lon_tiles
    ]


def count_requests(
    temporal: str,
    mode: str,
    params: Sequence[str],
    *,
    sites: Sequence[Mapping[str, Any]] | None = None,
    bbox: Mapping[str, float] | None = None,
    split_by_family: bool = True,
) -> int:
    """How many HTTP requests an ask would take, without building any.

    Drives the "6 requests" badge in the dock, so the user sees the cost of a
    continental bounding box *before* pressing Fetch rather than after.
    """
    if mode == "point":
        groups = (
            list(family_groups(params).values()) if split_by_family else [list(params)]
        )
        per_site = sum(len(_chunk(group, POINT_MAX_PARAMS)) for group in groups)
        # No sites means no requests. Flooring at one made the dock's badge
        # promise "1 request." with an empty site list, and then refuse the
        # fetch with an internal message when the button was pressed.
        return per_site * len(sites or ())
    if bbox is None:
        return 0
    return len(tile_bbox(bbox)) * len(params)


def plan_requests(
    temporal: str,
    mode: str,
    params: Sequence[str],
    *,
    start: str | date | datetime,
    end: str | date | datetime,
    sites: Sequence[Mapping[str, Any]] | None = None,
    bbox: Mapping[str, float] | None = None,
    community: str = "RE",
    fmt: str = "JSON",
    time_standard: str = "UTC",
    split_by_family: bool = True,
) -> list[PowerRequest]:
    """Split an ask into API-legal requests.

    Point requests fan out over sites (the API serves one coordinate each),
    over parent-dataset families, and then over parameters in chunks of 20.
    Regional requests fan out over tiles and then over parameters, because the
    regional endpoint serves exactly one parameter per request.

    ``split_by_family`` is what makes provenance attributable. POWER's
    ``header.sources`` is per *request*, so a mixed ask returns
    ``['MERRA2', 'SYN1DEG']`` and neither value can be traced to a dataset.
    Splitting costs one extra request per additional family per site and buys
    a defensible answer to "where did this number come from".
    """
    start_str = format_power_date(start, temporal)
    end_str = format_power_date(end, temporal)
    common: dict[str, Any] = dict(
        temporal=temporal,
        mode=mode,
        community=community,
        start=start_str,
        end=end_str,
        fmt=fmt.upper(),
        time_standard=time_standard.upper(),
    )

    requests: list[PowerRequest] = []

    if mode == "point":
        if not sites:
            raise PowerValidationError("point mode requires at least one site.")

        if split_by_family:
            grouped = [(fam, names) for fam, names in family_groups(params).items()]
        else:
            grouped = [(None, list(params))]

        for site in sites:
            if "latitude" not in site or "longitude" not in site:
                raise PowerValidationError(
                    f"Site {site!r} needs both 'latitude' and 'longitude'."
                )
            for family, names in grouped:
                for chunk in _chunk(names, POINT_MAX_PARAMS):
                    url = build_power_url(
                        temporal,
                        mode,
                        chunk,
                        start=start,
                        end=end,
                        latitude=site["latitude"],
                        longitude=site["longitude"],
                        community=community,
                        fmt=fmt,
                        time_standard=time_standard,
                    )
                    requests.append(
                        PowerRequest(
                            url=url,
                            params=chunk,
                            site=site.get("name"),
                            latitude=site["latitude"],
                            longitude=site["longitude"],
                            family=family,
                            **common,
                        )
                    )
        return requests

    if bbox is None:
        raise PowerValidationError("regional mode requires a bbox.")

    tiles = tile_bbox(bbox)
    if len(tiles) > 1:
        logger.info(
            "POWER regional bbox spans more than %g degrees; tiling into %d requests.",
            REGIONAL_MAX_SPAN_DEGREES,
            len(tiles) * len(params),
        )
    for parameter in params:
        for tile in tiles:
            url = build_power_url(
                temporal,
                mode,
                [parameter],
                start=start,
                end=end,
                bbox=tile,
                community=community,
                fmt=fmt,
                time_standard=time_standard,
            )
            requests.append(
                PowerRequest(
                    url=url,
                    params=(parameter,),
                    bbox=tile,
                    family=family_of(parameter),
                    **common,
                )
            )
    return requests


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #


def _slug(text: str) -> str:
    """Reduce text to a filesystem-safe token."""
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()


def cache_path(cache_dir: str | Path, request: PowerRequest) -> Path:
    """The deterministic cache location for ``request``.

    Layout is ``<cache_dir>/<temporal>/<community>/<mode>/<slug>-<hash><suffix>``.
    The slug keeps the directory browsable; the hash is taken over the **full
    URL**, so any difference that changes the response -- parameters, window,
    coordinates, format, time standard -- changes the path. Hashing the URL
    rather than a hand-picked field list means a query field added later can
    never silently alias onto an existing entry.

    The suffix comes from the request's format. DAVINCI hardcoded ``.nc``,
    which was harmless there because it only ever asked for NetCDF; here point
    requests are JSON and a ``.nc`` name would be a lie on disk.
    """
    digest = hashlib.sha256(request.url.encode()).hexdigest()[:12]
    if request.site is not None:
        label = _slug(request.site)
    elif request.mode == "regional":
        label = _slug("-".join(request.params))
    else:
        label = _slug(f"{request.latitude}-{request.longitude}")
    name = f"{label}-{request.start}-{request.end}-{digest}{request.suffix}"
    return Path(cache_dir) / request.temporal / request.community / request.mode / name


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #


def _default_sleep(seconds: float) -> None:
    """Sleep between retries. Injectable so tests do not actually wait."""
    time.sleep(seconds)


def fetch_with_retries(
    url: str,
    fetcher: Fetcher,
    *,
    max_tries: int = DEFAULT_MAX_TRIES,
    timeout: float = DEFAULT_TIMEOUT,
    sleep: Callable[[float], None] = _default_sleep,
) -> bytes:
    """Fetch ``url``, retrying only what is worth retrying.

    429 and 5xx get exponential backoff. A 422 is a validation failure -- the
    same request fails identically forever -- so it is raised on the first
    attempt with the API's own message and the URL attached.
    """
    last: PowerHTTPError | None = None
    for attempt in range(1, max_tries + 1):
        try:
            return fetcher.fetch(url, timeout)
        except PowerHTTPError as exc:
            if not exc.is_retryable:
                raise
            last = exc
            if attempt < max_tries:
                backoff = 2.0 ** (attempt - 1)
                logger.warning(
                    "POWER HTTP %s (attempt %d/%d); retrying in %.0fs",
                    exc.status,
                    attempt,
                    max_tries,
                    backoff,
                )
                sleep(backoff)
    assert last is not None  # only reachable after a retryable failure
    raise last


def fetch_to_cache(
    request: PowerRequest,
    cache_dir: str | Path,
    fetcher: Fetcher | None = None,
    *,
    force: bool = False,
    offline: bool = False,
    max_tries: int = DEFAULT_MAX_TRIES,
    timeout: float = DEFAULT_TIMEOUT,
    sleep: Callable[[float], None] = _default_sleep,
) -> tuple[Path, bool]:
    """Return the cached body for ``request``, fetching it if needed.

    Returns ``(path, was_cached)``. ``was_cached`` drives the ``CACHE_HIT`` QA
    finding and lets the UI explain why a re-fetch took no time at all.

    The cache is a correctness feature, not only a speed one: POWER's docs warn
    that a client which "persists in requesting the same relative location" may
    be blocked, so a hit must never issue a request. That claim is testable --
    pass a fetcher that raises when called.

    Parameters
    ----------
    force
        Re-fetch even on a cache hit.
    offline
        Never fetch; a miss raises :class:`PowerCacheMiss`.
    """
    path = cache_path(cache_dir, request)
    if path.exists() and not force:
        logger.debug("POWER cache hit: %s", path)
        return path, True

    if offline:
        raise PowerCacheMiss(
            f"Nothing cached for {request.temporal}/{request.mode} "
            f"[{request.start}..{request.end}] and offline mode is on.\n"
            f"Turn off offline mode, or fetch this request from the NASA POWER "
            f"panel:\n  {request.url}"
        )
    if fetcher is None:
        raise PowerValidationError(
            "fetch_to_cache needs a fetcher when the cache misses. Pass "
            "UrllibFetcher() for a plain fetch, or QgisFetcher(feedback) inside "
            "a QgsTask."
        )

    body = fetch_with_retries(
        request.url, fetcher, max_tries=max_tries, timeout=timeout, sleep=sleep
    )

    # Write via a temp file in the same directory and rename. An interrupted
    # write must never leave a truncated file that a later run -- which never
    # re-fetches on a hit -- would read as valid data forever.
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique per writer: a fixed "<name>.partial" lets two threads fetching the
    # same URL truncate each other's temp file, which is exactly the torn cache
    # entry the atomic write exists to prevent.
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident():x}.partial")
    try:
        tmp.write_bytes(body)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    logger.debug("POWER cached %d bytes to %s", len(body), path)
    return path, False
