"""``nasapower:powerpoint`` -- fetch a POWER time series at points.

Takes either a point layer or a typed lat/lon, and writes the same long-form
table the dock builds: one feature per (site, parameter, timestep). That layout
is what the Temporal Controller can animate, and keeping it identical to the
dock's means a series exported here and a series fetched interactively are the
same thing.
"""

from __future__ import annotations

import json
from pathlib import Path

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingException,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterPoint,
    QgsProcessingParameterString,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QDateTime, QMetaType, QTimeZone

from nasa_power.core.api import fetch_to_cache, plan_requests
from nasa_power.core.citation import build_citation
from nasa_power.core.decode import parse_point_response
from nasa_power.core.errors import PowerError
from nasa_power.core.qa import QaReport, postfetch, preflight
from nasa_power.processing.base import PowerAlgorithm
from nasa_power.qgis_bridge import paths
from nasa_power.qgis_bridge.layers_point import FIELDS, to_wgs84

P_PARAMETERS = "PARAMETERS"
P_SITES = "SITES"
P_POINT = "POINT"
P_LABEL_FIELD = "LABEL_FIELD"
P_CONVERT_SI = "CONVERT_SI"
P_OUTPUT = "OUTPUT"


class PowerPointAlgorithm(PowerAlgorithm):
    """Fetch POWER data at one or more points."""

    def name(self) -> str:
        return "powerpoint"

    def displayName(self) -> str:  # noqa: N802 - QGIS API
        return "NASA POWER at points"

    def shortHelpString(self) -> str:  # noqa: N802 - QGIS API
        return (
            "Fetches NASA POWER parameters at one or more points and writes a "
            "long-form table: one feature per site, parameter and timestep.\n\n"
            "Parameters are POWER codes, comma separated, e.g. "
            "<code>T2M,ALLSKY_SFC_SW_DWN</code>. They are split into separate "
            "requests by parent dataset, so each value is attributable to "
            "MERRA-2 or to CERES rather than to both.\n\n"
            "Timestamps default to UTC. POWER's own default is Local Solar "
            "Time, which is roughly a seven-hour offset at mid-latitudes.\n\n"
            "Responses are cached on disk and shared with the NASA POWER panel, "
            "so a repeated request makes no network call."
        )

    def initAlgorithm(self, config=None) -> None:  # noqa: N802 - QGIS API
        self.addParameter(
            QgsProcessingParameterString(
                P_PARAMETERS,
                "POWER parameters (comma separated)",
                defaultValue="T2M,ALLSKY_SFC_SW_DWN",
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                P_SITES,
                "Point layer (optional; overrides the single point below)",
                types=[Qgis.ProcessingSourceType.VectorPoint],
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                P_LABEL_FIELD,
                "Field to label sites with (optional)",
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterPoint(
                P_POINT,
                "Single point",
                defaultValue="-105.27,40.02 [EPSG:4326]",
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                P_CONVERT_SI,
                "Convert to SI units (off keeps POWER's native units)",
                defaultValue=False,
            )
        )
        self.add_common_parameters(gridded=False)
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                P_OUTPUT, "POWER series", type=Qgis.ProcessingSourceType.VectorPoint
            )
        )

    # ------------------------------------------------------------------ #

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        common = self.read_common(parameters, context)
        codes = [
            p.strip().upper()
            for p in self.parameterAsString(parameters, P_PARAMETERS, context).split(",")
            if p.strip()
        ]
        sites = self._sites(parameters, context, feedback)
        if not sites:
            raise QgsProcessingException(
                "No sites: give a point layer with at least one feature, or a point."
            )

        report = preflight(
            temporal=common["temporal"],
            mode="point",
            parameters=codes,
            start=common["start"],
            end=common["end"],
            sites=sites,
            time_standard=common["time_standard"],
            community=common["community"],
        )
        self.refuse_if_blocked(report, feedback)

        requests = plan_requests(
            common["temporal"],
            "point",
            codes,
            start=common["start"],
            end=common["end"],
            sites=sites,
            community=common["community"],
            fmt="JSON",
            time_standard=common["time_standard"],
        )
        feedback.pushInfo(f"{len(requests)} request(s) planned.")

        sink, dest_id = self.parameterAsSink(
            parameters,
            P_OUTPUT,
            context,
            _fields(),
            QgsWkbTypes.Type.Point,
            QgsCoordinateReferenceSystem("EPSG:4326"),
        )
        if sink is None:
            raise QgsProcessingException("Could not create the output sink.")

        convert_si = self.parameterAsBool(parameters, P_CONVERT_SI, context)
        cache_dir = paths.resolve_cache_dir()
        fetcher = self.fetcher(feedback)
        written = 0
        citation = ""

        for index, request in enumerate(requests):
            if feedback.isCanceled():
                break
            try:
                path, was_cached = fetch_to_cache(request, cache_dir, fetcher)
                payload = json.loads(Path(path).read_bytes())
            except (PowerError, OSError, ValueError) as exc:
                # One failed request must not lose the others, and must not
                # leave a half-written sink behind a raw traceback.
                feedback.reportError(
                    f"Request failed for {', '.join(request.params)} at "
                    f"{request.site}: {exc}",
                    fatalError=False,
                )
                continue
            observations, facts = parse_point_response(
                payload,
                temporal=request.temporal,
                requested=request.params,
                site=request.site or "site",
                url=request.url,
                convert=convert_si,
            )
            raw_keys = sorted(
                {
                    key
                    for series in payload.get("properties", {})
                    .get("parameter", {})
                    .values()
                    for key in series
                }
            )
            self.report(
                postfetch(
                    request, facts, observations, raw_time_keys=raw_keys,
                    was_cached=was_cached,
                ),
                feedback,
            )
            citation = citation or build_citation(
                facts.api_name, facts.api_version, sources=facts.sources
            )

            for observation in observations:
                sink.addFeature(
                    _feature(observation, facts.time_standard, facts.sources),
                    QgsFeatureSink.Flag.FastInsert,
                )
                written += 1
            feedback.setProgress(100.0 * (index + 1) / len(requests))

        feedback.pushInfo(f"Wrote {written} feature(s).")
        if citation:
            feedback.pushInfo(f"Cite as: {citation}")
        return {P_OUTPUT: dest_id}

    # ------------------------------------------------------------------ #

    def _sites(self, parameters, context, feedback) -> list[dict]:
        source = self.parameterAsSource(parameters, P_SITES, context)
        if source is not None:
            label_field = self.parameterAsString(parameters, P_LABEL_FIELD, context)
            crs = source.sourceCrs()
            out = []
            for index, feature in enumerate(source.getFeatures(), start=1):
                geometry = feature.geometry()
                if geometry.isEmpty():
                    continue
                # Reprojected per feature: POWER takes degrees, and a layer in
                # Web Mercator would otherwise send metres in the millions.
                point = to_wgs84(geometry.asPoint(), crs)
                name = (
                    str(feature[label_field])
                    if label_field and label_field in source.fields().names()
                    else f"feature_{index}"
                )
                out.append(
                    {"name": name, "latitude": point.y(), "longitude": point.x()}
                )
            return out

        # parameterAsPoint returns QgsPointXY(0, 0) for an empty value rather
        # than None, so an unset point silently fetched the Gulf of Guinea.
        # Check the raw parameter instead.
        if not str(parameters.get(P_POINT) or "").strip():
            return []
        point = self.parameterAsPoint(
            parameters, P_POINT, context, QgsCoordinateReferenceSystem("EPSG:4326")
        )
        if point is None:
            return []
        return [{"name": "point", "latitude": point.y(), "longitude": point.x()}]


_TYPES = {
    "string": QMetaType.Type.QString,
    "double": QMetaType.Type.Double,
    "datetime": QMetaType.Type.QDateTime,
}


def _fields() -> QgsFields:
    """The same schema ``layers_point`` builds, so the two are interchangeable."""
    fields = QgsFields()
    for name, kind, length in FIELDS:
        field = QgsField(name, _TYPES[kind])
        if length:
            field.setLength(length)
        fields.append(field)
    return fields


def _feature(observation, time_standard: str, sources) -> QgsFeature:
    feature = QgsFeature(_fields())
    feature.setGeometry(
        QgsGeometry.fromPointXY(
            QgsPointXY(observation.longitude, observation.latitude)
        )
    )

    def stamp(value):
        out = QDateTime(value)
        out.setTimeZone(QTimeZone.utc())
        return out

    feature.setAttributes(
        [
            observation.site,
            observation.parameter,
            stamp(observation.t_start),
            stamp(observation.t_end),
            observation.value,
            observation.units,
            observation.native_value,
            observation.native_units,
            observation.longitude,
            observation.latitude,
            observation.elevation_m,
            observation.cell_longitude,
            observation.cell_latitude,
            observation.family.value,
            observation.temporal,
            (time_standard or "UTC").upper(),
            ",".join(sources),
        ]
    )
    return feature
