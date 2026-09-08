"""The NASA POWER dock widget.

M0 ships the frame only: the section layout the later milestones fill in, and
a :meth:`NasaPowerDock.teardown` hook so ``plugin.unload()`` has one place to
release anything the panels grab from QGIS (map tools, canvas connections,
running tasks).

Panels land here in order:

* WHERE  -- M2 (map click / typed lat-lon / point layer) and M3 (extent)
* WHAT   -- M2 (parameters, temporal, dates, community, UTC|LST)
* OUTPUT -- M2 (add to map, styling, SI toggle, cache dir)
* QA     -- M2 (findings table, copy citation, copy request URLs)
* PLOT   -- M4 (native QgsLineChartPlot)
"""

from __future__ import annotations

from qgis.gui import QgsDockWidget
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

DOCK_TITLE = "NASA POWER"
#: Used for the dock's object name and as the QgsMessageLog tag, so log lines
#: from every ring are filterable under one heading.
LOG_TAG = "NASA POWER"


class NasaPowerDock(QgsDockWidget):
    """Container for the fetch controls, QA findings and the time-series plot."""

    def __init__(self, iface, parent=None):
        super().__init__(DOCK_TITLE, parent or iface.mainWindow())
        self.iface = iface
        self.setObjectName("NasaPowerDock")

        # A scroll area, because the assembled panels are taller than a docked
        # panel on a laptop screen and QgsDockWidget will not scroll on its own.
        body = QWidget()
        self._layout = QVBoxLayout(body)
        self._layout.setContentsMargins(6, 6, 6, 6)
        self._layout.setSpacing(8)

        placeholder = QLabel(
            "Fetch controls arrive in M2.\n\n"
            "Point mode first: pick a location, choose parameters and a date "
            "range, and the result lands as a layer the Temporal Controller "
            "can animate."
        )
        placeholder.setWordWrap(True)
        placeholder.setTextFormat(Qt.TextFormat.PlainText)
        placeholder.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._layout.addWidget(placeholder)
        self._layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        self.setWidget(scroll)

    def teardown(self) -> None:
        """Release anything held outside this widget's own object tree.

        Called from ``plugin.unload()`` before the dock is removed. Qt disposes
        of child widgets on its own; what it will not do is unset a map tool
        still installed on the canvas, disconnect a canvas signal, or cancel a
        running QgsTask -- all of which outlive a reload and then call back into
        modules that no longer exist.
        """
        return
