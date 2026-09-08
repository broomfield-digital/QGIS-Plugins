"""Fixture loading for the ring-1 unit tests.

The fixtures are real API responses captured from power.larc.nasa.gov and
committed under ``tests/fixtures``; ``MANIFEST.json`` records the URL each one
came from and why it is kept. Paths resolve from this file rather than from the
working directory so the suite runs the same from the repo root, from an IDE,
or from ``unittest discover`` with any ``-t``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: Repository root: tests/unit/support.py -> tests/unit -> tests -> root.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Directory holding the committed API fixtures.
FIXTURES = REPO_ROOT / "tests" / "fixtures"


def load_json(name: str) -> Any:
    """Parse the fixture named ``name`` as JSON."""
    return json.loads(load_bytes(name).decode("utf-8"))


def load_bytes(name: str) -> bytes:
    """Return the fixture named ``name`` verbatim, as it came off the wire."""
    return (FIXTURES / name).read_bytes()
