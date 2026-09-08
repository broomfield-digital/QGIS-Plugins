"""NASA POWER plugin for QGIS 4.

Only :func:`classFactory` belongs here. Every import it needs is deferred
inside the function body: QGIS imports this module while scanning the plugin
directory, long before the user enables the plugin, and a module-level import
that fails there takes the whole scan down rather than reporting one bad
plugin.
"""

from __future__ import annotations

__version__ = "0.1.0"


def classFactory(iface):  # noqa: N802 - name fixed by the QGIS plugin API
    """Return the plugin instance. Called by QGIS with the ``QgisInterface``."""
    from nasa_power.plugin import NasaPowerPlugin

    return NasaPowerPlugin(iface)
