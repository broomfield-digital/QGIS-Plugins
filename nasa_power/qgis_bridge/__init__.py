"""Ring 2: turn ring-1 results into QGIS layers, tasks and metadata.

May import ``qgis.core`` and ``qgis.PyQt``; may **not** import ``qgis.gui``,
any widget, or ``iface``. That boundary is what lets these modules be tested
under a headless ``QgsApplication`` with no GUI and no network, and what stops
layer-building logic from quietly growing a dependency on a particular dock
layout.

``qgis.PyQt`` itself is allowed: a ``QgsDateTimeRange`` cannot be built without
``QDateTime`` and ``Qt.TimeSpec``.
"""
