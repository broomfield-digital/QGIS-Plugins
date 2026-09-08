"""Persisted plugin preferences.

Plain :class:`QgsSettings` under one namespaced prefix, rather than the newer
``QgsSettingsTree.createPluginTreeNode`` entries. The typed-entry API is nicer
to read, but it is a *registration* that has to be unregistered on unload, and
a missed teardown survives a plugin reload as a stale node -- the same class of
leak that produces duplicate toolbar icons and stale-pointer crashes.
``QgsSettings`` has nothing to leak, works identically headless, and still
drives an Options page.

Every value here is a preference, not state: losing the file costs the user
their defaults and nothing else.
"""

from __future__ import annotations

from qgis.core import QgsSettings

#: One prefix for everything, so `QgsSettings().remove(PREFIX)` is a complete
#: uninstall and nothing of ours is loose in the global namespace.
PREFIX = "nasa_power"

# Keys. Named as constants so a typo is an ImportError rather than a silently
# defaulted setting.
KEY_CACHE_DIR = f"{PREFIX}/cache_dir"
KEY_COMMUNITY = f"{PREFIX}/community"
KEY_TEMPORAL = f"{PREFIX}/temporal"
KEY_TIME_STANDARD = f"{PREFIX}/time_standard"
KEY_CONVERT_TO_SI = f"{PREFIX}/convert_to_si"
KEY_ADD_TO_MAP = f"{PREFIX}/add_to_map"
KEY_AUTO_STYLE = f"{PREFIX}/auto_style"
KEY_MAX_CONCURRENCY = f"{PREFIX}/max_concurrency"
KEY_LAST_PARAMETERS = f"{PREFIX}/last_parameters"

#: Concurrent requests. POWER publishes no rate limit but asks not to be
#: hammered; four is enough to hide latency on a tiled fetch without looking
#: like a scraper.
DEFAULT_MAX_CONCURRENCY = 4


def _settings() -> QgsSettings:
    return QgsSettings()


def get_str(key: str, default: str = "") -> str:
    value = _settings().value(key, default)
    return default if value is None else str(value)


def set_str(key: str, value: str) -> None:
    _settings().setValue(key, value)


def get_bool(key: str, default: bool) -> bool:
    # QgsSettings round-trips booleans as the strings "true"/"false" on some
    # backends, so the type must be requested explicitly rather than inferred.
    return bool(_settings().value(key, default, type=bool))


def set_bool(key: str, value: bool) -> None:
    _settings().setValue(key, bool(value))


def get_int(key: str, default: int) -> int:
    try:
        return int(_settings().value(key, default, type=int))
    except (TypeError, ValueError):
        return default


def set_int(key: str, value: int) -> None:
    _settings().setValue(key, int(value))


def get_list(key: str) -> list[str]:
    """A stored string list. Absent and empty are the same thing here."""
    value = _settings().value(key, [])
    if isinstance(value, str):
        return [v for v in value.split(",") if v]
    return [str(v) for v in (value or [])]


def set_list(key: str, values: list[str]) -> None:
    _settings().setValue(key, list(values))


def reset_all() -> None:
    """Forget every stored preference. Used by the Options page's reset."""
    _settings().remove(PREFIX)
