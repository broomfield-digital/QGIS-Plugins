"""Build the point layer: one feature per (site, parameter, timestep).

The **long form**. It repeats a point geometry for every timestep, which looks
wasteful until you try the alternatives:

* **Wide** -- one feature per site, one field per timestep -- is what a naive
  GeoJSON read gives you. It cannot animate at all:
  ``QgsVectorLayerTemporalProperties`` reads *fields*, not field *names*. It
  also grows a field per timestep, so a year of hourly data asks for 8760
  columns.
* **A joined table** is 1:1 in QGIS, and a time series is 1:N; the join
  silently keeps one row.
* **A related child table** is relationally correct and the temporal
  controller does not traverse relations, so the slider still does nothing.

Long form is the only layout the time slider drives, and geometry is 16 bytes.

The mode is ``FeatureDateTimeStartAndEndFromFields``, not
``...InstantFromField``. Measured: instant mode plus a fixed duration widens
the window and selects a day either side of the frame; two explicit fields give
the exact half-open interval.
"""

from __future__ import annotations

from typing import Sequence

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QDateTime, QTimeZone, QVariant

from nasa_power.core.decode import Observation

#: POWER speaks degrees and nothing else.
POWER_CRS = QgsCoordinateReferenceSystem("EPSG:4326")

#: The layer schema. Order is the attribute-table column order, so it runs
#: identity -> time -> value -> provenance, which is how it gets read.
FIELDS: tuple[tuple[str, str, int], ...] = (
    ("site", "string", 64),
    ("parameter", "string", 32),
    ("t_start", "datetime", 0),
    ("t_end", "datetime", 0),
    ("value", "double", 0),
    ("units", "string", 24),
    ("native_value", "double", 0),
    ("native_units", "string", 24),
    ("longitude", "double", 0),
    ("latitude", "double", 0),
    ("elevation_m", "double", 0),
    ("cell_longitude", "double", 0),
    ("cell_latitude", "double", 0),
    ("family", "string", 24),
    ("temporal", "string", 12),
    ("time_standard", "string", 8),
    ("source", "string", 32),
)

_FIELD_NAMES = tuple(name for name, _type, _len in FIELDS)


def memory_uri() -> str:
    """The provider URI for the long-form memory layer."""
    parts = ["Point?crs=EPSG:4326"]
    for name, kind, length in FIELDS:
        parts.append(f"field={name}:{kind}({length})" if length else f"field={name}:{kind}")
    # Indexed so the temporal controller's per-frame filter does not scan.
    parts.append("index=yes")
    return "&".join(parts)


def _qdatetime(value) -> QDateTime:
    """A timezone-explicit ``QDateTime``.

    Explicit UTC matters: a naive ``QDateTime`` is interpreted in local time,
    which would shift every timestamp by the machine's offset and put the
    plugin's own 7-hour LST trap back in by another route.
    """
    stamp = QDateTime(value)
    stamp.setTimeZone(QTimeZone.utc())
    return stamp


def build_point_layer(
    observations: Sequence[Observation],
    name: str,
    *,
    time_standard: str = "UTC",
    sources: Sequence[str] = (),
) -> QgsVectorLayer:
    """Assemble a memory layer from observations. **Main thread only.**

    Features are added in one batched call: a year of hourly data at several
    sites runs to tens of thousands of features, and per-feature commits would
    make the pause after a fetch far worse than the fetch.
    """
    layer = QgsVectorLayer(memory_uri(), name, "memory")
    if not layer.isValid():
        raise RuntimeError(f"Could not create the memory layer for {name!r}.")

    source_text = ",".join(sources)
    provider = layer.dataProvider()
    fields = layer.fields()

    features: list[QgsFeature] = []
    for observation in observations:
        feature = QgsFeature(fields)
        feature.setGeometry(
            QgsGeometry.fromPointXY(QgsPointXY(observation.longitude, observation.latitude))
        )
        feature.setAttributes(
            [
                observation.site,
                observation.parameter,
                _qdatetime(observation.t_start),
                _qdatetime(observation.t_end),
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
                time_standard.upper(),
                source_text,
            ]
        )
        features.append(feature)

    provider.addFeatures(features)
    layer.updateExtents()
    apply_temporal_properties(layer)
    return layer


def apply_temporal_properties(layer: QgsVectorLayer) -> None:
    """Wire the layer to the Temporal Controller.

    ``FeatureDateTimeStartAndEndFromFields`` with an explicit half-open
    interval. Instant mode plus a duration was measured to over-select: for a
    controller range of Feb 2-3 it produced a filter matching Feb 1 as well.
    """
    properties = layer.temporalProperties()
    properties.setMode(Qgis.VectorTemporalMode.FeatureDateTimeStartAndEndFromFields)
    properties.setStartField("t_start")
    properties.setEndField("t_end")
    properties.setIsActive(True)


def merge_into(layer: QgsVectorLayer, observations: Sequence[Observation], **kwargs) -> int:
    """Append observations to an existing layer. Returns the number added.

    Used when a second site or parameter arrives for a layer already on the
    map, so a multi-request fetch produces one layer rather than one per
    request.
    """
    extra = build_point_layer(observations, "scratch", **kwargs)
    features = list(extra.getFeatures())
    layer.dataProvider().addFeatures(features)
    layer.updateExtents()
    layer.triggerRepaint()
    return len(features)


def to_wgs84(point: QgsPointXY, source_crs: QgsCoordinateReferenceSystem) -> QgsPointXY:
    """Reproject a canvas click into the degrees POWER expects.

    Not optional: in a Web Mercator project a raw canvas coordinate is metres
    in the millions, and POWER would reject it -- or worse, in a project whose
    units happen to look plausible, quietly answer for the wrong place.
    """
    if source_crs == POWER_CRS:
        return point
    transform = QgsCoordinateTransform(source_crs, POWER_CRS, QgsProject.instance())
    return transform.transform(point)


def parameters_in(layer: QgsVectorLayer) -> list[str]:
    """Distinct parameter codes present, sorted.

    Sorted rather than first-seen: ``uniqueValues()`` hands back a ``set``, so
    encounter order is not available at all, and a combo box rebuilt from an
    arbitrary order would reshuffle itself every time a layer changed.
    """
    index = layer.fields().indexOf("parameter")
    if index < 0:
        return []
    return sorted({str(v) for v in layer.uniqueValues(index)})


def sites_in(layer: QgsVectorLayer) -> list[str]:
    """Distinct site labels present."""
    index = layer.fields().indexOf("site")
    if index < 0:
        return []
    return sorted({str(v) for v in layer.uniqueValues(index)})


def series(
    layer: QgsVectorLayer, parameter: str, site: str
) -> list[tuple[QDateTime, float]]:
    """The ``(time, value)`` pairs for one parameter at one site, time-ordered.

    Feeds the dock's chart. Nulls are dropped rather than plotted as zero --
    POWER's fill is a real absence, and a zero would read as darkness at noon.
    """
    from qgis.core import QgsExpression, QgsFeatureRequest

    # quotedValue(), never an f-string. Site labels come from whatever field a
    # user's own point layer calls "name"/"site"/"station", so an apostrophe --
    # O'Hare, Coeur d'Alene -- is ordinary, and it closes the string literal
    # early. QGIS answers an invalid filter expression with zero features and
    # no error at all, so the chart would simply be blank; a label crafted to
    # close the literal and open an always-true clause would instead plot every
    # site's values under a name matching none of them.
    expression = (
        f"{QgsExpression.quotedColumnRef('parameter')} = "
        f"{QgsExpression.quotedValue(parameter)} AND "
        f"{QgsExpression.quotedColumnRef('site')} = {QgsExpression.quotedValue(site)}"
    )
    request = QgsFeatureRequest().setFilterExpression(expression)
    points: list[tuple[QDateTime, float]] = []
    for feature in layer.getFeatures(request):
        value = feature["value"]
        if value is None or isinstance(value, QVariant) and value.isNull():
            continue
        points.append((feature["t_start"], float(value)))
    points.sort(key=lambda pair: pair[0])
    return points
