"""Ring 1: the POWER API contract, in the standard library only.

Everything here is true without QGIS, so everything here is testable without
QGIS -- ``make test`` runs the whole ring on any Python 3.10+ in under a
second, offline. Nothing in this package may import ``qgis``, ``osgeo``,
``numpy``, Qt, or any third-party module; ``make guard`` enforces that in a
subprocess.

The rule earns its keep because this ring holds every rule that can be got
wrong: the 2-10 degree regional window, the 20/1 parameter caps, the missing
``hourly/regional`` endpoint, year-only monthly dates, the ``YYYY13`` annual
mean masquerading as a thirteenth month, LST-vs-UTC, per-community units,
the SRB-to-CERES provenance seam, and the cache key.
"""
