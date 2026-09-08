"""Plugin entry point: toolbar action, dock widget, and Processing provider.

Two separate QGIS entry points reach a plugin that declares
``hasProcessingProvider=yes``:

* ``qgis.utils.startPlugin`` (QGIS Desktop) calls :meth:`initGui` only.
* ``qgis.utils.startProcessingPlugin`` (``qgis_process``, headless) calls
  :meth:`initProcessing` only.

Neither calls both, so the provider is registered from ``__init__`` -- which
runs on both paths -- behind an idempotence flag. This mirrors QGIS's own
``processing`` plugin (``ProcessingPlugin.__init__`` calls
``self.initProcessing()``); registering from ``initGui`` instead would leave
``qgis_process`` without the provider.

Everything registered here is torn down in :meth:`unload`. Reloading a plugin
deletes its modules from ``sys.modules`` and strips its path from
``sys.path``, so anything left registered outlives the module that created it:
duplicate toolbar icons, duplicate toolbox groups, and stale C++ pointers.
"""

from __future__ import annotations

import os

from qgis.core import QgsApplication
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtCore import Qt

PLUGIN_DIR = os.path.dirname(__file__)
ICON_PATH = os.path.join(PLUGIN_DIR, "icons", "nasa_power.svg")

#: Shown in the toolbar, under the Plugins menu, and as the dock title.
MENU_TITLE = "NASA POWER"


class NasaPowerPlugin:
    """The object QGIS holds for the lifetime of the enabled plugin."""

    def __init__(self, iface):
        self.iface = iface
        self._gui_initialized = False
        self._processing_initialized = False
        self._action: QAction | None = None
        self._dock = None
        self._provider = None
        self._options_factory = None

        # Runs on both the desktop and the qgis_process path.
        self.initProcessing()

    # ------------------------------------------------------------------ #
    # Processing
    # ------------------------------------------------------------------ #

    def initProcessing(self) -> None:  # noqa: N802 - name fixed by the QGIS API
        """Register the Processing provider. Safe to call more than once."""
        if self._processing_initialized:
            return

        from nasa_power.processing.provider import PowerProvider

        self._provider = PowerProvider()
        QgsApplication.processingRegistry().addProvider(self._provider)
        self._processing_initialized = True

    # ------------------------------------------------------------------ #
    # GUI
    # ------------------------------------------------------------------ #

    def initGui(self) -> None:  # noqa: N802 - name fixed by the QGIS API
        """Build the toolbar action and the dock. Safe to call more than once."""
        if self._gui_initialized:
            return

        from nasa_power.gui.dock import NasaPowerDock

        self._dock = NasaPowerDock(self.iface)
        self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._dock)
        self._dock.hide()

        self._action = QAction(QIcon(ICON_PATH), MENU_TITLE, self.iface.mainWindow())
        self._action.setCheckable(True)
        self._action.setToolTip("Fetch NASA POWER data into QGIS")
        # Keep the button and the dock in sync in both directions, so closing
        # the dock with its own X untoggles the toolbar button.
        self._action.toggled.connect(self._dock.setVisible)
        self._dock.visibilityChanged.connect(self._action.setChecked)

        self.iface.addToolBarIcon(self._action)
        # Plugins menu, not the Web menu. `category=Web` in metadata.txt is
        # right for how the plugin repository classifies this -- it is a web
        # service client -- but it is not where anyone looks for it. Nobody
        # opening QGIS to fetch some data thinks "Web".
        self.iface.addPluginToMenu(MENU_TITLE, self._action)

        from nasa_power.gui.options import PowerOptionsFactory

        self._options_factory = PowerOptionsFactory()
        self.iface.registerOptionsWidgetFactory(self._options_factory)

        self._gui_initialized = True

    # ------------------------------------------------------------------ #
    # Teardown
    # ------------------------------------------------------------------ #

    def unload(self) -> None:
        """Remove everything this plugin registered anywhere in QGIS."""
        # Registered process-wide by the fetcher, so it outlives this module
        # and would keep running on every request QGIS makes after a reload.
        from nasa_power.qgis_bridge.net import unregister_user_agent

        unregister_user_agent()

        if self._provider is not None:
            QgsApplication.processingRegistry().removeProvider(self._provider)
            self._provider = None
        self._processing_initialized = False

        if self._options_factory is not None:
            # Registered with QGIS, not owned by this widget tree: without this
            # a reload leaves a page bound to a deleted module, and the user
            # gets a second NASA POWER entry in Options.
            self.iface.unregisterOptionsWidgetFactory(self._options_factory)
            self._options_factory = None

        if self._action is not None:
            self.iface.removePluginMenu(MENU_TITLE, self._action)
            self.iface.removeToolBarIcon(self._action)
            self._action.deleteLater()
            self._action = None

        if self._dock is not None:
            self._dock.teardown()
            self.iface.removeDockWidget(self._dock)
            self._dock.deleteLater()
            self._dock = None

        self._gui_initialized = False
