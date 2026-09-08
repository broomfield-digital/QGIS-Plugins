"""Unit conversion, keyed on the units string POWER actually returned.

POWER's native units depend on **(parameter, temporal level, community)**, not
on the parameter alone. Measured across the three communities for daily
``ALLSKY_SFC_SW_DWN``:

===========  ===================
community    native units
===========  ===================
``RE``       ``kW-hr/m^2/day``
``AG``       ``MJ/m^2/day``
``SB``       ``W m-2``
===========  ===================

...while ``T2M`` is ``C`` in all three, and *hourly* solar is ``Wh/m^2``
rather than any of them. A table keyed on ``(parameter, temporal)`` -- which
is what DAVINCI's ``POWER_CATALOG`` uses -- is therefore wrong the first time
a user picks a different community, and it fails as a units assertion rather
than as a wrong number, which is the good outcome only by luck.

So the rules here are keyed on the **returned units string**. Every response
carries it (``parameters[PARAM].units`` in JSON, the ``units`` band attribute
in NetCDF), so the key is always available and always current. A units string
this table does not know is passed through unconverted and reported as such --
never guessed at.

Two ordering rules that are easy to get backwards:

* **Mask before scaling.** POWER's fill value is ``-999.0``; scaled by the
  daily-irradiance factor it becomes ``-41625.0``, which is not obviously
  wrong on a colour ramp.
* **``Wh/m^2`` to ``W m-2`` is x1, not x3600.** A watt-hour accumulated over
  one hour *is* a watt. This bit the original DAVINCI design, which assumed
  hourly solar arrived as ``W/m^2``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

#: 1 kWh/m^2/day = 1000 Wh spread over 24 h = 41.667 W/m^2.
KWH_M2_DAY_TO_W_M2 = 1000.0 / 24.0
#: 1 MJ/m^2/day = 1e6 J over 86400 s = 11.574 W/m^2.
MJ_M2_DAY_TO_W_M2 = 1.0e6 / 86400.0

#: Length of one timestep, in hours, for the levels that accumulate. Only
#: ``hourly`` reports an accumulated (rather than per-day) radiative unit, so
#: this has one entry; it exists so :data:`UnitRule.per_accumulation` is
#: explicit about what it divides by instead of hiding a bare ``1.0``.
STEP_HOURS = {"hourly": 1.0}


@dataclass(frozen=True)
class UnitRule:
    """How to get one native units string to a canonical one.

    ``canonical_value = native_value * scale + offset``

    When ``per_accumulation`` is set the value is an accumulation over one
    timestep rather than a rate, so ``scale`` is divided by the timestep length
    in hours before use.
    """

    canonical: str
    scale: float = 1.0
    offset: float = 0.0
    per_accumulation: bool = False
    note: str = ""

    @property
    def is_identity(self) -> bool:
        """Whether this rule only relabels the units."""
        return self.scale == 1.0 and self.offset == 0.0 and not self.per_accumulation


#: Native units string -> conversion. Every key was observed in a live response
#: or in a parameter dictionary (all three communities, daily) on 2026-09-07.
UNIT_RULES: dict[str, UnitRule] = {
    # -- temperature ---------------------------------------------------- #
    "C": UnitRule("K", offset=273.15),
    # -- radiative flux, as a per-day rate ------------------------------- #
    "kW-hr/m^2/day": UnitRule("W m-2", scale=KWH_M2_DAY_TO_W_M2, note="community RE"),
    "MJ/m^2/day": UnitRule("W m-2", scale=MJ_M2_DAY_TO_W_M2, note="community AG"),
    "W m-2": UnitRule("W m-2", note="community SB; already canonical"),
    # -- radiative flux, accumulated over one timestep ------------------- #
    "Wh/m^2": UnitRule(
        "W m-2",
        per_accumulation=True,
        note="hourly; a watt-hour over one hour is a watt, so this is x1",
    ),
    # -- pressure ------------------------------------------------------- #
    "kPa": UnitRule("Pa", scale=1000.0),
    # -- wind ----------------------------------------------------------- #
    "m/s": UnitRule("m s-1"),
    # -- humidity and moisture ------------------------------------------ #
    "%": UnitRule("%"),
    "g/kg": UnitRule("kg kg-1", scale=1.0e-3),
    "m3 m-3": UnitRule("m3 m-3"),
    "kg m-3": UnitRule("kg m-3"),
    "kg m-2": UnitRule("kg m-2"),
    # -- precipitation -------------------------------------------------- #
    "mm/day": UnitRule("mm day-1"),
    "mm/hour": UnitRule("mm hr-1"),
    # -- lengths -------------------------------------------------------- #
    "m": UnitRule("m"),
    "cm": UnitRule("m", scale=0.01),
    # -- angles, counts, indices ---------------------------------------- #
    "Degrees": UnitRule("degree"),
    "Days": UnitRule("day"),
    "count": UnitRule("1"),
    "dimensionless": UnitRule("1"),
    "1": UnitRule("1"),
    "Dobsons": UnitRule("DU"),
    # -- deliberately not converted ------------------------------------- #
    "degree-day-c": UnitRule(
        "degree-day-c",
        note="An integrated index, not a physical unit; SI has no equivalent.",
    ),
    "W m-2 x 40": UnitRule(
        "UV index",
        note=(
            "ALLSKY_SFC_UV_INDEX. The served value IS the index; the string "
            "describes how the index relates to erythemal irradiance. Dividing "
            "by 40 would produce W m-2 but would no longer be the index the "
            "user asked for, so the value is left alone."
        ),
    ),
}


def rule_for(native_units: str) -> UnitRule | None:
    """Return the rule for ``native_units``, or ``None`` if it is unknown.

    Unknown is a normal outcome: POWER serves 150+ parameters per temporal
    level and adds more. Callers pass the value through unchanged and say so.
    """
    return UNIT_RULES.get(native_units.strip())


def canonical_units(native_units: str) -> str:
    """The units a converted value carries. Unknown units are returned as-is."""
    rule = rule_for(native_units)
    return rule.canonical if rule is not None else native_units


def effective_scale(rule: UnitRule, temporal: str) -> float:
    """``rule``'s scale factor at ``temporal``.

    Raises
    ------
    ValueError
        If the rule accumulates over a timestep but the temporal level has no
        defined step length. That is not a case to guess at: silently choosing
        a step would produce a plausible number that is wrong by the ratio of
        the real step to the guessed one.
    """
    if not rule.per_accumulation:
        return rule.scale
    try:
        hours = STEP_HOURS[temporal]
    except KeyError:
        raise ValueError(
            f"{rule.canonical!r} conversion needs a timestep length, but "
            f"temporal={temporal!r} has none defined. Known: "
            f"{', '.join(sorted(STEP_HOURS))}."
        ) from None
    return rule.scale / hours


def to_canonical(
    value: float | None,
    native_units: str,
    temporal: str,
    *,
    fill_value: float | None = None,
) -> float | None:
    """Convert one value, masking the fill sentinel first.

    Parameters
    ----------
    value
        The native value, or ``None`` for already-masked data.
    native_units
        The units string from the response -- never a hardcoded guess.
    temporal
        ``hourly``, ``daily``, ``monthly`` or ``climatology``.
    fill_value
        The response's own ``header.fill_value``. Matched exactly: POWER
        writes ``-999.0`` as a literal, not as a near miss.

    Returns
    -------
    float or None
        ``None`` where the input was fill or missing.
    """
    if value is None:
        return None
    if fill_value is not None and value == fill_value:
        return None

    rule = rule_for(native_units)
    if rule is None:
        return value
    return value * effective_scale(rule, temporal) + rule.offset


def convert_series(
    values: Iterable[float | None],
    native_units: str,
    temporal: str,
    *,
    fill_value: float | None = None,
) -> list[float | None]:
    """:func:`to_canonical` over a sequence, resolving the rule once."""
    rule = rule_for(native_units)
    if rule is None:
        return [None if (fill_value is not None and v == fill_value) else v for v in values]

    scale = effective_scale(rule, temporal)
    offset = rule.offset
    out: list[float | None] = []
    for value in values:
        if value is None or (fill_value is not None and value == fill_value):
            out.append(None)
        else:
            out.append(value * scale + offset)
    return out


def describe_conversion(native_units: str, temporal: str) -> str:
    """A one-line record of what was applied, for the layer's history.

    Written into ``QgsLayerMetadata`` so a layer can always answer "what was
    done to these numbers", including when the answer is "nothing".
    """
    rule = rule_for(native_units)
    if rule is None:
        return f"units {native_units!r} not recognised; values left unconverted"
    if rule.is_identity:
        return f"units {native_units!r} -> {rule.canonical!r} (relabelled; no arithmetic)"

    scale = effective_scale(rule, temporal)
    parts = []
    if scale != 1.0:
        parts.append(f"x {scale:.6g}")
    if rule.offset:
        parts.append(f"{rule.offset:+.6g}")
    applied = " ".join(parts) or "no arithmetic"
    return f"units {native_units!r} -> {rule.canonical!r} ({applied})"


def known_units() -> Sequence[str]:
    """Every native units string this module can convert."""
    return tuple(sorted(UNIT_RULES))
