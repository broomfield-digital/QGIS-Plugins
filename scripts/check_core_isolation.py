#!/usr/bin/env python3
"""Fail if ``nasa_power.core`` imports anything outside the standard library.

Ring 1 is the plugin's whole correctness surface -- URL construction, the
2-10 degree bbox window, parameter caps, unit conversion, the monthly YYYY13
trap, provenance. It is worth testing on any Python in under a second, and it
stays that way only if nothing drags in qgis, numpy, GDAL or Qt.

Run as a subprocess (``make guard``) rather than as a unittest: a test runner
that has already imported numpy for its own reasons would mask exactly the
leak this is looking for.
"""

from __future__ import annotations

import importlib
import os
import sys

#: Every ring-1 module. Listed rather than globbed so a new module has to be
#: added here deliberately -- a glob would silently skip one that fails to
#: import at all.
CORE_MODULES = (
    "api",
    "citation",
    "decode",
    "dictionary",
    "display",
    "errors",
    "fetcher",
    "provenance",
    "qa",
    "timeaxis",
    "units",
)

#: Top-level packages ring 1 may never pull in, directly or transitively.
FORBIDDEN = frozenset(
    {
        "qgis",
        "osgeo",
        "numpy",
        "PyQt5",
        "PyQt6",
        "pandas",
        "xarray",
        "requests",
        "scipy",
        "matplotlib",
        "netCDF4",
    }
)


def main() -> int:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    missing: list[str] = []
    for name in CORE_MODULES:
        try:
            importlib.import_module(f"nasa_power.core.{name}")
        except ModuleNotFoundError as exc:
            # A module not yet written is a gap, not a leak -- report it
            # separately so an in-progress milestone reads clearly.
            if exc.name == f"nasa_power.core.{name}":
                missing.append(name)
                continue
            raise

    leaked = sorted(m for m in sys.modules if m.split(".")[0] in FORBIDDEN)

    if missing:
        print(f"NOT YET WRITTEN: {missing}")
    print(f"LEAKED: {leaked}")

    if leaked:
        print(
            "\nRing 1 must stay stdlib-only so tests/unit runs on any Python.\n"
            "Move whatever needs these into nasa_power/gdalio/ or "
            "nasa_power/qgis_bridge/.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
