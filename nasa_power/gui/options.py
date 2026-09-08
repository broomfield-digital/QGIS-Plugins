"""A NASA POWER page under Settings → Options.

Holds the preferences that outlive one fetch: where the cache lives, how big it
has grown, how many requests may run at once, and the defaults the dock opens
with. The dock's own checkboxes stay where they are -- they are per-fetch
choices, not settings.

The factory is registered with ``iface.registerOptionsWidgetFactory`` and
**must** be unregistered on unload. A registration outlives the module that
made it, so a reload without teardown leaves a page bound to code that no
longer exists -- and the user gets three NASA POWER pages.
"""

from __future__ import annotations

import os

from qgis.gui import QgsFileWidget, QgsOptionsPageWidget, QgsOptionsWidgetFactory
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from nasa_power.core.api import COMMUNITIES, TEMPORAL_LEVELS
from nasa_power.core.citation import ACKNOWLEDGEMENT, HOMEPAGE
from nasa_power.qgis_bridge import paths, settings

PAGE_TITLE = "NASA POWER"
PAGE_KEY = "nasapower"

_ICON = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "icons", "nasa_power.svg"
)


def _human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} GB"


class PowerOptionsPage(QgsOptionsPageWidget):
    """The page itself. ``apply()`` is called when the user clicks OK."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)

        form = QFormLayout()

        self.cache_dir = QgsFileWidget()
        self.cache_dir.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        self.cache_dir.setFilePath(str(paths.resolve_cache_dir(create=False)))
        self.cache_dir.setToolTip(
            "Where POWER responses are kept. Entries are named by a hash of "
            "their request URL, so this directory can be shared with another "
            "tool using the same scheme."
        )
        form.addRow("Cache directory", self.cache_dir)

        cache_row = QHBoxLayout()
        self.cache_size = QLabel(_human(paths.cache_size_bytes()))
        self.clear_cache = QPushButton("Clear cache")
        cache_row.addWidget(self.cache_size)
        cache_row.addWidget(self.clear_cache)
        cache_row.addStretch(1)
        form.addRow("Cached data", cache_row)

        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 8)
        self.concurrency.setValue(
            settings.get_int(settings.KEY_MAX_CONCURRENCY, settings.DEFAULT_MAX_CONCURRENCY)
        )
        self.concurrency.setToolTip(
            "How many requests run at once. POWER publishes no rate limit but "
            "asks not to be hammered; four is enough to hide latency on a tiled "
            "fetch without looking like a scraper."
        )
        form.addRow("Concurrent requests", self.concurrency)

        self.community = QComboBox()
        self.community.addItems(list(COMMUNITIES))
        self.community.setCurrentText(settings.get_str(settings.KEY_COMMUNITY, "RE"))
        self.community.setToolTip(
            "Default community. This changes the native units of radiation "
            "parameters: RE gives kW-hr/m^2/day, AG gives MJ/m^2/day, SB gives "
            "W m-2."
        )
        form.addRow("Default community", self.community)

        self.temporal = QComboBox()
        self.temporal.addItems(list(TEMPORAL_LEVELS))
        self.temporal.setCurrentText(settings.get_str(settings.KEY_TEMPORAL, "daily"))
        form.addRow("Default resolution", self.temporal)

        self.convert_si = QCheckBox(
            "Convert values to SI units by default (K, W m-2, Pa)"
        )
        self.convert_si.setChecked(settings.get_bool(settings.KEY_CONVERT_TO_SI, False))
        self.convert_si.setToolTip(
            "Off by default so layer values match what the POWER website shows. "
            "Whichever way this is set, the conversion applied — including its "
            "factor — is written into every layer's history."
        )
        form.addRow("", self.convert_si)

        layout.addLayout(form)

        note = QLabel(
            f"{ACKNOWLEDGEMENT}\n\n"
            f"POWER is not ground truth: its meteorology is reanalysis "
            f"(MERRA-2/GEOS) and its solar half is satellite-derived analysis "
            f"(CERES SYN1deg, and NASA/GEWEX SRB before 2001-01-01). "
            f"{HOMEPAGE}"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid);")
        layout.addWidget(note)
        layout.addStretch(1)

        self.clear_cache.clicked.connect(self._clear_cache)

    def _clear_cache(self) -> None:
        """Delete every cached response, after asking.

        Destructive and irreversible in the sense that matters -- refilling it
        means re-querying POWER for data it has already served, which its own
        documentation asks clients not to do. So it asks first and says how
        much is at stake.
        """
        directory = paths.resolve_cache_dir(create=False)
        if not directory.exists():
            self.cache_size.setText(_human(0))
            return

        # rasters/ is not a cache of downloads: it holds the mosaicked GeoTIFFs
        # that gridded map layers are reading from. Deleting those does not
        # cost a re-download, it invalidates layers already on the map -- so
        # they are counted and offered separately rather than swept up by a
        # button whose promise is "it will just be fetched again".
        rasters = directory / "rasters"
        downloads = paths.cache_size_bytes(directory) - paths.cache_size_bytes(rasters)
        derived = paths.cache_size_bytes(rasters)

        message = (
            f"Delete {_human(downloads)} of cached POWER responses from\n"
            f"{directory}?\n\nThey will be downloaded again the next time they "
            f"are asked for."
        )
        if derived:
            message += (
                f"\n\nThis will NOT touch the {_human(derived)} of mosaicked "
                f"rasters in rasters/, because map layers may be reading them. "
                f"Delete that folder by hand if you want it gone."
            )
        answer = QMessageBox.question(
            self,
            "Clear the NASA POWER cache?",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        import shutil

        for child in directory.iterdir():
            if child.name == "rasters":
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        self.cache_size.setText(_human(paths.cache_size_bytes()))

    def apply(self) -> None:
        """Persist the page. Called when the user accepts the dialog."""
        chosen = self.cache_dir.filePath().strip()
        if chosen:
            paths.set_cache_dir(chosen)
        settings.set_int(settings.KEY_MAX_CONCURRENCY, self.concurrency.value())
        settings.set_str(settings.KEY_COMMUNITY, self.community.currentText())
        settings.set_str(settings.KEY_TEMPORAL, self.temporal.currentText())
        settings.set_bool(settings.KEY_CONVERT_TO_SI, self.convert_si.isChecked())


class PowerOptionsFactory(QgsOptionsWidgetFactory):
    """Puts :class:`PowerOptionsPage` in the QGIS options dialog."""

    def __init__(self):
        super().__init__(PAGE_TITLE, QIcon(_ICON), PAGE_KEY)

    def path(self) -> list[str]:
        return []

    def createWidget(self, parent=None) -> PowerOptionsPage:  # noqa: N802 - QGIS API
        return PowerOptionsPage(parent)
