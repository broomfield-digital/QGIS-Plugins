"""Turn a mosaicked GeoTIFF into an animating, styled QGIS raster layer.

Two things the GDAL provider will not do for us:

* **Set a CRS.** POWER's NetCDF has none, and although
  :func:`nasa_power.gdalio.mosaic.mosaic` stamps EPSG:4326 into the GeoTIFF,
  :func:`build_raster_layer` sets it again unconditionally. The cost is one
  call; the failure it prevents is a layer that draws in the right place in a
  WGS84 project and in the wrong hemisphere in a projected one.
* **Report temporal capabilities.** Measured:
  ``provider.temporalCapabilities().hasTemporalCapabilities()`` is ``False``
  for these files, so ``TemporalRangeFromDataProvider`` is not an option and
  the per-band ranges have to be supplied explicitly.

The ranges are **half-open**. With closed ranges two adjacent bands both match
an instant on their shared boundary, and the animation flickers between them.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Sequence

from qgis.core import (
    Qgis,
    QgsColorRampShader,
    QgsCoordinateReferenceSystem,
    QgsDateTimeRange,
    QgsRasterLayer,
    QgsRasterShader,
    QgsSingleBandPseudoColorRenderer,
    QgsStyle,
)
from qgis.PyQt.QtCore import QDateTime, QTimeZone

from nasa_power.gdalio.mosaic import band_statistics
from nasa_power.qgis_bridge.styling import CLASS_COUNT, ramp_for

POWER_CRS = QgsCoordinateReferenceSystem("EPSG:4326")


def _qdatetime(value: datetime) -> QDateTime:
    stamp = QDateTime(value)
    stamp.setTimeZone(QTimeZone.utc())
    return stamp


def build_raster_layer(
    path: str | Path,
    name: str,
    intervals: Sequence[tuple[datetime, datetime]],
) -> QgsRasterLayer:
    """Load ``path`` as a raster layer with per-band temporal ranges."""
    layer = QgsRasterLayer(str(path), name, "gdal")
    if not layer.isValid():
        raise RuntimeError(f"Could not load {path} as a raster layer.")

    layer.setCrs(POWER_CRS)
    apply_temporal_properties(layer, intervals)
    return layer


def apply_temporal_properties(
    layer: QgsRasterLayer, intervals: Sequence[tuple[datetime, datetime]]
) -> None:
    """Give each band its own half-open time range.

    Band numbers are 1-based and must line up with ``intervals`` in order --
    which is why the mosaic drops annual-mean bands rather than leaving them
    for a caller to skip here.
    """
    properties = layer.temporalProperties()
    properties.setMode(Qgis.RasterTemporalMode.FixedRangePerBand)
    properties.setFixedRangePerBand(
        {
            index: QgsDateTimeRange(
                _qdatetime(start), _qdatetime(end), True, False
            )
            for index, (start, end) in enumerate(intervals, start=1)
        }
    )
    properties.setIsActive(True)


def style_raster_layer(
    layer: QgsRasterLayer,
    parameter: str,
    units: str = "",
    *,
    limits: tuple[float, float] | None = None,
) -> bool:
    """Apply a pseudocolour ramp with a stretch fixed across every band.

    ``limits`` defaults to the min and max over the **whole** cube. Per-band
    limits would make the animation pulse: the colours would move because the
    scale moved rather than because the field changed, and a viewer reads that
    as signal.

    Returns ``False`` when the raster is entirely nodata -- a legal POWER
    response for an ocean-only box over a land parameter -- rather than leaving
    a renderer configured against an empty range.
    """
    if limits is None:
        limits = band_statistics(layer.source())
    if limits is None:
        return False

    low, high = limits
    if low == high:
        pad = abs(low) * 0.01 or 1.0
        low, high = low - pad, high + pad

    ramp_name, invert = ramp_for(parameter, units)
    ramp = QgsStyle.defaultStyle().colorRamp(ramp_name)
    if ramp is None:  # pragma: no cover - guarded by ramp_for
        return False
    if invert:
        ramp.invert()

    shader_function = QgsColorRampShader(low, high)
    shader_function.setColorRampType(Qgis.ShaderInterpolationMethod.Linear)
    # EqualInterval, not Continuous: measured on this build, Continuous ignores
    # the requested class count entirely (asked for 11, produced 5).
    shader_function.setClassificationMode(Qgis.ShaderClassificationMethod.EqualInterval)
    shader_function.setSourceColorRamp(ramp)
    # (classes, band, extent, input). The min/max handed to the constructor are
    # already the whole-cube range, so no band needs sampling -- passing band
    # -1 keeps it from re-deriving limits from band 1 alone, which is exactly
    # the per-band stretch this function exists to avoid.
    shader_function.classifyColorRamp(CLASS_COUNT, -1)

    shader = QgsRasterShader()
    shader.setRasterShaderFunction(shader_function)

    renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
    renderer.setClassificationMin(low)
    renderer.setClassificationMax(high)
    layer.setRenderer(renderer)
    layer.triggerRepaint()
    return True


def band_for(layer: QgsRasterLayer, moment: datetime) -> int:
    """Which band the Temporal Controller shows at ``moment``. 0 if none."""
    stamp = _qdatetime(moment)
    band = layer.temporalProperties().bandForTemporalRange(
        layer, QgsDateTimeRange(stamp, stamp)
    )
    # Measured: QGIS answers -1 for an instant outside every band's range. Bands
    # are 1-based, so -1 is not a "no band" a caller can test for -- it is
    # truthy, and `if band_for(...)` would hand it straight to a renderer as a
    # band number. Normalise to the 0 this function documents.
    return band if band > 0 else 0
