"""Show what the plugin noticed about a fetch, and let the user take it away.

The QA report is the plugin's honesty surface. It goes three places -- layer
metadata, the Processing feedback stream, and here -- and this is the one a
user actually sees, so it carries the offending URL beside each row: a POWER
422 names the field it rejected, and without the request beside it that message
cannot be acted on.

"Copy citation" and "Copy request URLs" exist because both are things people
need *outside* QGIS -- in a paper, in a bug report -- and retyping a 300-
character URL from a table is not a plan.
"""

from __future__ import annotations

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from nasa_power.core.qa import Level, QaReport

#: Severity colours. Deliberately muted -- the panel must read in both the
#: light and dark QGIS themes, and a saturated red on a dark background is
#: harder to read than the word "Error" already is.
LEVEL_COLORS = {
    Level.BLOCKING: QColor(190, 60, 60),
    Level.ERROR: QColor(190, 90, 40),
    Level.WARNING: QColor(170, 140, 40),
    Level.INFO: QColor(110, 130, 150),
}


class QaPanel(QWidget):
    """A table of findings, plus the two things worth copying out."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._citation = ""
        self._urls: list[str] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.summary = QLabel("No fetch yet.")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["", "Finding", "Request"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setWordWrap(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setMaximumHeight(180)
        layout.addWidget(self.table)

        buttons = QHBoxLayout()
        self.copy_citation = QPushButton("Copy citation")
        self.copy_urls = QPushButton("Copy request URLs")
        self.copy_citation.setEnabled(False)
        self.copy_urls.setEnabled(False)
        buttons.addWidget(self.copy_citation)
        buttons.addWidget(self.copy_urls)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.copy_citation.clicked.connect(self._copy_citation)
        self.copy_urls.clicked.connect(self._copy_urls)

    def show_report(self, report: QaReport, citation: str = "", urls=()) -> None:
        """Replace the table with ``report``'s findings, worst first."""
        self._citation = citation
        self._urls = list(urls)
        self.copy_citation.setEnabled(bool(citation))
        self.copy_urls.setEnabled(bool(self._urls))

        self.summary.setText(report.summary())

        findings = sorted(report.findings, key=lambda f: -int(f.level))
        self.table.setRowCount(len(findings))
        for row, finding in enumerate(findings):
            level_item = QTableWidgetItem(finding.level.label)
            level_item.setForeground(QBrush(LEVEL_COLORS.get(finding.level, QColor())))
            self.table.setItem(row, 0, level_item)

            text = finding.message
            message_item = QTableWidgetItem(text)
            # The detail is where the measurement and the "what to do" live;
            # it is too long for a cell but exactly right for a tooltip.
            tooltip = "\n\n".join(p for p in (finding.detail, finding.url) if p)
            if tooltip:
                message_item.setToolTip(tooltip)
            self.table.setItem(row, 1, message_item)

            url_item = QTableWidgetItem("open" if finding.url else "")
            if finding.url:
                url_item.setToolTip(finding.url)
                url_item.setData(Qt.ItemDataRole.UserRole, finding.url)
            self.table.setItem(row, 2, url_item)

        self.table.resizeRowsToContents()

    def clear(self) -> None:
        self.table.setRowCount(0)
        self.summary.setText("No fetch yet.")
        self.copy_citation.setEnabled(False)
        self.copy_urls.setEnabled(False)

    def _copy_citation(self) -> None:
        QApplication.clipboard().setText(self._citation)

    def _copy_urls(self) -> None:
        QApplication.clipboard().setText("\n".join(self._urls))
