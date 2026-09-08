"""Where the download cache lives.

One measured subtlety justifies this being its own module rather than a
one-liner: **``QgsApplication.qgisSettingsDirPath()`` returns a different path
headless than it does in the desktop app** -- the headless form drops the
``QGIS4`` profile segment. Re-deriving the cache directory at each call site
would therefore give ``qgis_process`` a different cache from the dock, and
every command-line run would re-download what the panel already has. Since
POWER's docs warn that a client repeatedly requesting the same location may be
blocked, that is a politeness bug as well as a slow one.

So the path is resolved **once**, written to settings, and read back from there
forever after. The first resolution wins; nothing re-derives it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from qgis.core import QgsApplication

from nasa_power.qgis_bridge import settings

#: Subdirectory under whatever root is chosen.
CACHE_SUBDIR = os.path.join("cache", "nasa_power")


def _platform_cache_root() -> Path:
    """The OS's own cache location, as an **absolute** path.

    Absolute is the load-bearing property, not tidiness: a relative path would
    put the cache in the process's working directory, which differs per
    ``qgis_process`` invocation, so nothing would ever be a cache hit and POWER
    would be re-queried for the same location every run.
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    # XDG, and its documented default.
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")


def default_cache_dir() -> Path:
    """The cache directory to use when the user has not chosen one.

    Inside the QGIS profile, so it travels with the profile and is removed when
    the profile is. Falls back to the platform cache directory when QGIS
    reports no settings path -- which happens in a bare ``QgsApplication``
    before ``initQgis()``.
    """
    root = QgsApplication.qgisSettingsDirPath()
    if root:
        return Path(root) / CACHE_SUBDIR
    return (_platform_cache_root() / "nasa_power").expanduser().resolve()


def resolve_cache_dir(create: bool = True) -> Path:
    """The cache directory, resolved once and then remembered.

    Reading the persisted value rather than re-deriving it is what keeps the
    dock and ``qgis_process`` pointed at the same files.
    """
    stored = settings.get_str(settings.KEY_CACHE_DIR)
    if stored:
        path = Path(stored).expanduser()
    else:
        path = default_cache_dir()
        settings.set_str(settings.KEY_CACHE_DIR, str(path))

    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def set_cache_dir(path: str | Path) -> Path:
    """Point the cache somewhere else and remember it.

    Nothing is moved: existing entries stay where they are and the new
    directory starts empty. Cache entries are named by a hash of their request
    URL, so a directory can safely be shared with another tool using the same
    scheme -- DAVINCI's ``~/.cache/davinci/power`` layout is deliberately
    compatible for NetCDF requests.
    """
    resolved = Path(path).expanduser()
    resolved.mkdir(parents=True, exist_ok=True)
    settings.set_str(settings.KEY_CACHE_DIR, str(resolved))
    return resolved


def cache_size_bytes(path: Path | None = None) -> int:
    """Total size of the cache, for the Options page to display."""
    root = path or resolve_cache_dir(create=False)
    if not root.exists():
        return 0
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
