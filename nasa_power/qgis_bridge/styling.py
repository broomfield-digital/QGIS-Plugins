"""Auto-style output so a fetch lands looking like the thing it is.

Two decisions carry most of the value:

* **A ramp per variable class.** Irradiance reads as a warm sequential ramp,
  temperature as a diverging one about its own midpoint, precipitation as
  blues. Getting this from the units string means it works for parameters no
  one has hand-listed.
* **A stretch computed once, across the whole series.** Re-deriving limits per
  frame makes an animation pulse: the colours shift because the scale moved,
  not because the weather did, and a viewer reads that as signal.

  On the point path this comes free -- the graduated renderer classifies over
  every feature in the layer, and the long form puts every timestep in the same
  layer, so the classification *is* over the whole series. :func:`fixed_range`
  exists for the raster path, where each band would otherwise be stretched
  against itself; it has no caller until the gridded milestone lands.

Ramp names in this build are capitalised -- ``'Viridis'``, ``'RdBu'`` -- and
there are 35 of them. Every name used here is checked against
``QgsStyle.defaultStyle()`` at call time and falls back rather than returning
an unstyled layer.
"""

from __future__ import annotations

from typing import Sequence

from qgis.core import (
    QgsClassificationEqualInterval,
    QgsGraduatedSymbolRenderer,
    QgsStyle,
    QgsSymbol,
    QgsVectorLayer,
)

from nasa_power.core.display import variable_class

#: Variable class -> ramp name. Chosen for what the quantity *is*: sequential
#: where zero is a real floor (irradiance, precipitation, wind), diverging
#: where the interesting structure is either side of a middle (temperature,
#: pressure), cyclic-ish where the value wraps (direction).
RAMPS: dict[str, str] = {
    "irradiance": "Inferno",
    "temperature": "RdBu",
    "precipitation": "Blues",
    "humidity": "BuGn",
    "wind": "Viridis",
    "pressure": "PuOr",
    "fraction": "Cividis",
    "direction": "Turbo",
    "other": "Viridis",
}

#: Ramps that run dark-to-light in a way that reads backwards for the quantity.
#: RdBu is red-to-blue, but warm should mean warm, so temperature inverts it.
INVERTED = frozenset({"temperature"})

#: Classes in a graduated renderer. Enough structure to read a gradient,
#: few enough to tell apart in a legend.
CLASS_COUNT = 7

#: Point size range, in millimetres. Small enough that a dense grid of sites
#: does not merge into a blob; large enough to click.
MIN_POINT_SIZE = 1.6
MAX_POINT_SIZE = 6.0


def ramp_for(parameter: str, units: str = "") -> tuple[str, bool]:
    """``(ramp name, invert)`` for a parameter, falling back to a real ramp."""
    klass = variable_class(parameter, units)
    name = RAMPS.get(klass, "Viridis")
    if name not in QgsStyle.defaultStyle().colorRampNames():
        name = "Viridis"
    return name, klass in INVERTED


def style_point_layer(
    layer: QgsVectorLayer,
    parameter: str,
    units: str = "",
    *,
    value_field: str = "value",
) -> bool:
    """Apply a graduated colour+size renderer keyed on ``value_field``.

    Returns ``False`` when there is nothing to classify -- an all-fill series,
    or a single distinct value -- rather than leaving a half-configured
    renderer. POWER returning fill for a whole series is legal (an ocean cell
    for a land parameter), so this is a normal outcome, not an error.
    """
    values = [
        f[value_field]
        for f in layer.getFeatures()
        if f[value_field] is not None
    ]
    if len({round(float(v), 9) for v in values}) < 2:
        return False

    ramp_name, invert = ramp_for(parameter, units)
    ramp = QgsStyle.defaultStyle().colorRamp(ramp_name)
    if ramp is None:  # pragma: no cover - guarded by ramp_for
        return False
    if invert:
        ramp.invert()

    renderer = QgsGraduatedSymbolRenderer(value_field, [])
    renderer.setClassificationMethod(QgsClassificationEqualInterval())
    renderer.setSourceSymbol(QgsSymbol.defaultSymbol(layer.geometryType()))
    renderer.updateClasses(layer, CLASS_COUNT)
    renderer.updateColorRamp(ramp)
    # Size as well as colour: on a dense point layer the size gradient survives
    # being printed in greyscale and being looked at by someone colour-blind.
    renderer.setSymbolSizes(MIN_POINT_SIZE, MAX_POINT_SIZE)

    layer.setRenderer(renderer)
    layer.triggerRepaint()
    return True


def fixed_range(values: Sequence[float | None]) -> tuple[float, float] | None:
    """The min and max to hold a scale fixed across every frame.

    Computed once over the whole series, then reused for every timestep, so an
    animation shows the data changing rather than the scale chasing it.
    """
    real = [float(v) for v in values if v is not None]
    if not real:
        return None
    low, high = min(real), max(real)
    if low == high:
        # A flat field still needs a non-degenerate range or the renderer
        # classifies everything into one bucket and the layer draws blank.
        pad = abs(low) * 0.01 or 1.0
        return low - pad, high + pad
    return low, high
