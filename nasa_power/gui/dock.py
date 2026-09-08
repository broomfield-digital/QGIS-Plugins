"""The NASA POWER dock: the whole fetch, from a click to a styled layer.

The flow, and which thread each step is on:

1. **Main thread.** Read the form, run ``core.qa.preflight``. A BLOCKING
   finding stops here -- no task is created and nothing is sent. These are
   requests POWER would refuse anyway, and one of them (``hourly/regional``)
   fails as a 19 KB HTML page with no clue what went wrong.
2. **Main thread.** Plan the requests with ``core.plan_requests``, which splits
   by site, by parent dataset and by the API's parameter caps.
3. **Worker threads.** ``PowerFetchTask`` fans the requests across chunk tasks.
   Nothing here touches a layer, a widget or ``QgsProject``.
4. **Main thread again**, in ``finished()``: parse, build layers, style them,
   attach metadata, add to the project.

Everything this dock registers with QGIS -- the map tool, the running task --
is released in :meth:`teardown`, because a plugin reload deletes this module
while the canvas keeps whatever it was handed.
"""

from __future__ import annotations

import json
from pathlib import Path

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsMessageLog,
    QgsProject,
)
from qgis.gui import QgsDockWidget
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from nasa_power.core.api import PowerRequest, count_requests, plan_requests
from nasa_power.core.citation import build_citation
from nasa_power.core.decode import Observation, ResponseFacts, parse_point_response
from nasa_power.core.display import layer_name
from nasa_power.core.provenance import family_of, grid_for
from nasa_power.core.qa import Level, QaReport, preflight, postfetch
from nasa_power.gui.panels import OutputPanel, WherePanel, WhatPanel
from nasa_power.gui.qa_panel import QaPanel
from nasa_power.gui.site_picker import SitePicker
from nasa_power.qgis_bridge import paths
from nasa_power.qgis_bridge.layer_metadata import apply_metadata
from nasa_power.qgis_bridge.layers_point import build_point_layer
from nasa_power.qgis_bridge.styling import style_point_layer
from nasa_power.qgis_bridge.tasks import FetchJob, PowerFetchTask

DOCK_TITLE = "NASA POWER"
LOG_TAG = "NASA POWER"


def _section(title: str) -> QLabel:
    label = QLabel(title.upper())
    label.setStyleSheet("font-weight: bold; margin-top: 6px;")
    return label


class NasaPowerDock(QgsDockWidget):
    """Fetch controls, QA findings, and (from M4) the time-series chart."""

    def __init__(self, iface, parent=None):
        super().__init__(DOCK_TITLE, parent or iface.mainWindow())
        self.iface = iface
        self.setObjectName("NasaPowerDock")

        self._task: PowerFetchTask | None = None
        self._picker: SitePicker | None = None
        self._previous_tool = None

        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        layout.addWidget(_section("Where"))
        self.where = WherePanel(iface)
        layout.addWidget(self.where)

        layout.addWidget(_section("What"))
        self.what = WhatPanel()
        layout.addWidget(self.what)

        layout.addWidget(_section("Output"))
        self.output = OutputPanel()
        layout.addWidget(self.output)

        self.fetch_button = QPushButton("Fetch")
        self.fetch_button.setDefault(True)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        layout.addWidget(self.fetch_button)
        layout.addWidget(self.cancel_button)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        layout.addWidget(_section("Notes"))
        self.qa = QaPanel()
        layout.addWidget(self.qa)

        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        self.setWidget(scroll)

        self.where.changed.connect(self._refresh)
        self.what.changed.connect(self._refresh)
        self.where.pick_requested.connect(self._set_picking)
        self.fetch_button.clicked.connect(self.fetch)
        self.cancel_button.clicked.connect(self.cancel)
        self._refresh()

    # ------------------------------------------------------------------ #
    # Form state
    # ------------------------------------------------------------------ #

    def _refresh(self) -> None:
        """Re-run pre-flight and update the badge and the Fetch button.

        Running the *same* pre-flight the fetch will run means the button can
        never be enabled for a request that would then be refused.
        """
        gridded = self.where.mode == "regional"
        self.what.set_grid_mode(gridded)

        report = self._preflight()
        blocked = report.is_blocked
        self.fetch_button.setEnabled(not blocked and self._task is None)

        parameters = self.what.selected_parameters()
        if not parameters:
            self.where.set_badge("Choose at least one parameter.")
        elif blocked:
            self.where.set_badge(report.blocking[0].message)
        else:
            try:
                n = count_requests(
                    self.what.temporal_level,
                    self.where.mode,
                    parameters,
                    sites=self.where.sites(),
                    bbox=self.where.bbox(),
                )
            except Exception:
                n = 0
            plural = "" if n == 1 else "s"
            self.where.set_badge(f"{n} request{plural}.")

        self.qa.show_report(report)

    def _preflight(self) -> QaReport:
        start, end = self.what.dates()
        return preflight(
            temporal=self.what.temporal_level,
            mode=self.where.mode,
            parameters=self.what.selected_parameters(),
            start=start,
            end=end,
            sites=self.where.sites(),
            bbox=self.where.bbox(),
            time_standard=self.what.time_standard,
            community=self.what.community.currentText(),
        )

    # ------------------------------------------------------------------ #
    # Map picking
    # ------------------------------------------------------------------ #

    def _set_picking(self, active: bool) -> None:
        canvas = self.iface.mapCanvas()
        if active:
            if self._picker is None:
                self._picker = SitePicker(canvas)
                self._picker.picked.connect(self._site_picked)
            self._previous_tool = canvas.mapTool()
            canvas.setMapTool(self._picker)
        elif self._picker is not None:
            canvas.unsetMapTool(self._picker)
            if self._previous_tool is not None:
                canvas.setMapTool(self._previous_tool)

    def _site_picked(self, longitude: float, latitude: float) -> None:
        self.where.add_site(longitude, latitude)

    # ------------------------------------------------------------------ #
    # Fetch
    # ------------------------------------------------------------------ #

    def fetch(self) -> None:
        report = self._preflight()
        if report.is_blocked:
            self._message(report.blocking[0].message, Qgis.MessageLevel.Critical, report)
            return
        if self.where.mode == "regional":
            self._message(
                "Gridded fetching arrives in the next milestone; sites work now.",
                Qgis.MessageLevel.Info,
            )
            return

        start, end = self.what.dates()
        try:
            requests = plan_requests(
                self.what.temporal_level,
                "point",
                self.what.selected_parameters(),
                start=start,
                end=end,
                sites=self.where.sites(),
                community=self.what.community.currentText(),
                fmt="JSON",
                time_standard=self.what.time_standard,
            )
        except Exception as exc:
            self._message(str(exc), Qgis.MessageLevel.Critical)
            return

        self.output.persist()
        job = FetchJob(
            requests=requests,
            cache_dir=paths.resolve_cache_dir(),
            force=self.output.force_refetch.isChecked(),
        )

        task = PowerFetchTask(f"NASA POWER: {len(requests)} request(s)", job)
        task.on_complete = self._fetch_complete
        # Subtasks must exist before the manager takes the parent.
        task.build_subtasks()
        task.progressChanged.connect(self.progress.setValue)

        # Held for the task's life: C++ owns the task, but a garbage-collected
        # Python wrapper loses on_complete and the bound slots with it.
        self._task = task
        self._set_running(True)
        QgsApplication.taskManager().addTask(task)

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()

    def _set_running(self, running: bool) -> None:
        self.fetch_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        self.progress.setVisible(running)
        self.progress.setValue(0)

    def _fetch_complete(self, job: FetchJob, cancelled: bool) -> None:
        """Main thread. Parse, build, style, add."""
        self._task = None
        self._set_running(False)

        if cancelled:
            self._message("Fetch cancelled. Nothing was added.", Qgis.MessageLevel.Info)
            return

        report = QaReport()
        by_parameter: dict[str, list[Observation]] = {}
        facts_by_parameter: dict[str, ResponseFacts] = {}
        urls: list[str] = []
        citation = ""

        for outcome in job.outcomes:
            urls.append(outcome.request.url)
            if not outcome.ok:
                report.add(
                    _failure_finding(outcome.request, outcome.error)
                )
                continue
            try:
                payload = json.loads(Path(outcome.path).read_bytes())
                observations, facts = parse_point_response(
                    payload,
                    temporal=outcome.request.temporal,
                    requested=outcome.request.params,
                    site=outcome.request.site or "site",
                    url=outcome.request.url,
                )
            except Exception as exc:
                report.add(_failure_finding(outcome.request, str(exc)))
                continue

            raw_keys = sorted(
                {
                    key
                    for series in payload.get("properties", {}).get("parameter", {}).values()
                    for key in series
                }
            )
            report.extend(
                postfetch(
                    outcome.request,
                    facts,
                    observations,
                    raw_time_keys=raw_keys,
                    was_cached=outcome.was_cached,
                )
            )
            citation = citation or build_citation(
                facts.api_name, facts.api_version, sources=facts.sources
            )
            for observation in observations:
                by_parameter.setdefault(observation.parameter, []).append(observation)
                facts_by_parameter.setdefault(observation.parameter, facts)

        self.qa.show_report(report, citation=citation, urls=urls)

        if not by_parameter:
            self._message(
                "Nothing came back. See the notes below for what POWER said.",
                Qgis.MessageLevel.Warning,
                report,
            )
            return

        added = self._add_layers(by_parameter, facts_by_parameter, report, urls)
        level = Qgis.MessageLevel.Warning if report.has_error else Qgis.MessageLevel.Success
        cached = job.cached_count
        note = f" ({cached} from cache)" if cached else ""
        self._message(
            f"Added {added} layer(s) from {len(job.outcomes)} request(s){note}.",
            level,
            report,
        )

    def _add_layers(
        self,
        by_parameter: dict[str, list[Observation]],
        facts_by_parameter: dict[str, ResponseFacts],
        report: QaReport,
        urls: list[str],
    ) -> int:
        """One layer per parameter, so each carries a single set of units."""
        if not self.output.add_to_map.isChecked():
            return 0

        added = 0
        for parameter, observations in by_parameter.items():
            facts = facts_by_parameter[parameter]
            units = observations[0].units
            grid = grid_for(family_of(parameter))
            name = layer_name(
                parameter,
                units,
                observations[0].temporal,
                facts.time_standard,
                long_name=facts.long_names.get(parameter, ""),
                sources=facts.sources,
                grid_label=grid.label if grid else "",
                warn=report.has_error,
            )
            layer = build_point_layer(
                observations,
                name,
                time_standard=facts.time_standard,
                sources=facts.sources,
            )
            if self.output.auto_style.isChecked():
                style_point_layer(layer, parameter, observations[0].native_units)
            apply_metadata(
                layer,
                facts,
                report,
                parameters=[parameter],
                temporal=observations[0].temporal,
                urls=urls,
                grid_label=grid.label if grid else "",
            )
            QgsProject.instance().addMapLayer(layer)
            added += 1
        return added

    # ------------------------------------------------------------------ #

    def _message(
        self, text: str, level: Qgis.MessageLevel, report: QaReport | None = None
    ) -> None:
        """Message bar for the headline, log for the detail.

        The QGIS convention: the bar is for what just happened, the log is for
        why. Findings go to the log in full so a user who scrolled past the bar
        can still find out what the plugin objected to.
        """
        self.iface.messageBar().pushMessage("NASA POWER", text, level=level, duration=8)
        if report is not None:
            for finding in report.at_least(Level.WARNING):
                QgsMessageLog.logMessage(
                    f"{finding}\n{finding.detail}\n{finding.url}".strip(),
                    LOG_TAG,
                    Qgis.MessageLevel.Warning,
                )

    def teardown(self) -> None:
        """Release everything held outside this widget's own object tree.

        Qt disposes of child widgets. What it will not do is unset a map tool
        installed on the canvas, remove canvas markers, or cancel a running
        task -- all of which outlive a plugin reload and then call back into
        modules that no longer exist.
        """
        if self._task is not None:
            self._task.on_complete = None
            self._task.cancel()
            self._task = None
        if self._picker is not None:
            self._picker.clear_markers()
            self.iface.mapCanvas().unsetMapTool(self._picker)
            self._picker = None


def _failure_finding(request: PowerRequest, error: str):
    from nasa_power.core.qa import QaFinding

    return QaFinding(
        Level.ERROR,
        "REQUEST_FAILED",
        f"A request for {', '.join(request.params)} failed.",
        detail=error,
        affected=request.params,
        url=request.url,
    )
