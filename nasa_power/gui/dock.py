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

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Sequence

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
from nasa_power.core.qa import (
    Level,
    QaReport,
    check_valid_range,
    postfetch,
    preflight,
)
from nasa_power.gui import param_model
from nasa_power.gui.panels import OutputPanel, WherePanel, WhatPanel
from nasa_power.gui.plot_widget import PowerPlotWidget
from nasa_power.gui.qa_panel import QaPanel
from nasa_power.gui.site_picker import SitePicker
from nasa_power.qgis_bridge import paths, settings
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
        self._dictionary = None
        self._dictionary_task = None
        self._preflight_report = None

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

        layout.addWidget(_section("Series"))
        self.plot = PowerPlotWidget()
        layout.addWidget(self.plot)

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
        self.what.show_all.toggled.connect(self._repopulate_parameters)
        self.what.load_button.clicked.connect(self.load_parameter_list)
        self.what.community.currentTextChanged.connect(self._dictionary_context_changed)
        self.what.temporal.currentTextChanged.connect(self._dictionary_context_changed)
        self._connect_temporal_controller()
        self._dictionary_context_changed()
        self._refresh()

    # ------------------------------------------------------------------ #
    # Parameter list
    # ------------------------------------------------------------------ #

    def _dictionary_context_changed(self, *_args) -> None:
        """Load a cached dictionary for the current community and resolution.

        Cache only. Changing a dropdown must not start a download, and a
        missing dictionary is not an error -- the curated list covers it.
        """
        self._dictionary = param_model.cached_dictionary(
            self.what.community.currentText(),
            self.what.temporal_level,
            paths.resolve_cache_dir(),
        )
        self._repopulate_parameters()

    def _repopulate_parameters(self, *_args) -> None:
        self.what.set_parameter_choices(
            param_model.choices(self._dictionary, show_all=self.what.show_all.isChecked())
        )
        self.what.load_button.setEnabled(self._dictionary is None)

    def load_parameter_list(self) -> None:
        """Fetch POWER's parameter list for the current community/resolution."""
        if self._dictionary_task is not None:
            return
        task = param_model.DictionaryTask(
            self.what.community.currentText(),
            self.what.temporal_level,
            paths.resolve_cache_dir(),
        )
        task.on_complete = self._dictionary_loaded
        self.what.load_button.setEnabled(False)
        # Held for the task's life, like the fetch task: a garbage-collected
        # wrapper loses on_complete.
        self._dictionary_task = task
        QgsApplication.taskManager().addTask(task)

    def _dictionary_loaded(self, parameters, error: str) -> None:
        self._dictionary_task = None
        if parameters is None:
            self.what.load_button.setEnabled(True)
            self._message(
                f"Could not load the parameter list. {error}", Qgis.MessageLevel.Warning
            )
            return
        self._dictionary = parameters
        self._repopulate_parameters()
        self._message(
            f"Loaded {len(parameters)} parameters for "
            f"{self.what.community.currentText()}/{self.what.temporal_level}.",
            Qgis.MessageLevel.Success,
        )

    def _connect_temporal_controller(self) -> None:
        """Track the Temporal Controller so the chart cursor follows the map.

        Optional wiring: ``iface`` may have no controller in a headless or
        stripped context, and a chart that cannot follow the slider is much
        better than a dock that fails to build.
        """
        self._temporal_connection = None
        controller = getattr(self.iface, "mapCanvas", None)
        try:
            canvas = self.iface.mapCanvas()
            controller = canvas.temporalController()
        except Exception:
            return
        if controller is None:
            return
        try:
            controller.updateTemporalRange.connect(self._temporal_range_changed)
            self._temporal_connection = controller
        except Exception:  # pragma: no cover - signal shape varies by build
            self._temporal_connection = None

    def _temporal_range_changed(self, temporal_range) -> None:
        """Move the chart cursor to the frame the map is showing."""
        try:
            begin = temporal_range.begin()
            moment = begin.toPyDateTime() if begin.isValid() else None
        except Exception:  # pragma: no cover - defensive
            moment = None
        self.plot.set_cursor(moment)

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

        gridded = self.where.mode == "regional"
        start, end = self.what.dates()
        try:
            requests = plan_requests(
                self.what.temporal_level,
                self.where.mode,
                self.what.selected_parameters(),
                start=start,
                end=end,
                sites=None if gridded else self.where.sites(),
                bbox=self.where.bbox() if gridded else None,
                community=self.what.community.currentText(),
                # Point mode reads JSON, which is already GeoJSON and carries
                # units, fill value and provenance in band. Gridded mode reads
                # NetCDF, which GDAL opens as a georeferenced multi-band raster
                # -- a point NetCDF is a 1x1 grid GDAL cannot open at all.
                fmt="NETCDF" if gridded else "JSON",
                time_standard=self.what.time_standard,
            )
        except Exception as exc:
            self._message(str(exc), Qgis.MessageLevel.Critical)
            return

        self.output.persist()
        # Kept, not discarded: LST_REQUESTED, MIXED_PROVENANCE and RECORD_START
        # are decided before the fetch, and they are exactly the findings that
        # should reach the layer's metadata and its warning prefix. Overwriting
        # the panel with only the post-fetch report lost them.
        self._preflight_report = report
        job = FetchJob(
            requests=requests,
            cache_dir=paths.resolve_cache_dir(),
            force=self.output.force_refetch.isChecked(),
            concurrency=settings.get_int(
                settings.KEY_MAX_CONCURRENCY, settings.DEFAULT_MAX_CONCURRENCY
            ),
        )

        task = PowerFetchTask(f"NASA POWER: {len(requests)} request(s)", job)
        task.on_complete = self._fetch_complete
        # Subtasks must exist before the manager takes the parent.
        task.build_subtasks()
        # progressChanged carries a double and QProgressBar.setValue takes an
        # int: connected directly, PyQt6 raises TypeError on every tick, and
        # QGIS's excepthook turns that into a dialog per progress update.
        task.progressChanged.connect(lambda value: self.progress.setValue(int(value)))

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

        if job.requests and job.requests[0].mode == "regional":
            self._gridded_complete(job)
            return

        report = QaReport()
        report.extend(getattr(self, "_preflight_report", None) or QaReport())
        by_parameter: dict[str, list[Observation]] = {}
        facts_by_parameter: dict[str, ResponseFacts] = {}
        urls: list[str] = []
        all_sources: set[str] = set()
        api_name = api_version = ""

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
                    convert=self.output.convert_si.isChecked(),
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
            # Every response's sources, not just the first: a family-split
            # fetch has two parents and citing one contradicts the layer names
            # this same fetch produces.
            all_sources.update(facts.sources)
            api_name = api_name or facts.api_name
            api_version = api_version or facts.api_version
            for observation in observations:
                by_parameter.setdefault(observation.parameter, []).append(observation)
                facts_by_parameter.setdefault(observation.parameter, facts)

        citation = (
            build_citation(api_name, api_version, sources=tuple(sorted(all_sources)))
            if all_sources or api_name
            else ""
        )
        self.qa.show_report(report, citation=citation, urls=urls)

        if not by_parameter:
            self._message(
                "Nothing came back. See the notes below for what POWER said.",
                Qgis.MessageLevel.Warning,
                report,
            )
            return

        self._plot_observations(by_parameter)
        added = self._add_layers(by_parameter, facts_by_parameter, report, urls)
        level = Qgis.MessageLevel.Warning if report.has_error else Qgis.MessageLevel.Success
        cached = job.cached_count
        note = f" ({cached} from cache)" if cached else ""
        self._message(
            f"Added {added} layer(s) from {len(job.outcomes)} request(s){note}.",
            level,
            report,
        )

    #: Beyond this many lines a chart is a smear rather than a reading.
    MAX_PLOT_SERIES = 6

    def _plot_observations(
        self, by_parameter: dict[str, list[Observation]]
    ) -> None:
        """Chart one line per (parameter, site), capped so it stays readable.

        Values are the converted ones, and a masked fill is simply absent
        rather than plotted as zero -- POWER's fill is a real gap, and a zero
        in an irradiance series reads as darkness at noon.
        """
        # One unit per chart. A shared y-axis labelled with whichever
        # parameter came back first drew irradiance against a Celsius scale,
        # which is not a chart, it is a coincidence. Where a fetch spans units,
        # the largest group is plotted and the QA panel says which.
        by_units: dict[str, list[str]] = {}
        for parameter, observations in by_parameter.items():
            for observation in observations:
                by_units.setdefault(observation.units, []).append(parameter)
                break
        chosen_units = (
            max(by_units, key=lambda u: len(by_units[u])) if by_units else ""
        )
        plotted = set(by_units.get(chosen_units, []))

        series: list[tuple[str, list[tuple[datetime, float]]]] = []
        units = chosen_units
        for parameter, observations in by_parameter.items():
            if parameter not in plotted:
                continue
            by_site: dict[str, list[tuple[datetime, float]]] = {}
            for observation in observations:
                if observation.value is None:
                    continue
                by_site.setdefault(observation.site, []).append(
                    (observation.t_start, observation.value)
                )
            for site, points in by_site.items():
                points.sort(key=lambda pair: pair[0])
                label = f"{parameter} · {site}" if len(by_site) > 1 else parameter
                series.append((label, points))

        if len(series) > self.MAX_PLOT_SERIES:
            series = series[: self.MAX_PLOT_SERIES]
        self.plot.set_series(series, units=units)

    def _gridded_complete(self, job: FetchJob) -> None:
        """Main thread. Mosaic each parameter's tiles into one raster layer.

        One layer per parameter, always: the parents are not co-registered
        (CERES 1 degree against MERRA-2 0.5 x 0.625), so a shared raster would
        be misaligned. ``mosaic`` refuses a cross-family set outright.
        """
        from nasa_power.core.qa import QaFinding
        from nasa_power.gdalio.mosaic import MosaicError, mosaic
        from nasa_power.qgis_bridge.layers_raster import (
            build_raster_layer,
            style_raster_layer,
        )

        report = QaReport()
        report.extend(getattr(self, "_preflight_report", None) or QaReport())
        urls = [o.request.url for o in job.outcomes]
        by_parameter: dict[str, list] = {}
        for outcome in job.outcomes:
            if not outcome.ok:
                report.add(_failure_finding(outcome.request, outcome.error))
                continue
            by_parameter.setdefault(outcome.request.params[0], []).append(outcome)

        if not by_parameter:
            self.qa.show_report(report, urls=urls)
            self._message(
                "Nothing came back. See the notes below.", Qgis.MessageLevel.Warning, report
            )
            return

        added = 0
        for parameter, outcomes in by_parameter.items():
            first = outcomes[0].request
            target = Path(job.cache_dir) / "rasters" / _raster_name(
                parameter, [o.request for o in outcomes]
            )
            try:
                result = mosaic(
                    [o.path for o in outcomes],
                    target,
                    temporal=first.temporal,
                    parameter=parameter,
                    sources=[],
                )
            # RuntimeError too: GDAL raises plain RuntimeErrors under
            # UseExceptions, so an unreadable cached tile would otherwise escape
            # finished() on the main thread and take QGIS's excepthook with it.
            except (MosaicError, RuntimeError, OSError) as exc:
                report.add(
                    QaFinding(
                        Level.ERROR,
                        "MOSAIC_FAILED",
                        f"Could not build a raster for {parameter}.",
                        detail=str(exc),
                        affected=(parameter,),
                    )
                )
                continue

            report.extend(
                check_valid_range(
                    parameter, result.valid_range, result.out_of_range, first.url
                )
            )
            if result.dropped_bands:
                report.add(
                    QaFinding(
                        Level.INFO,
                        "YYYY13_DROPPED",
                        f"Dropped {len(result.dropped_bands)} annual-mean band(s).",
                        detail=(
                            "POWER's monthly axis carries a thirteenth value per year "
                            "that is the annual mean, not a month. Rendered as a band "
                            "it would be a spurious frame in the animation."
                        ),
                        affected=tuple(str(b) for b in result.dropped_bands),
                    )
                )

            if not self.output.add_to_map.isChecked():
                continue

            grid = grid_for(family_of(parameter))
            name = layer_name(
                parameter,
                result.units,
                first.temporal,
                first.time_standard,
                grid_label=grid.label if grid else "",
                warn=report.has_error,
            )
            layer = build_raster_layer(result.path, name, result.intervals)
            if self.output.auto_style.isChecked():
                style_raster_layer(layer, parameter, result.units)
            QgsProject.instance().addMapLayer(layer)
            added += 1

        self.qa.show_report(report, urls=urls)
        cached = job.cached_count
        note = f" ({cached} tile(s) from cache)" if cached else ""
        self._message(
            f"Added {added} raster layer(s) from {len(job.outcomes)} tile(s){note}.",
            Qgis.MessageLevel.Warning if report.has_error else Qgis.MessageLevel.Success,
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
                converted=self.output.convert_si.isChecked(),
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
        for task in (self._task, self._dictionary_task):
            if task is not None:
                task.on_complete = None
                task.cancel()
        self._task = None
        self._dictionary_task = None
        if self._picker is not None:
            self._picker.clear_markers()
            self.iface.mapCanvas().unsetMapTool(self._picker)
            self._picker = None
        # The controller outlives this dock, so a connection left behind would
        # call into a module that a reload has already deleted.
        controller = getattr(self, "_temporal_connection", None)
        if controller is not None:
            try:
                controller.updateTemporalRange.disconnect(self._temporal_range_changed)
            except (TypeError, RuntimeError):  # pragma: no cover - already gone
                pass
            self._temporal_connection = None


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


def _raster_name(parameter: str, requests: Sequence[PowerRequest]) -> str:
    """A filename that changes whenever the data behind it would.

    The bug this exists to prevent, measured: naming a mosaic
    ``{parameter}-{temporal}-{start}-{end}.tif`` omits the extent, the
    community and the time standard. Fetch ``ALLSKY_SFC_SW_DWN`` for the same
    dates under RE and then under AG and the second write lands on the first
    file -- so a layer whose name says ``[kW-hr/m^2/day]`` ends up drawing
    ``MJ/m^2/day`` values, wrong by a factor of 3.6, in the legend, in Identify
    and in any zonal statistics. It survives into a saved project.

    So the name carries a digest of the **request URLs**, which is the same
    identity ``api.cache_path`` uses for the downloads themselves: anything
    that would change the pixels changes the digest. The readable prefix is
    kept so the directory is still browsable.
    """
    digest = hashlib.sha256(
        "\n".join(sorted(r.url for r in requests)).encode()
    ).hexdigest()[:12]
    first = requests[0]
    return f"{parameter}-{first.temporal}-{first.start}-{first.end}-{digest}.tif"
