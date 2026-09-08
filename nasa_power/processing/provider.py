"""The ``nasapower`` Processing provider.

Registered from :meth:`nasa_power.plugin.NasaPowerPlugin.initProcessing`, which
runs on both the desktop and the ``qgis_process`` path. The provider exists
from M0 with no algorithms in it: declaring ``hasProcessingProvider=yes`` in
``metadata.txt`` without a working ``initProcessing`` makes
``qgis.utils.startProcessingPlugin`` hard-fail with "plugin has no
initProcessing() method", so the flag and the provider have to land together.

Algorithms are added in M5.
"""

from __future__ import annotations

import os

from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon

PROVIDER_ID = "nasapower"
PROVIDER_NAME = "NASA POWER"

_ICON_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "icons", "nasa_power.svg")


class PowerProvider(QgsProcessingProvider):
    """Groups the NASA POWER algorithms in the Processing toolbox."""

    def loadAlgorithms(self) -> None:  # noqa: N802 - name fixed by the QGIS API
        """Register this provider's algorithms.

        QGIS calls this on registration and again on every ``refreshAlgorithms``,
        so it must be safe to re-run; ``addAlgorithm`` takes ownership of each
        instance, which is why a fresh one is constructed per call rather than
        held on ``self``.
        """
        # M5: alg_point, alg_regional, alg_citation.
        return

    def id(self) -> str:
        """Stable identifier. Algorithm ids are ``nasapower:<name>``."""
        return PROVIDER_ID

    def name(self) -> str:
        """Provider name as shown in the toolbox tree."""
        return PROVIDER_NAME

    def longName(self) -> str:  # noqa: N802 - name fixed by the QGIS API
        return "NASA POWER (Prediction Of Worldwide Energy Resources)"

    def icon(self) -> QIcon:
        return QIcon(_ICON_PATH)
