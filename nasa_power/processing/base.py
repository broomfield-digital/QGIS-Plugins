"""Shared plumbing for the NASA POWER Processing algorithms.

The algorithms deliberately delegate to the same ``core`` and ``qgis_bridge``
functions the dock uses. That is the point of the ring split: a value fetched
from the toolbox, from ``qgis_process`` on a cluster, or by clicking Fetch in
the panel goes through one code path, so all three can only be wrong in the
same way.

Two behaviours are shared and worth stating once:

* **QA findings go to ``feedback.pushWarning``**, so the command-line path
  reports exactly what the dock's Notes panel reports. A BLOCKING finding
  aborts before any HTTP happens.
* **The cache is shared with the dock**, resolved through
  ``qgis_bridge.paths``. A ``qgis_process`` run therefore reuses whatever the
  panel already downloaded -- which matters because POWER asks not to be
  re-queried for the same location.
"""

from __future__ import annotations

from datetime import date

from qgis.core import (
    Qgis,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterDateTime,
    QgsProcessingParameterEnum,
    QgsProcessingParameterString,
)
from qgis.PyQt.QtCore import QDate, QDateTime, QTime

from nasa_power.core.api import COMMUNITIES, TEMPORAL_LEVELS
from nasa_power.core.qa import Level, QaReport

TIME_STANDARDS = ("UTC", "LST")

P_TEMPORAL = "TEMPORAL"
P_START = "START"
P_END = "END"
P_COMMUNITY = "COMMUNITY"
P_TIME_STANDARD = "TIME_STANDARD"


class PowerAlgorithm(QgsProcessingAlgorithm):
    """Base for the POWER algorithms: shared parameters and QA reporting."""

    def group(self) -> str:
        return "Fetch"

    def groupId(self) -> str:  # noqa: N802 - QGIS API
        return "fetch"

    def createInstance(self):  # noqa: N802 - QGIS API
        # Mandatory: QGIS clones the algorithm per run, and the default
        # implementation raises.
        return type(self)()

    # ------------------------------------------------------------------ #

    def add_common_parameters(self, *, gridded: bool) -> None:
        """Temporal level, window, community and time standard."""
        levels = [t for t in TEMPORAL_LEVELS if not (gridded and t == "hourly")]
        self.addParameter(
            QgsProcessingParameterEnum(
                P_TEMPORAL,
                "Temporal resolution"
                + (" (POWER has no hourly gridded endpoint)" if gridded else ""),
                options=levels,
                # Static strings, so the command line reads
                # `TEMPORAL=daily` rather than `TEMPORAL=1`. An opaque index is
                # both unreadable in a saved model and silently wrong when the
                # option list changes -- here it already differs between the
                # point and gridded algorithms, because gridded has no hourly.
                defaultValue="daily",
                usesStaticStrings=True,
            )
        )
        self._levels = levels

        self.addParameter(
            QgsProcessingParameterDateTime(
                P_START,
                "Start date",
                type=Qgis.ProcessingDateTimeParameterDataType.Date,
                # Qt6 dropped the single-argument QDateTime(QDate) overload;
                # a QTime is mandatory now.
                defaultValue=QDateTime(QDate(2024, 2, 1), QTime(0, 0)),
            )
        )
        self.addParameter(
            QgsProcessingParameterDateTime(
                P_END,
                "End date",
                type=Qgis.ProcessingDateTimeParameterDataType.Date,
                defaultValue=QDateTime(QDate(2024, 2, 29), QTime(0, 0)),
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                P_COMMUNITY,
                "Community (changes the native units of radiation parameters)",
                options=list(COMMUNITIES),
                defaultValue="RE",
                usesStaticStrings=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                P_TIME_STANDARD,
                "Time standard",
                options=list(TIME_STANDARDS),
                defaultValue="UTC",
                usesStaticStrings=True,
            )
        )

    def read_common(self, parameters, context) -> dict:
        """Pull the shared parameters out as plain Python values."""
        temporal = self.parameterAsEnumString(parameters, P_TEMPORAL, context)
        community = self.parameterAsEnumString(parameters, P_COMMUNITY, context)
        time_standard = self.parameterAsEnumString(parameters, P_TIME_STANDARD, context)
        return {
            "temporal": temporal,
            "community": community,
            "time_standard": time_standard,
            "start": _as_date(self.parameterAsDateTime(parameters, P_START, context)),
            "end": _as_date(self.parameterAsDateTime(parameters, P_END, context)),
        }

    # ------------------------------------------------------------------ #

    def report(self, report: QaReport, feedback) -> None:
        """Push every finding to the feedback stream, worst first.

        Identical to what the dock's Notes panel shows, so a headless run is
        not a quieter run.
        """
        for finding in sorted(report.findings, key=lambda f: -int(f.level)):
            text = f"{finding.code}: {finding.message}"
            if finding.detail:
                text = f"{text}\n  {finding.detail}"
            if finding.url:
                text = f"{text}\n  {finding.url}"
            if finding.level >= Level.WARNING:
                feedback.pushWarning(text)
            else:
                feedback.pushInfo(text)

    def refuse_if_blocked(self, report: QaReport, feedback) -> None:
        """Abort before any HTTP if pre-flight found something fatal."""
        self.report(report, feedback)
        if report.is_blocked:
            raise QgsProcessingException(
                "; ".join(f.message for f in report.blocking)
            )


def _as_date(value: QDateTime) -> date:
    return value.date().toPyDate()
