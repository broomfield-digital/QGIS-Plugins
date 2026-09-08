"""The dock's form controls: WHERE, WHAT and OUTPUT.

Grouped in one module because they are one form. Each panel owns its widgets
and exposes the request it describes as plain data -- a dict of sites, a list
of parameters, dates -- so the dock never reaches into a widget and the fetch
path never sees Qt.

Layout order is the order the question gets asked: where, what, and what to do
with it. A scientist who wants "T2M, daily, February, at these four sites"
should not have to hunt.
"""

from __future__ import annotations

from datetime import date

from qgis.core import Qgis, QgsCoordinateReferenceSystem, QgsMapLayerProxyModel, QgsProject
from qgis.gui import (
    QgsCheckableComboBox,
    QgsCollapsibleGroupBox,
    QgsExtentGroupBox,
    QgsFileWidget,
    QgsMapLayerComboBox,
)
from qgis.PyQt.QtCore import QDate, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QDateEdit,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from nasa_power.core.api import COMMUNITIES, TEMPORAL_LEVELS
from nasa_power.core.dictionary import CURATED_PARAMETERS
from nasa_power.core.display import display_name
from nasa_power.qgis_bridge import settings
from nasa_power.qgis_bridge.layers_point import to_wgs84


class WherePanel(QWidget):
    """Choose sites (point mode) or an extent (gridded mode)."""

    changed = pyqtSignal()
    pick_requested = pyqtSignal(bool)

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        mode_row = QHBoxLayout()
        self.point_mode = QRadioButton("Sites")
        self.point_mode.setChecked(True)
        self.grid_mode = QRadioButton("Area")
        self._modes = QButtonGroup(self)
        self._modes.addButton(self.point_mode)
        self._modes.addButton(self.grid_mode)
        mode_row.addWidget(self.point_mode)
        mode_row.addWidget(self.grid_mode)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        # -- sites ------------------------------------------------------- #
        self.sites_box = QWidget()
        sites_layout = QVBoxLayout(self.sites_box)
        sites_layout.setContentsMargins(0, 0, 0, 0)

        self.pick_button = QPushButton("Click the map to add a site")
        self.pick_button.setCheckable(True)
        sites_layout.addWidget(self.pick_button)

        coord_row = QHBoxLayout()
        self.latitude = QDoubleSpinBox()
        self.latitude.setRange(-90.0, 90.0)
        self.latitude.setDecimals(4)
        self.latitude.setValue(40.02)
        self.longitude = QDoubleSpinBox()
        self.longitude.setRange(-180.0, 180.0)
        self.longitude.setDecimals(4)
        self.longitude.setValue(-105.27)
        self.add_button = QPushButton("Add")
        coord_row.addWidget(QLabel("Lat"))
        coord_row.addWidget(self.latitude)
        coord_row.addWidget(QLabel("Lon"))
        coord_row.addWidget(self.longitude)
        coord_row.addWidget(self.add_button)
        sites_layout.addLayout(coord_row)

        self.site_list = QListWidget()
        self.site_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.site_list.setMaximumHeight(90)
        sites_layout.addWidget(self.site_list)

        list_buttons = QHBoxLayout()
        self.remove_button = QPushButton("Remove")
        self.clear_button = QPushButton("Clear")
        list_buttons.addWidget(self.remove_button)
        list_buttons.addWidget(self.clear_button)
        list_buttons.addStretch(1)
        sites_layout.addLayout(list_buttons)

        # The feature that makes this better than clicking one point at a time.
        layer_row = QFormLayout()
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(QgsMapLayerProxyModel.Filter.PointLayer)
        self.layer_combo.setAllowEmptyLayer(True, "— none —")
        self.selected_only = QRadioButton("Selected features only")
        layer_row.addRow("Or every point in", self.layer_combo)
        layer_row.addRow("", self.selected_only)
        sites_layout.addLayout(layer_row)

        layout.addWidget(self.sites_box)

        # -- extent ------------------------------------------------------ #
        self.extent_box = QgsExtentGroupBox()
        self.extent_box.setTitle("Area")
        self.extent_box.setOutputCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
        if hasattr(iface, "mapCanvas"):
            self.extent_box.setCurrentExtent(
                iface.mapCanvas().extent(), iface.mapCanvas().mapSettings().destinationCrs()
            )
            self.extent_box.setOutputExtentFromCurrent()
        self.extent_box.setVisible(False)
        layout.addWidget(self.extent_box)

        self.request_badge = QLabel("")
        self.request_badge.setWordWrap(True)
        layout.addWidget(self.request_badge)

        # -- wiring ------------------------------------------------------ #
        self.point_mode.toggled.connect(self._mode_changed)
        self.pick_button.toggled.connect(self.pick_requested)
        self.add_button.clicked.connect(self._add_typed_site)
        self.remove_button.clicked.connect(self._remove_selected)
        self.clear_button.clicked.connect(self.clear_sites)
        self.site_list.model().rowsInserted.connect(self.changed)
        self.site_list.model().rowsRemoved.connect(self.changed)
        self.extent_box.extentChanged.connect(self.changed)
        self.layer_combo.layerChanged.connect(self.changed)

    # -- state ----------------------------------------------------------- #

    @property
    def mode(self) -> str:
        return "point" if self.point_mode.isChecked() else "regional"

    def _mode_changed(self, point_selected: bool) -> None:
        self.sites_box.setVisible(point_selected)
        self.extent_box.setVisible(not point_selected)
        if not point_selected and self.pick_button.isChecked():
            self.pick_button.setChecked(False)
        self.changed.emit()

    def add_site(self, longitude: float, latitude: float, name: str = "") -> None:
        label = name or f"site_{self.site_list.count() + 1}"
        item = QListWidgetItem(f"{label}   {latitude:.4f}, {longitude:.4f}")
        item.setData(Qt.ItemDataRole.UserRole, {"name": label, "latitude": latitude, "longitude": longitude})
        self.site_list.addItem(item)

    def _add_typed_site(self) -> None:
        self.add_site(self.longitude.value(), self.latitude.value())

    def _remove_selected(self) -> None:
        for item in self.site_list.selectedItems():
            self.site_list.takeItem(self.site_list.row(item))

    def clear_sites(self) -> None:
        self.site_list.clear()

    def sites(self) -> list[dict]:
        """Sites to fetch, as plain dicts in WGS84 degrees.

        A chosen point layer wins over the typed list, because picking a layer
        is the more deliberate act.
        """
        layer = self.layer_combo.currentLayer()
        if layer is not None:
            return self._sites_from_layer(layer)
        return [
            self.site_list.item(row).data(Qt.ItemDataRole.UserRole)
            for row in range(self.site_list.count())
        ]

    def _sites_from_layer(self, layer) -> list[dict]:
        """Every point (or every selected point) in ``layer``, in degrees."""
        features = (
            layer.selectedFeatures()
            if self.selected_only.isChecked() and layer.selectedFeatureCount()
            else layer.getFeatures()
        )
        # Reprojected per feature: the layer may be in any CRS, and POWER only
        # accepts degrees.
        crs = layer.crs()
        name_field = _label_field(layer)

        out: list[dict] = []
        for index, feature in enumerate(features, start=1):
            geometry = feature.geometry()
            if geometry.isEmpty():
                continue
            point = to_wgs84(geometry.asPoint(), crs)
            label = str(feature[name_field]) if name_field else f"feature_{index}"
            out.append({"name": label, "latitude": point.y(), "longitude": point.x()})
        return out

    def bbox(self) -> dict | None:
        """The extent in WGS84 degrees, or ``None`` in point mode."""
        if self.mode != "regional":
            return None
        extent = self.extent_box.outputExtent()
        if extent.isEmpty():
            return None
        return {
            "lat_min": extent.yMinimum(),
            "lat_max": extent.yMaximum(),
            "lon_min": extent.xMinimum(),
            "lon_max": extent.xMaximum(),
        }

    def set_badge(self, text: str) -> None:
        self.request_badge.setText(text)


def _label_field(layer) -> str | None:
    """A field worth using as a site name, if the layer has an obvious one."""
    preferred = ("name", "site", "station", "id", "label", "title")
    names = {f.name().lower(): f.name() for f in layer.fields()}
    for candidate in preferred:
        if candidate in names:
            return names[candidate]
    return None


class WhatPanel(QWidget):
    """Choose parameters, temporal level, dates, community and time standard."""

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QFormLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.parameters = QgsCheckableComboBox()
        self.parameters.setDefaultText("Choose parameters…")
        self._populate_parameters(CURATED_PARAMETERS)
        layout.addRow("Parameters", self.parameters)

        self.temporal = QComboBox()
        self.temporal.addItems(list(TEMPORAL_LEVELS))
        self.temporal.setCurrentText("daily")
        layout.addRow("Resolution", self.temporal)

        self.start = QDateEdit(QDate(2024, 2, 1))
        self.start.setCalendarPopup(True)
        self.start.setDisplayFormat("yyyy-MM-dd")
        self.end = QDateEdit(QDate(2024, 2, 29))
        self.end.setCalendarPopup(True)
        self.end.setDisplayFormat("yyyy-MM-dd")
        layout.addRow("From", self.start)
        layout.addRow("To", self.end)

        self.community = QComboBox()
        self.community.addItems(list(COMMUNITIES))
        self.community.setToolTip(
            "POWER community. This changes the native units of radiation "
            "parameters: RE gives kW-hr/m^2/day, AG gives MJ/m^2/day, SB gives W m-2."
        )
        layout.addRow("Community", self.community)

        standard_row = QHBoxLayout()
        self.utc = QRadioButton("UTC")
        self.utc.setChecked(True)
        self.lst = QRadioButton("Local solar")
        self.lst.setToolTip(
            "POWER's own default is Local Solar Time — about a seven-hour offset "
            "from UTC at mid-latitudes. Choose it only if you want local solar "
            "time; anything compared against a UTC model needs UTC."
        )
        standard_row.addWidget(self.utc)
        standard_row.addWidget(self.lst)
        standard_row.addStretch(1)
        layout.addRow("Timestamps", standard_row)

        self.parameters.checkedItemsChanged.connect(lambda _: self.changed.emit())
        self.temporal.currentTextChanged.connect(lambda _: self.changed.emit())
        self.start.dateChanged.connect(lambda _: self.changed.emit())
        self.end.dateChanged.connect(lambda _: self.changed.emit())
        self.community.currentTextChanged.connect(lambda _: self.changed.emit())
        self.utc.toggled.connect(lambda _: self.changed.emit())

    def _populate_parameters(self, codes) -> None:
        self.parameters.clear()
        for code in codes:
            # The code is the value; the readable name is what gets shown,
            # because "ALLSKY_SFC_SW_DWN" is not a thing anyone recognises at a
            # glance.
            self.parameters.addItemWithCheckState(
                f"{display_name(code)}  ({code})", Qt.CheckState.Unchecked, code
            )

    def set_parameter_choices(self, infos) -> None:
        """Repopulate from a fetched dictionary, preserving what was checked."""
        checked = set(self.selected_parameters())
        self.parameters.clear()
        for info in infos:
            state = (
                Qt.CheckState.Checked if info.name in checked else Qt.CheckState.Unchecked
            )
            label = f"{display_name(info.name, info.long_name)}  ({info.name})"
            self.parameters.addItemWithCheckState(label, state, info.name)
            if info.definition:
                index = self.parameters.count() - 1
                self.parameters.setItemData(index, info.definition, Qt.ItemDataRole.ToolTipRole)

    def selected_parameters(self) -> list[str]:
        return [str(v) for v in self.parameters.checkedItemsData()]

    @property
    def temporal_level(self) -> str:
        return self.temporal.currentText()

    @property
    def time_standard(self) -> str:
        return "UTC" if self.utc.isChecked() else "LST"

    def dates(self) -> tuple[date, date]:
        return self.start.date().toPyDate(), self.end.date().toPyDate()

    def set_grid_mode(self, gridded: bool) -> None:
        """Reflect the constraints gridded mode imposes.

        Two of them, both measured: there is no ``hourly/regional`` endpoint at
        all, and a regional request takes exactly one parameter.
        """
        hourly_index = self.temporal.findText("hourly")
        if hourly_index >= 0:
            item = self.temporal.model().item(hourly_index)
            item.setEnabled(not gridded)
            item.setToolTip(
                "POWER has no hourly gridded endpoint." if gridded else ""
            )
        if gridded and self.temporal.currentText() == "hourly":
            self.temporal.setCurrentText("daily")


class OutputPanel(QWidget):
    """What to do with the result once it arrives."""

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        from qgis.PyQt.QtWidgets import QCheckBox

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.add_to_map = QCheckBox("Add the result to the map")
        self.add_to_map.setChecked(settings.get_bool(settings.KEY_ADD_TO_MAP, True))
        self.auto_style = QCheckBox("Style it automatically")
        self.auto_style.setChecked(settings.get_bool(settings.KEY_AUTO_STYLE, True))
        self.convert_si = QCheckBox("Convert to SI units")
        self.convert_si.setChecked(settings.get_bool(settings.KEY_CONVERT_TO_SI, False))
        self.convert_si.setToolTip(
            "Off by default so layer values match what the POWER website shows. "
            "On, radiation becomes W m-2, temperature K and pressure Pa — and the "
            "conversion, with its factor, is written into the layer's history."
        )
        self.force_refetch = QCheckBox("Ignore the cache and re-download")
        self.force_refetch.setToolTip(
            "Normally a repeated request is served from disk without touching the "
            "network. POWER asks not to be re-queried for the same location."
        )

        for box in (self.add_to_map, self.auto_style, self.convert_si, self.force_refetch):
            layout.addWidget(box)

        cache_row = QFormLayout()
        self.cache_dir = QgsFileWidget()
        self.cache_dir.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        cache_row.addRow("Cache", self.cache_dir)
        layout.addLayout(cache_row)

        for box in (self.add_to_map, self.auto_style, self.convert_si, self.force_refetch):
            box.toggled.connect(lambda _: self.changed.emit())

    def persist(self) -> None:
        """Remember the checkbox states for next session."""
        settings.set_bool(settings.KEY_ADD_TO_MAP, self.add_to_map.isChecked())
        settings.set_bool(settings.KEY_AUTO_STYLE, self.auto_style.isChecked())
        settings.set_bool(settings.KEY_CONVERT_TO_SI, self.convert_si.isChecked())
