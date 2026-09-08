"""Short names for layers, legends and chart axes.

POWER's ``longname`` values are titles, not labels: "All Sky Surface Shortwave
Downward Irradiance" is 45 characters, and a layer name carries units, temporal
level, time standard and grid besides. In a QGIS layer tree that truncates to
something like "All Sky Surface Shortwave Downwa..." -- which is exactly the
part that does not distinguish it from the clear-sky version sitting under it.

So known parameters get a hand-written terse name, and unknown ones get their
``longname`` mechanically shortened rather than dropped.
"""

from __future__ import annotations

import re

from nasa_power.core.provenance import Family, family_of

#: Longest name that survives the QGIS layer tree at a normal panel width.
MAX_DISPLAY_NAME = 32

#: Terse names for the parameters most likely to be used. Written to be
#: distinguishable at a glance from their near neighbours -- the whole point is
#: that all-sky and clear-sky must not truncate to the same string.
DISPLAY_NAMES: dict[str, str] = {
    # Radiation
    "ALLSKY_SFC_SW_DWN": "Surface Downwelling Shortwave",
    "CLRSKY_SFC_SW_DWN": "Clear-Sky Downwelling Shortwave",
    "ALLSKY_SFC_LW_DWN": "Surface Downwelling Longwave",
    "CLRSKY_SFC_LW_DWN": "Clear-Sky Downwelling Longwave",
    "ALLSKY_SFC_SW_DNI": "Direct Normal Irradiance",
    "CLRSKY_SFC_SW_DNI": "Clear-Sky Direct Normal",
    "ALLSKY_SFC_SW_DIFF": "Diffuse Horizontal Irradiance",
    "ALLSKY_SFC_UV_INDEX": "UV Index",
    "ALLSKY_KT": "Clearness Index",
    "CLRSKY_KT": "Clear-Sky Clearness Index",
    "ALLSKY_NKT": "Normalized Clearness Index",
    "ALLSKY_SRF_ALB": "Surface Albedo",
    "CLRSKY_SRF_ALB": "Clear-Sky Surface Albedo",
    "TOA_SW_DWN": "TOA Downwelling Shortwave",
    "AOD_55": "Aerosol Optical Depth 550nm",
    "CLOUD_OD": "Cloud Optical Depth",
    # Meteorology
    "T2M": "2 m Temperature",
    "T2M_MAX": "2 m Temperature (daily max)",
    "T2M_MIN": "2 m Temperature (daily min)",
    "T2M_RANGE": "2 m Temperature Range",
    "T2MDEW": "2 m Dew Point",
    "T2MWET": "2 m Wet Bulb",
    "TS": "Earth Skin Temperature",
    "RH2M": "2 m Relative Humidity",
    "QV2M": "2 m Specific Humidity",
    "PS": "Surface Pressure",
    "PSC": "Corrected Surface Pressure",
    "WS10M": "10 m Wind Speed",
    "WS50M": "50 m Wind Speed",
    "WD10M": "10 m Wind Direction",
    "WD50M": "50 m Wind Direction",
    "WS10M_MAX": "10 m Wind Speed (daily max)",
    "PRECTOTCORR": "Precipitation (corrected)",
    "PRECTOT": "Precipitation",
    "CLOUD_AMT": "Cloud Amount",
    "TO3": "Total Column Ozone",
    "GWETTOP": "Surface Soil Wetness",
    "GWETROOT": "Root Zone Soil Wetness",
    "GWETPROF": "Profile Soil Moisture",
    "FRSNO": "Snow Cover Fraction",
}

#: Words worth abbreviating when shortening an unknown ``longname``.
_ABBREVIATIONS = (
    ("Shortwave Downward Irradiance", "Downwelling Shortwave"),
    ("Longwave Downward Irradiance", "Downwelling Longwave"),
    ("Downward Irradiance", "Downwelling"),
    ("Temperature at 2 Meters", "2 m Temperature"),
    ("Relative Humidity at 2 Meters", "2 m Relative Humidity"),
    ("Specific Humidity at 2 Meters", "2 m Specific Humidity"),
    ("Wind Speed at ", ""),
    ("Wind Direction at ", ""),
    ("at 2 Meters", "2 m"),
    ("at 10 Meters", "10 m"),
    ("at 50 Meters", "50 m"),
    (" Meters", " m"),
    ("All Sky ", ""),
    ("Clear Sky ", "Clear-Sky "),
    ("Maximum", "max"),
    ("Minimum", "min"),
    ("Corrected", "corr."),
)


def shorten(long_name: str, limit: int = MAX_DISPLAY_NAME) -> str:
    """Squeeze a POWER ``longname`` toward ``limit`` characters.

    Applies known abbreviations first and only truncates as a last resort, so
    "All Sky Surface Shortwave Downward Irradiance" becomes "Surface
    Downwelling Shortwave" rather than a clipped prefix.
    """
    text = " ".join(long_name.split())
    for old, new in _ABBREVIATIONS:
        if len(text) <= limit:
            break
        text = text.replace(old, new)
    text = " ".join(text.split())

    if len(text) <= limit:
        return text
    # Truncate on a word boundary; an ellipsis is more honest than a hard cut.
    clipped = text[: limit - 1].rsplit(" ", 1)[0]
    return f"{clipped}…" if clipped else text[: limit - 1] + "…"


def display_name(parameter: str, long_name: str = "", limit: int = MAX_DISPLAY_NAME) -> str:
    """A terse, distinguishable name for ``parameter``.

    Falls back through: the curated table, a shortened ``longname``, and
    finally the parameter code itself -- which is never empty, so a layer
    always has a name.
    """
    key = parameter.strip().upper()
    known = DISPLAY_NAMES.get(key)
    if known:
        return known
    if long_name:
        return shorten(long_name, limit)
    return key


#: What a variable is, for choosing a colour ramp and a sensible stretch.
VARIABLE_CLASSES = (
    "irradiance",
    "temperature",
    "precipitation",
    "humidity",
    "wind",
    "pressure",
    "fraction",
    "direction",
    "other",
)


def variable_class(parameter: str, units: str = "") -> str:
    """Classify ``parameter`` for styling.

    Drives ramp choice: sequential and warm for irradiance, diverging for
    temperature, blues for precipitation. Uses the units string as a tiebreak
    because it is always present in a response, whereas the dictionary may not
    have been fetched.
    """
    name = parameter.strip().upper()
    unit = units.strip()

    if name.startswith(("WD", "SG_")) or unit in {"Degrees", "degree"}:
        return "direction"
    if unit in {"W m-2", "kW-hr/m^2/day", "MJ/m^2/day", "Wh/m^2"} or name.startswith(
        ("ALLSKY_SFC_SW", "CLRSKY_SFC_SW", "ALLSKY_SFC_LW", "CLRSKY_SFC_LW", "TOA_")
    ):
        return "irradiance"
    if unit in {"C", "K"} or name.startswith(("T2M", "TS", "T10M")):
        return "temperature"
    if unit in {"mm/day", "mm day-1", "mm/hour", "mm hr-1"} or "PRECTOT" in name:
        return "precipitation"
    if name.startswith(("RH", "QV")) or unit in {"%", "g/kg", "kg kg-1"}:
        return "humidity"
    if name.startswith("WS") or unit in {"m/s", "m s-1"}:
        return "wind"
    if unit in {"kPa", "Pa"} or name.startswith("PS"):
        return "pressure"
    if unit in {"1", "dimensionless", "m3 m-3"} or name.startswith(("GWET", "FRS", "CLOUD_AMT")):
        return "fraction"
    return "other"


def layer_name(
    parameter: str,
    units: str,
    temporal: str,
    time_standard: str,
    *,
    long_name: str = "",
    sources: tuple[str, ...] = (),
    grid_label: str = "",
    warn: bool = False,
) -> str:
    """Build the layer name, which is the metadata a user cannot avoid reading.

    Everything load-bearing goes here -- what, in what units, at what cadence,
    on which time standard, from which parent dataset -- because a layer tree
    entry is read every time and a metadata panel is read approximately never.

    A leading warning sign marks a layer carrying an ERROR-level QA finding,
    most importantly one whose window straddles the 2001 SRB-to-CERES seam.
    """
    parts = [display_name(parameter, long_name)]
    if units:
        parts.append(f"[{units}]")
    if temporal:
        parts.append(temporal)
    if time_standard:
        parts.append(time_standard.upper())

    provenance = ""
    if sources or grid_label:
        from nasa_power.core.provenance import describe_sources

        inner = describe_sources(sources) if sources else ""
        if grid_label:
            inner = f"{inner} {grid_label}".strip()
        provenance = f"({inner})"

    name = " ".join(p for p in [*parts, provenance] if p)
    return f"⚠ {name}" if warn else name


def family_label(parameter: str) -> str:
    """Which half of POWER ``parameter`` belongs to, spelled out."""
    return family_of(parameter).label
