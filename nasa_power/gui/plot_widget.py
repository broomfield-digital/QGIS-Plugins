"""A time-series chart in the dock, drawn with QGIS's own charting API.

``QgsLineChartPlot`` is new in QGIS 4 and is the right tool here for three
reasons: it needs no dependency, it is styled with QGIS symbols so the chart
matches the map, and the same plot object drops into a print layout when the
figure needs to leave QGIS.

What it is *not* is a widget. ``QgsLineChartPlotWidget`` is a settings editor
(only ``setPlot``/``createPlot``) and ``QgsPlotCanvas`` is a ``QGraphicsView``
base for tool-driven canvases. So the chart is painted directly: a plain
``QWidget`` whose ``paintEvent`` builds a ``QgsRenderContext`` from its own
``QPainter`` and calls ``plot.render()``.

**There is no datetime axis.** Measured: ``Qgis.PlotAxisType`` has exactly two
members, ``Categorical`` and ``Interval``. So time is carried as a series index
and the labels are pre-formatted and thinned to fit -- which is also why the
x-axis of a monthly series reads as months rather than as a squeezed calendar.

DataPlotly is not an alternative on this build: its plot view is a
``QWebEngineView`` and QtWebEngine is not in the bundle at all.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from qgis.core import (
    Qgis,
    QgsLineChartPlot,
    QgsLineSymbol,
    QgsMargins,
    QgsPlotData,
    QgsPlotRenderContext,
    QgsRenderContext,
    QgsTextFormat,
    QgsXyPlotSeries,
)
from qgis.PyQt.QtCore import QPointF, QRectF, QSizeF, Qt
from qgis.PyQt.QtGui import QColor, QPainter, QPen
from qgis.PyQt.QtWidgets import QSizePolicy, QWidget

#: Series colours, in order. Chosen to stay distinguishable in both QGIS themes
#: and under the common forms of colour blindness -- a chart of two parameters
#: at three sites is six lines, and a legend that needs colour discrimination
#: to read is not a legend.
SERIES_COLORS = (
    "#4477aa", "#ee6677", "#228833", "#ccbb44", "#66ccee", "#aa3377", "#bbbbbb",
)

#: Roughly how many x labels fit before they collide. A month of daily data is
#: 29 stamps; drawing all of them produces a black smear.
MAX_X_LABELS = 8

MIN_HEIGHT = 180


class PowerPlotWidget(QWidget):
    """Draws one or more labelled series against a shared categorical x-axis."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(MIN_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self._series: list[tuple[str, list[tuple[datetime, float]]]] = []
        self._categories: list[str] = []
        self._units = ""
        #: Index of the timestep the Temporal Controller is showing, or -1.
        self._cursor_index = -1

    # ------------------------------------------------------------------ #

    def set_series(
        self,
        series: Sequence[tuple[str, Sequence[tuple[datetime, float]]]],
        units: str = "",
    ) -> None:
        """Replace the chart's contents.

        ``series`` is ``(label, [(time, value), ...])``. Series need not share
        timestamps; the union is used for the axis and a series simply has no
        point where it has no value -- which is what a POWER fill looks like
        after masking, and it should read as a gap rather than as a zero.
        """
        self._series = [(label, list(points)) for label, points in series]
        self._units = units

        moments = sorted({t for _label, points in self._series for t, _v in points})
        self._categories = [self._format(t, moments) for t in moments]
        self._moments = moments
        self.update()

    def set_cursor(self, moment: datetime | None) -> None:
        """Draw a vertical line at ``moment``, tracking the Temporal Controller.

        ``moment`` is normalised to UTC first. ``QDateTime.toPyDateTime()``
        returns a **naive** datetime even for a UTC-stamped range, and the
        series timestamps are timezone-aware, so comparing them raw is a
        TypeError -- which would take the whole signal handler down every time
        the user moved the slider.
        """
        if moment is None or not getattr(self, "_moments", None):
            self._cursor_index = -1
            self.update()
            return

        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        else:
            moment = moment.astimezone(timezone.utc)

        # Nearest timestep at or before the frame, so the cursor sits on the
        # sample being shown rather than between two.
        self._cursor_index = max(
            (i for i, t in enumerate(self._moments) if t <= moment), default=-1
        )
        self.update()

    def clear(self) -> None:
        self._series = []
        self._categories = []
        self._moments = []
        self._cursor_index = -1
        self.update()

    # ------------------------------------------------------------------ #

    @staticmethod
    def _format(moment: datetime, all_moments: Sequence[datetime]) -> str:
        """Format one x label at a resolution the series actually needs."""
        if len(all_moments) < 2:
            return moment.strftime("%Y-%m-%d")
        span = all_moments[-1] - all_moments[0]
        if span.total_seconds() <= 3 * 86400:
            return moment.strftime("%d %H:%M")
        if span.days <= 400:
            return moment.strftime("%m-%d")
        return moment.strftime("%Y-%m")

    def _thinned_categories(self) -> list[str]:
        """Blank out labels that would collide, keeping the ends."""
        count = len(self._categories)
        if count <= MAX_X_LABELS:
            return list(self._categories)
        step = max(1, round(count / MAX_X_LABELS))
        return [
            label if (index % step == 0 or index == count - 1) else ""
            for index, label in enumerate(self._categories)
        ]

    def _build_plot(self, size: QSizeF) -> tuple[QgsLineChartPlot, QgsPlotData]:
        plot = QgsLineChartPlot.create()
        plot.setSize(size)
        plot.setMargins(QgsMargins(14, 6, 6, 18))
        # On the axis, not the plot: `setXAxisType` belongs to
        # QgsVectorLayerXyPlotDataGatherer, which is the layer-driven path.
        plot.xAxis().setType(Qgis.PlotAxisType.Categorical)

        values = [v for _label, points in self._series for _t, v in points]
        low, high = (min(values), max(values)) if values else (0.0, 1.0)
        if low == high:
            pad = abs(low) * 0.05 or 1.0
            low, high = low - pad, high + pad
        else:
            pad = (high - low) * 0.05
            low, high = low - pad, high + pad

        plot.setXMinimum(0)
        plot.setXMaximum(max(len(self._categories) - 1, 1))
        plot.setYMinimum(low)
        plot.setYMaximum(high)

        text = QgsTextFormat()
        text.setSize(7)
        plot.xAxis().setTextFormat(text)
        plot.yAxis().setTextFormat(text)
        if self._units:
            plot.yAxis().setLabelSuffix(f" {self._units}")
            plot.yAxis().setLabelSuffixPlacement(
                Qgis.PlotAxisSuffixPlacement.LastLabel
            )

        data = QgsPlotData()
        data.setCategories(self._thinned_categories())

        index_of = {moment: i for i, moment in enumerate(getattr(self, "_moments", []))}
        for order, (_label, points) in enumerate(self._series):
            series = QgsXyPlotSeries()
            for moment, value in points:
                series.append(float(index_of.get(moment, 0)), float(value))
            data.addSeries(series)

            symbol = QgsLineSymbol.createSimple(
                {
                    "color": SERIES_COLORS[order % len(SERIES_COLORS)],
                    "width": "0.5",
                }
            )
            # Unconditional: a fresh plot reports lineSymbolCount() == 1, but
            # setLineSymbolAt grows the list, so guarding on the count would
            # leave every series after the first with the default colour and
            # make a six-line chart unreadable.
            plot.setLineSymbolAt(order, symbol)

        return plot, data

    # ------------------------------------------------------------------ #

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        try:
            if not self._series:
                self._paint_placeholder(painter)
                return

            size = QSizeF(self.width(), self.height())
            plot, data = self._build_plot(size)

            context = QgsRenderContext.fromQPainter(painter)
            plot.render(context, QgsPlotRenderContext(), data)
            self._paint_cursor(painter, plot)
            self._paint_legend(painter)
        finally:
            painter.end()

    def _paint_placeholder(self, painter: QPainter) -> None:
        painter.setPen(QPen(self.palette().mid().color()))
        painter.drawText(
            self.rect(),
            Qt.AlignmentFlag.AlignCenter,
            "Fetch a series to see it plotted here.",
        )

    def _paint_cursor(self, painter: QPainter, plot: QgsLineChartPlot) -> None:
        """A vertical line at the frame the Temporal Controller is showing.

        Drawn over the plot rather than through it: the plot API has no cursor,
        and the x mapping is simple enough (categorical, evenly spaced) to
        reproduce from the margins.
        """
        if self._cursor_index < 0 or len(self._categories) < 2:
            return

        margins = plot.margins()
        left = margins.left()
        right = self.width() - margins.right()
        if right <= left:
            return

        fraction = self._cursor_index / (len(self._categories) - 1)
        x = left + fraction * (right - left)
        pen = QPen(QColor(242, 166, 59, 200))
        pen.setWidth(1)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawLine(
            QPointF(x, margins.top()), QPointF(x, self.height() - margins.bottom())
        )

    def _paint_legend(self, painter: QPainter) -> None:
        """A compact legend. Six lines need one; one line does not."""
        if len(self._series) < 2:
            return

        painter.save()
        metrics = painter.fontMetrics()
        x = 20.0
        y = 4.0
        swatch = 8.0
        for order, (label, _points) in enumerate(self._series):
            colour = QColor(SERIES_COLORS[order % len(SERIES_COLORS)])
            width = swatch + 4 + metrics.horizontalAdvance(label) + 10
            if x + width > self.width():
                break
            painter.fillRect(QRectF(x, y + 3, swatch, 2), colour)
            painter.setPen(QPen(self.palette().text().color()))
            painter.drawText(QPointF(x + swatch + 4, y + metrics.ascent()), label)
            x += width
        painter.restore()
