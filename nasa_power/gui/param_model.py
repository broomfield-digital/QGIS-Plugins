"""Feed the parameter combo box from POWER's own dictionary.

Hardcoding a parameter list is wrong for a measured reason: units depend on the
**community**, so the same code means ``kW-hr/m^2/day`` under RE and
``MJ/m^2/day`` under AG. The dictionary endpoint gives the current answer for
whatever the user has selected, along with a definition for the tooltip.

It is used **only when already cached**, plus an explicit "Load all" button.
That is deliberate: the panel must open instantly and work offline, so it
starts from :data:`~nasa_power.core.dictionary.CURATED_PARAMETERS` and improves
itself when a dictionary is to hand. Monthly RE alone returns **1388** entries,
mostly ``_00``…``_23`` hour-of-day variants, so the full list is behind a
toggle rather than being the default.
"""

from __future__ import annotations

from qgis.core import Qgis, QgsMessageLog, QgsTask

from nasa_power.core.dictionary import (
    CURATED_PARAMETERS,
    ParameterInfo,
    filter_parameters,
    load_dictionary,
)
from nasa_power.core.errors import PowerError

LOG_TAG = "NASA POWER"


def curated_infos(
    parameters: dict[str, ParameterInfo] | None = None
) -> list[ParameterInfo]:
    """The starting selection, enriched from a dictionary when one is loaded."""
    if not parameters:
        return [ParameterInfo(name=code, units="", long_name="") for code in CURATED_PARAMETERS]
    out: list[ParameterInfo] = []
    for code in CURATED_PARAMETERS:
        info = parameters.get(code)
        out.append(info or ParameterInfo(name=code, units="", long_name=""))
    return out


def cached_dictionary(
    community: str, temporal: str, cache_dir
) -> dict[str, ParameterInfo] | None:
    """Load a dictionary from disk only. ``None`` if there is none.

    Never fetches: the panel has to open without waiting on the network, and a
    missing dictionary is not an error -- the curated list covers it.
    """
    try:
        parameters, _from_cache = load_dictionary(
            community, temporal, cache_dir, fetcher=None
        )
        return parameters
    except (PowerError, OSError):
        return None


class DictionaryTask(QgsTask):
    """Fetch one parameter dictionary in the background.

    A dictionary is ~52 KB and cached for a month, so this runs at most once
    per (community, temporal) per month. It is still a task rather than a
    blocking call, because "the panel froze when I changed a dropdown" is
    exactly the behaviour the whole threading design exists to avoid.
    """

    def __init__(self, community: str, temporal: str, cache_dir) -> None:
        super().__init__(
            f"NASA POWER parameter list ({community}/{temporal})", QgsTask.Flag.CanCancel
        )
        self.community = community
        self.temporal = temporal
        self.cache_dir = cache_dir
        self.parameters: dict[str, ParameterInfo] | None = None
        self.error = ""
        #: Called on the main thread from ``finished()``.
        self.on_complete = None

    def run(self) -> bool:
        from nasa_power.qgis_bridge.net import QgisFetcher

        try:
            self.parameters, _from_cache = load_dictionary(
                self.community, self.temporal, self.cache_dir, QgisFetcher()
            )
            return True
        except (PowerError, OSError) as exc:
            self.error = str(exc)
            QgsMessageLog.logMessage(
                f"Could not load the POWER parameter list: {exc}",
                LOG_TAG,
                Qgis.MessageLevel.Warning,
            )
            return False

    def finished(self, result: bool) -> None:
        if self.on_complete is not None:
            self.on_complete(self.parameters if result else None, self.error)


def choices(
    parameters: dict[str, ParameterInfo] | None,
    *,
    show_all: bool,
    include_hour_variants: bool = False,
) -> list[ParameterInfo]:
    """What the combo box should show, given what has been loaded."""
    if parameters is None:
        return curated_infos(None)
    if not show_all:
        return curated_infos(parameters)
    return filter_parameters(parameters, include_hour_variants=include_hour_variants)
