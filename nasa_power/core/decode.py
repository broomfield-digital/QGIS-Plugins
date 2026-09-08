"""Parse POWER JSON into flat observations, and record what the response said.

``format=JSON`` is the wire format for point requests, and it is a genuinely
good one: it **already is GeoJSON** (a ``Feature`` for point, a
``FeatureCollection`` of cell centres for regional), and it carries units, the
fill value, the time standard, the parent datasets and the API version *in
band*. Nothing here has to be assumed or looked up.

Three traps this module exists to absorb:

* **A 200 response can silently omit a requested parameter.** A 1983 request
  for ``T2M,ALLSKY_SFC_SW_DWN`` returns HTTP 200 containing only ``T2M``, with
  an explanation in ``messages[]``. A parser keyed on the *requested* list
  raises ``KeyError``; one keyed on the *response* drops a variable without
  saying so. So both lists are kept and diffed.
* **The geometry echoes the requested coordinate**, not the grid cell the
  values actually came from. POWER serves the nearest cell and never discloses
  which, so the cell centre is derived locally and labelled as derived.
* **Fill is ``-999.0``** and must be masked *before* unit scaling -- scaled, it
  becomes -41625 W/m2, which is not obviously wrong on a colour ramp.

A point response is ``(time, lat=1, lon=1)``: structurally identical to a
regional one. Dispatch is therefore on the **mode that was requested**, never
on the shape of what came back, which is why point and regional have separate
entry points here rather than one clever function.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from nasa_power.core.errors import PowerValidationError
from nasa_power.core.provenance import Family, family_of, snap_to_grid
from nasa_power.core.timeaxis import decode_time_keys, dropped_keys
from nasa_power.core.units import canonical_units, convert_series, describe_conversion


class PowerParseError(PowerValidationError):
    """A response that did not have the shape its endpoint promises."""


@dataclass(frozen=True)
class ResponseFacts:
    """What a response said about itself, kept verbatim.

    Everything here is read from the response rather than assumed, because
    every field has been observed to vary: the API version differs *per
    endpoint* (daily v2.9.7 while monthly was v2.9.8), the time standard
    defaults to LST unless asked otherwise, and the source list depends on both
    the parameters and the era requested.
    """

    #: ``header.sources`` -- per **request**, not per parameter. A mixed
    #: request returns them all, attributable to none, which is why the planner
    #: splits by family.
    sources: tuple[str, ...] = ()
    #: ``header.fill_value``. Read, never hardcoded, even though it has always
    #: been -999.0.
    fill_value: float | None = None
    #: ``header.time_standard`` as **returned**, which is what matters -- a
    #: request can ask for UTC and the endpoint can still answer in LST.
    time_standard: str = ""
    api_name: str = ""
    api_version: str = ""
    title: str = ""
    start: str = ""
    end: str = ""
    #: ``messages[]``, which is non-empty on plenty of 200s: provenance notes,
    #: silently substituted parameters, dropped parameters.
    messages: tuple[str, ...] = ()
    #: ``parameters[PARAM].units`` -- the key every unit conversion is keyed on.
    units: Mapping[str, str] = field(default_factory=dict)
    #: ``parameters[PARAM].longname``.
    long_names: Mapping[str, str] = field(default_factory=dict)
    #: Parameters actually present in the payload.
    returned_parameters: tuple[str, ...] = ()
    #: Parameters the caller asked for, for the diff.
    requested_parameters: tuple[str, ...] = ()

    @property
    def missing_parameters(self) -> tuple[str, ...]:
        """Requested but absent -- the silent-omission case."""
        returned = set(self.returned_parameters)
        return tuple(p for p in self.requested_parameters if p not in returned)

    @property
    def unexpected_parameters(self) -> tuple[str, ...]:
        """Present but not requested -- POWER substituting a parameter."""
        requested = set(self.requested_parameters)
        return tuple(p for p in self.returned_parameters if p not in requested)


@dataclass(frozen=True)
class Observation:
    """One value, at one place, at one time, for one parameter.

    The long form. It costs a repeated geometry per timestep, and it is the
    only layout the QGIS temporal controller can animate:
    ``QgsVectorLayerTemporalProperties`` reads *fields*, not field *names*, so
    a wide layout with one column per timestep cannot drive the time slider at
    all.
    """

    site: str
    parameter: str
    #: Half-open ``[t_start, t_end)`` in UTC.
    t_start: datetime
    t_end: datetime
    #: Converted value, or ``None`` where POWER sent its fill sentinel.
    value: float | None
    #: Units after conversion.
    units: str
    #: Value exactly as served, before masking and scaling. Kept so a layer can
    #: show POWER's own numbers alongside canonical ones without a re-fetch.
    native_value: float | None
    native_units: str
    #: The coordinate that was *requested* (point) or the cell centre the
    #: response reported (regional).
    longitude: float
    latitude: float
    elevation_m: float | None
    #: Grid cell centre, derived locally by snapping. ``None`` for regional,
    #: where the response's own coordinate already *is* the cell centre.
    cell_longitude: float | None
    cell_latitude: float | None
    family: Family
    temporal: str


def _require(payload: Mapping[str, Any], key: str, url: str | None) -> Any:
    try:
        return payload[key]
    except (KeyError, TypeError):
        where = f"\n  {url}" if url else ""
        raise PowerParseError(
            f"POWER response is missing {key!r}. The response schema may have "
            f"changed.{where}"
        ) from None


def read_facts(
    payload: Mapping[str, Any],
    requested: Sequence[str] = (),
    *,
    url: str | None = None,
) -> ResponseFacts:
    """Pull the self-describing fields out of a decoded JSON response."""
    header = payload.get("header") or {}
    if not isinstance(header, Mapping):
        raise PowerParseError(f"POWER response 'header' is not an object.\n  {url or ''}")
    api = header.get("api") or {}
    parameters = payload.get("parameters") or {}

    sources = header.get("sources") or ()
    if isinstance(sources, str):
        sources = (sources,)

    messages = payload.get("messages") or ()
    if isinstance(messages, str):
        messages = (messages,)

    return ResponseFacts(
        sources=tuple(str(s) for s in sources),
        fill_value=header.get("fill_value"),
        time_standard=str(header.get("time_standard", "")),
        api_name=str(api.get("name", "")) if isinstance(api, Mapping) else "",
        api_version=str(api.get("version", "")) if isinstance(api, Mapping) else "",
        title=str(header.get("title", "")),
        start=str(header.get("start", "")),
        end=str(header.get("end", "")),
        messages=tuple(str(m) for m in messages),
        units={k: str(v.get("units", "")) for k, v in parameters.items() if isinstance(v, Mapping)},
        long_names={
            k: str(v.get("longname", "")) for k, v in parameters.items() if isinstance(v, Mapping)
        },
        returned_parameters=tuple(parameters),
        requested_parameters=tuple(requested),
    )


def _observations_from_block(
    block: Mapping[str, Mapping[str, float]],
    *,
    facts: ResponseFacts,
    temporal: str,
    site: str,
    longitude: float,
    latitude: float,
    elevation: float | None,
    derive_cell: bool,
    convert: bool = False,
) -> list[Observation]:
    """Turn one ``properties.parameter`` block into observations."""
    out: list[Observation] = []

    for parameter, series in block.items():
        if not isinstance(series, Mapping):
            raise PowerParseError(
                f"POWER response parameter {parameter!r} is not a mapping of "
                f"time key to value."
            )
        native_units = facts.units.get(parameter, "")
        family = family_of(parameter)
        cell_lat, cell_lon = (
            snap_to_grid(family, latitude, longitude) if derive_cell else (None, None)
        )

        decoded = decode_time_keys(list(series), temporal)
        natives = [series[key] for key, _s, _e in decoded]

        if convert:
            converted = convert_series(
                natives, native_units, temporal, fill_value=facts.fill_value
            )
            canonical = canonical_units(native_units)
        else:
            # Native units are the default, so a layer's numbers match what the
            # POWER website shows and nothing is silently rescaled. Fill is
            # still masked -- that is not a unit choice, it is the difference
            # between a missing value and -999.
            converted = [
                None if (facts.fill_value is not None and v == facts.fill_value) else v
                for v in natives
            ]
            canonical = native_units

        for (key, t_start, t_end), native, value in zip(decoded, natives, converted):
            out.append(
                Observation(
                    site=site,
                    parameter=parameter,
                    t_start=t_start,
                    t_end=t_end,
                    value=value,
                    units=canonical,
                    native_value=None if native == facts.fill_value else native,
                    native_units=native_units,
                    longitude=longitude,
                    latitude=latitude,
                    elevation_m=elevation,
                    cell_longitude=cell_lon,
                    cell_latitude=cell_lat,
                    family=family,
                    temporal=temporal,
                )
            )
    return out


def _geometry(feature: Mapping[str, Any], url: str | None) -> tuple[float, float, float | None]:
    """``(lon, lat, elevation_m)`` from a GeoJSON point geometry."""
    geometry = _require(feature, "geometry", url)
    coords = geometry.get("coordinates") if isinstance(geometry, Mapping) else None
    if not isinstance(coords, (list, tuple)) or len(coords) < 2:
        raise PowerParseError(f"POWER response geometry has no usable coordinates.\n  {url or ''}")
    longitude = float(coords[0])
    latitude = float(coords[1])
    elevation = float(coords[2]) if len(coords) > 2 and coords[2] is not None else None
    return longitude, latitude, elevation


def parse_point_response(
    body: bytes | str | Mapping[str, Any],
    *,
    temporal: str,
    requested: Sequence[str] = (),
    site: str = "site",
    url: str | None = None,
    convert: bool = False,
) -> tuple[list[Observation], ResponseFacts]:
    """Parse a point response: one GeoJSON ``Feature``.

    ``site`` labels the result, because the response has no site dimension of
    its own -- the caller fetched one coordinate per request and has to say
    which.
    """
    payload = _load(body, url)
    facts = read_facts(payload, requested, url=url)

    properties = _require(payload, "properties", url)
    block = properties.get("parameter") if isinstance(properties, Mapping) else None
    if not isinstance(block, Mapping):
        raise PowerParseError(
            f"POWER point response has no 'properties.parameter' block.\n  {url or ''}"
        )

    longitude, latitude, elevation = _geometry(payload, url)
    observations = _observations_from_block(
        block,
        facts=facts,
        temporal=temporal,
        site=site,
        longitude=longitude,
        latitude=latitude,
        elevation=elevation,
        # Point mode: the coordinate is what we asked for, so the answering
        # cell has to be derived.
        derive_cell=True,
        convert=convert,
    )
    return observations, facts


def parse_regional_response(
    body: bytes | str | Mapping[str, Any],
    *,
    temporal: str,
    requested: Sequence[str] = (),
    url: str | None = None,
    convert: bool = False,
) -> tuple[list[Observation], ResponseFacts]:
    """Parse a regional JSON response: a ``FeatureCollection`` of cell centres.

    Used for the small-area vector view and as an independent cross-check on
    the NetCDF raster path -- the two wire formats must agree cell for cell,
    and asserting that catches a georeferencing error that neither format could
    reveal alone.
    """
    payload = _load(body, url)
    facts = read_facts(payload, requested, url=url)

    features = _require(payload, "features", url)
    if not isinstance(features, (list, tuple)):
        raise PowerParseError(f"POWER regional 'features' is not a list.\n  {url or ''}")

    observations: list[Observation] = []
    for index, feature in enumerate(features):
        properties = feature.get("properties") if isinstance(feature, Mapping) else None
        block = properties.get("parameter") if isinstance(properties, Mapping) else None
        if not isinstance(block, Mapping):
            raise PowerParseError(
                f"POWER regional feature {index} has no 'properties.parameter'.\n  {url or ''}"
            )
        longitude, latitude, elevation = _geometry(feature, url)
        observations.extend(
            _observations_from_block(
                block,
                facts=facts,
                temporal=temporal,
                # A cell has no name; its coordinate is its identity.
                site=f"cell_{latitude:g}_{longitude:g}",
                longitude=longitude,
                latitude=latitude,
                elevation=elevation,
                # Regional: the response coordinate already IS the cell centre,
                # so deriving one would only add rounding error.
                derive_cell=False,
                convert=convert,
            )
        )
    return observations, facts


def _load(body: bytes | str | Mapping[str, Any], url: str | None) -> Mapping[str, Any]:
    if isinstance(body, Mapping):
        return body
    try:
        payload = json.loads(body)
    except ValueError as exc:
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
        hint = ""
        if text.lstrip()[:1] == "<":
            hint = (
                " The body is HTML, which usually means the URL did not name a real "
                "endpoint -- hourly/regional does this."
            )
        raise PowerParseError(
            f"POWER response is not JSON: {exc}.{hint}\n  {url or ''}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise PowerParseError(f"POWER response is not a JSON object.\n  {url or ''}")
    return payload


def conversion_note(facts: ResponseFacts, parameter: str, temporal: str) -> str:
    """One line describing what was done to ``parameter``'s values."""
    return describe_conversion(facts.units.get(parameter, ""), temporal)


def annual_means_dropped(
    payload: Mapping[str, Any], temporal: str, parameter: str | None = None
) -> list[str]:
    """Time keys that were discarded as annual means (``YYYY13`` / ``ANN``).

    Surfaced as a QA finding: a user who asked for 2020-2021 and received 24
    values from a 26-key response is owed an explanation of which two went.
    """
    properties = payload.get("properties") or {}
    block = properties.get("parameter") or {}
    if parameter is not None:
        block = {parameter: block.get(parameter, {})}
    keys: list[str] = []
    for series in block.values():
        if isinstance(series, Mapping):
            keys.extend(series)
    return dropped_keys(sorted(set(keys)), temporal)
