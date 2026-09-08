#!/usr/bin/env python3
"""Re-download ``tests/fixtures/`` from the live NASA POWER API.

The fixtures are committed so the whole test suite runs offline. Each one was
chosen because it pins a *trap*, not because it is a nice example -- the
monthly response is here for its thirteenth month, the tile pair for its shared
edge row, the 1983 request for the parameter POWER silently drops. The
``why`` field on each entry says which, and that text is the reason to keep the
fixture when someone later wonders if it can go.

Run with no arguments to refresh everything::

    ./scripts/refresh_fixtures.py

or name a subset::

    ./scripts/refresh_fixtures.py point_daily_2param regional_monthly_t2m

Deliberately stdlib-only and deliberately slow (one request at a time, with a
pause): POWER publishes no rate limit but its docs warn that a client which
"persists in requesting the same relative location" may be blocked.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode

BASE = "https://power.larc.nasa.gov/api"
TEMPORAL_BASE = f"{BASE}/temporal"
DICT_URL = f"{BASE}/system/manager/parameters"

FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "fixtures"
)

#: Seconds between requests. Politeness, not a measured requirement.
DELAY = 1.0
TIMEOUT = 120.0
USER_AGENT = "qgis-nasa-power-plugin/0.1.0 (fixture refresh; fillmore@ucar.edu)"

# Boulder, CO -- the site every trap in POWER.md was measured against.
BOULDER = {"latitude": 40.02, "longitude": -105.27}


def _url(temporal: str, mode: str, **params) -> str:
    """Build a POWER URL with the query in the canonical key order."""
    ordered = [
        ("parameters", params.pop("parameters")),
        ("community", params.pop("community", "RE")),
    ]
    for key in ("latitude", "longitude", "latitude-min", "latitude-max", "longitude-min", "longitude-max"):
        if key in params:
            ordered.append((key, params.pop(key)))
    for key in ("start", "end"):
        if key in params:
            ordered.append((key, params.pop(key)))
    ordered.append(("format", params.pop("format", "JSON")))
    ordered.append(("time-standard", params.pop("time-standard", "UTC")))
    ordered.extend(sorted(params.items()))
    return f"{TEMPORAL_BASE}/{temporal}/{mode}?{urlencode(ordered)}"


#: name -> (url, why it is worth committing)
FIXTURES: dict[str, tuple[str, str]] = {
    # ---- point, JSON ------------------------------------------------- #
    "point_daily_2param.json": (
        _url("daily", "point", parameters="T2M,ALLSKY_SFC_SW_DWN",
             start="20240201", end="20240203", **BOULDER),
        "The baseline point response. Two parameters from two different parents, "
        "so header.sources is ['MERRA2','SYN1DEG'] and attributable to neither -- "
        "the case that forces family-splitting.",
    ),
    "point_monthly_yyyy13.json": (
        _url("monthly", "point", parameters="T2M", start="2020", end="2021", **BOULDER),
        "Monthly keys are YYYYMM and YYYY13 is the ANNUAL MEAN, not a month. "
        "Left in, it is a spurious point every 13th step. Also pins that monthly "
        "takes year-only dates.",
    ),
    "point_hourly_utc.json": (
        _url("hourly", "point", parameters="ALLSKY_SFC_SW_DWN",
             start="20240601", end="20240601", **BOULDER),
        "Hourly solar in UTC. Units are Wh/m^2 (NOT kW-hr/m^2/day), and the "
        "Wh/m^2 -> W m-2 conversion is x1, not x3600.",
    ),
    "point_hourly_lst.json": (
        _url("hourly", "point", parameters="ALLSKY_SFC_SW_DWN",
             start="20240601", end="20240601", **{**BOULDER, "time-standard": "LST"}),
        "The same hour in Local Solar Time. Pairs with point_hourly_utc.json to "
        "pin the ~7 h phase shift at Boulder -- the silent error POWER's own "
        "default produces.",
    ),
    "point_1983_partial.json": (
        _url("daily", "point", parameters="T2M,ALLSKY_SFC_SW_DWN",
             start="19830601", end="19830603", **BOULDER),
        "HTTP 200 that silently OMITS a requested parameter. Radiation starts "
        "1984-01-01, but with a met parameter present POWER returns 200 with only "
        "T2M in properties.parameter plus a note in messages[]. A parser keyed on "
        "the requested list KeyErrors here.",
    ),
    "point_solar_2000.json": (
        _url("daily", "point", parameters="ALLSKY_SFC_SW_DWN",
             start="20001230", end="20001231", **BOULDER),
        "Solar before the transition: header.sources is ['SRB'], not CERES.",
    ),
    "point_solar_2001.json": (
        _url("daily", "point", parameters="ALLSKY_SFC_SW_DWN",
             start="20010101", end="20010102", **BOULDER),
        "Solar after the transition: header.sources is ['SYN1DEG']. With the 2000 "
        "fixture this bisects the SRB -> CERES seam to 2001-01-01.",
    ),
    "regional_daily_fc.json": (
        _url("daily", "regional", parameters="T2M", start="20240201", end="20240201",
             **{"latitude-min": 40, "latitude-max": 42,
                "longitude-min": -106, "longitude-max": -104}),
        "Regional JSON is a GeoJSON FeatureCollection of cell CENTRES. Used to "
        "cross-check the NetCDF raster's georeferencing against a second wire "
        "format that must agree cell for cell.",
    ),
    # ---- regional, NetCDF -------------------------------------------- #
    "regional_daily_t2m_tileN.nc": (
        _url("daily", "regional", parameters="T2M", start="20240201", end="20240203",
             format="NETCDF",
             **{"latitude-min": 40, "latitude-max": 42,
                "longitude-min": -106, "longitude-max": -104}),
        "North tile of a MERRA-2 pair. Its 0.5 deg grid has a node exactly on the "
        "lat-40.0 boundary, so it shares an edge row with the south tile.",
    ),
    "regional_daily_t2m_tileS.nc": (
        _url("daily", "regional", parameters="T2M", start="20240201", end="20240203",
             format="NETCDF",
             **{"latitude-min": 38, "latitude-max": 40,
                "longitude-min": -106, "longitude-max": -104}),
        "South tile. Mosaicking these two must yield 9 rows, not 10.",
    ),
    "regional_solar_tileN.nc": (
        _url("daily", "regional", parameters="ALLSKY_SFC_SW_DWN",
             start="20240201", end="20240203", format="NETCDF",
             **{"latitude-min": 40, "latitude-max": 42,
                "longitude-min": -106, "longitude-max": -104}),
        "CERES solar on its native 1.0 deg grid, centred on half-degrees -- so an "
        "integer tile boundary falls BETWEEN cells and nothing is duplicated. The "
        "counter-example to the MERRA-2 pair, and proof the two families are not "
        "co-registered.",
    ),
    "regional_solar_tileS.nc": (
        _url("daily", "regional", parameters="ALLSKY_SFC_SW_DWN",
             start="20240201", end="20240203", format="NETCDF",
             **{"latitude-min": 38, "latitude-max": 40,
                "longitude-min": -106, "longitude-max": -104}),
        "South solar tile. Mosaicking with tileN must yield 4 rows and no "
        "duplicate.",
    ),
    "regional_monthly_t2m.nc": (
        _url("monthly", "regional", parameters="T2M", start="2020", end="2021",
             format="NETCDF",
             **{"latitude-min": 40, "latitude-max": 42,
                "longitude-min": -106, "longitude-max": -104}),
        "The YYYY13 trap on the RASTER path, and the highest-consequence one: 26 "
        "bands for 24 months, NETCDF_DIM_time carrying 202013/202113, and NO "
        "time#units attribute at all, so there is nothing to CF-decode against.",
    ),
}

#: Parameter dictionaries, one per community -- these pin that radiation units
#: depend on the community, which a catalog keyed only on (parameter, temporal)
#: gets wrong.
DICTIONARIES = {
    f"dict_daily_{community}.json": (
        f"{DICT_URL}?{urlencode({'community': community, 'temporal': 'daily'})}",
        f"Community {community} parameter dictionary. Radiation units differ per "
        f"community (RE kW-hr/m^2/day, AG MJ/m^2/day, SB W m-2) while T2M is C in "
        f"all three.",
    )
    for community in ("RE", "AG", "SB")
}

#: Error responses. Committed so error handling is testable offline, and
#: because the 404 is HTML rather than JSON -- which is the whole problem.
ERRORS = {
    "error_422_bbox.json": (
        _url("daily", "regional", parameters="T2M", start="20240201", end="20240201",
             **{"latitude-min": 40, "latitude-max": 41,
                "longitude-min": -106, "longitude-max": -104}),
        "A 1 deg bbox: 422 'Please provide at least a 2 degree range in latitude'. "
        "The binding regional constraint is a MINIMUM, not just a maximum.",
    ),
    "error_404_hourly_regional.html": (
        _url("hourly", "regional", parameters="T2M", start="20240201", end="20240201",
             **{"latitude-min": 40, "latitude-max": 42,
                "longitude-min": -106, "longitude-max": -104}),
        "hourly/regional does not exist, and it fails as a 19 KB text/html page "
        "rather than a JSON API error -- so it must be refused locally or the user "
        "sees a wall of markup with no hint the combination is unsupported.",
    ),
}

ALL: dict[str, tuple[str, str]] = {**FIXTURES, **DICTIONARIES, **ERRORS}


def fetch(url: str) -> tuple[int, bytes, str]:
    """GET ``url``, returning ``(status, body, content_type)``.

    An error status is a normal outcome here -- two fixtures *are* error
    responses -- so this returns rather than raises.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read(), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type", "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "names", nargs="*",
        help="fixture names (with or without extension); default is all",
    )
    parser.add_argument("--list", action="store_true", help="list fixtures and why each exists")
    parser.add_argument("--dry-run", action="store_true", help="print URLs without fetching")
    args = parser.parse_args(argv)

    if args.list:
        for name, (url, why) in ALL.items():
            print(f"{name}\n  {why}\n  {url}\n")
        return 0

    if args.names:
        wanted = {}
        for requested in args.names:
            matches = [n for n in ALL if n == requested or n.rsplit(".", 1)[0] == requested]
            if not matches:
                print(f"unknown fixture: {requested}", file=sys.stderr)
                return 2
            wanted.update({m: ALL[m] for m in matches})
    else:
        wanted = ALL

    os.makedirs(FIXTURE_DIR, exist_ok=True)
    failures = 0

    for i, (name, (url, _why)) in enumerate(sorted(wanted.items())):
        if args.dry_run:
            print(f"{name}\n  {url}")
            continue
        if i:
            time.sleep(DELAY)

        status, body, content_type = fetch(url)
        expects_error = name.startswith("error_")
        ok = (status >= 400) if expects_error else (status == 200)
        if not ok:
            print(f"FAIL {name}: HTTP {status} ({content_type})", file=sys.stderr)
            print(f"     {url}", file=sys.stderr)
            print(f"     {body[:300].decode('utf-8', 'replace')}", file=sys.stderr)
            failures += 1
            continue

        # Pretty-print JSON so diffs on a refresh are readable rather than one
        # 40 KB line. NetCDF and HTML are written as received.
        if name.endswith(".json"):
            body = json.dumps(json.loads(body), indent=1, sort_keys=False).encode() + b"\n"

        path = os.path.join(FIXTURE_DIR, name)
        with open(path, "wb") as handle:
            handle.write(body)
        print(f"  {name:34s} HTTP {status}  {len(body):8,d} B  {content_type}")

    # The URLs are the fixtures' provenance: without them nobody can tell what a
    # committed .nc actually asked for.
    if not args.dry_run and not failures:
        manifest = {name: {"url": url, "why": why} for name, (url, why) in sorted(ALL.items())}
        with open(os.path.join(FIXTURE_DIR, "MANIFEST.json"), "w") as handle:
            json.dump(manifest, handle, indent=1)
            handle.write("\n")
        print(f"\nwrote MANIFEST.json ({len(manifest)} fixtures)")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
