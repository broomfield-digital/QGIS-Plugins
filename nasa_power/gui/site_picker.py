"""Pick a location by clicking the map.

The only interesting part is the coordinate transform. A canvas click arrives
in the **project's** CRS, and POWER speaks degrees only. In a Web Mercator
project a raw click is metres in the millions, which POWER rejects; in a
project whose units happen to look plausible it would quietly answer for the
wrong place, which is worse.
"""

from __future__ import annotations

from qgis.core import QgsPointXY, QgsProject
from qgis.gui import QgsMapToolEmitPoint, QgsVertexMarker
from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtGui import QColor

from nasa_power.qgis_bridge.layers_point import to_wgs84

MARKER_COLOR = QColor(242, 166, 59)
MARKER_SIZE = 12


class SitePicker(QgsMapToolEmitPoint):
    """Map tool that emits picked points as WGS84 degrees.

    Emits ``picked(longitude, latitude)``. Markers accumulate so a user can
    build up several sites before fetching; :meth:`clear_markers` removes them.
    """

    picked = pyqtSignal(float, float)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self._markers: list[QgsVertexMarker] = []

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        point = self.toMapCoordinates(event.pos())
        crs = self.canvas.mapSettings().destinationCrs()
        wgs84 = to_wgs84(QgsPointXY(point), crs)
        self.add_marker(wgs84)
        self.picked.emit(wgs84.x(), wgs84.y())

    def add_marker(self, point_wgs84: QgsPointXY) -> None:
        """Draw a marker at a WGS84 point, transforming back to the canvas CRS."""
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform

        crs = self.canvas.mapSettings().destinationCrs()
        target = point_wgs84
        if crs != QgsCoordinateReferenceSystem("EPSG:4326"):
            transform = QgsCoordinateTransform(
                QgsCoordinateReferenceSystem("EPSG:4326"), crs, QgsProject.instance()
            )
            target = transform.transform(point_wgs84)

        marker = QgsVertexMarker(self.canvas)
        marker.setCenter(target)
        marker.setColor(MARKER_COLOR)
        marker.setIconSize(MARKER_SIZE)
        marker.setIconType(QgsVertexMarker.IconType.ICON_CIRCLE)
        marker.setPenWidth(2)
        self._markers.append(marker)

    def clear_markers(self) -> None:
        """Remove every marker from the canvas.

        Must be called from the dock's teardown: a marker is a canvas child
        that outlives a plugin reload, and the reloaded plugin has no handle on
        it to remove it later.
        """
        for marker in self._markers:
            self.canvas.scene().removeItem(marker)
        self._markers.clear()

    def deactivate(self) -> None:
        super().deactivate()
