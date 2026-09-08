"""The one headless ``QgsApplication``, and the base class every ring-2 test uses.

Two things here are load-bearing, both measured on QGIS-final-4_2_2.app:

* **No ``setPrefixPath()``.** With no call, 35 providers register (gdal, ogr,
  mdal, wms, ...). Calling
  ``setPrefixPath('<app>/Contents/Resources/qgis', True)`` drops that to 17 --
  mdal and wms vanish, with nothing on stderr -- because it derives a
  nonexistent ``.../Resources/qgis/Contents/PlugIns/qgis``. So the bootstrap is
  ``QgsApplication([], False)`` then ``initQgis()``, and nothing else.
* **One application per process.** It is built once, here, at import, and the
  reference is held at module scope. Constructing a second one in the same
  interpreter aborts the process, so every test module imports this one rather
  than making its own.

``initQgis()`` is what loads the provider registry and, with it, the default
style -- the 35 colour ramps ``styling`` looks names up in do not exist before
it runs.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from qgis.core import QgsApplication, QgsProject

from nasa_power.core.decode import Observation
from nasa_power.core.provenance import Family

#: Repository root: tests/qgis/qgis_case.py -> tests/qgis -> tests -> root.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: The same committed API captures the ring-1 suite reads.
FIXTURES = REPO_ROOT / "tests" / "fixtures"

# Built at import, never torn down: exitQgis() at process exit is not worth the
# segfault risk in an interpreter that still holds Python wrappers for C++
# objects the application owns.
QGIS_APP = QgsApplication([], False)
QGIS_APP.initQgis()


def load_json(name: str) -> Any:
    """Parse the fixture named ``name`` as JSON."""
    return json.loads(load_bytes(name).decode("utf-8"))


def load_bytes(name: str) -> bytes:
    """Return the fixture named ``name`` verbatim, as it came off the wire."""
    return (FIXTURES / name).read_bytes()


#: Boulder, the coordinate every committed point fixture was fetched at.
BOULDER = (-105.27, 40.02)

#: One day, in seconds. Daily observations are 24 h wide.
_DAY = timedelta(days=1)


def observation(
    day: int,
    value: float | None,
    *,
    parameter: str = "T2M",
    site: str = "Boulder",
    longitude: float = BOULDER[0],
    latitude: float = BOULDER[1],
    native_value: float | None = None,
) -> Observation:
    """One synthetic daily observation in February 2024, for cases no fixture covers.

    Shared rather than copied per test module: layer building and styling both
    need the same long-form record, and two drifting copies of it would let one
    file's idea of a fill value or a timestep quietly stop matching the other's.

    ``value`` is the canonical (converted) number and ``native_value`` defaults
    to it. Pass ``native_value`` explicitly where the two must differ -- styling
    on the ``native_value`` column needs its own spread.
    """
    start = datetime(2024, 2, day, tzinfo=timezone.utc)
    return Observation(
        site=site,
        parameter=parameter,
        t_start=start,
        t_end=start + _DAY,
        value=value,
        units="K",
        native_value=value if native_value is None else native_value,
        native_units="C",
        longitude=longitude,
        latitude=latitude,
        elevation_m=1801.15,
        cell_longitude=-105.0,
        cell_latitude=40.0,
        family=Family.METEOROLOGY,
        temporal="daily",
    )


class QgisTestCase(unittest.TestCase):
    """Base class: a clean ``QgsProject`` around every test.

    ``QgsProject.instance()`` is global mutable state, and several things under
    test reach for it implicitly -- ``QgsCoordinateTransform`` takes it as its
    transform context, layer metadata writes into it. Clearing before and after
    keeps a layer added by one test out of the next one's ``mapLayers()``.
    """

    def setUp(self) -> None:
        super().setUp()
        QgsProject.instance().clear()
        self.addCleanup(QgsProject.instance().clear)
