"""Decode a POWER NetCDF time axis into real datetimes.

There is no single rule, which is the whole difficulty. Measured across live
responses:

============================  ===============================  ==============
response                      ``time#units``                   axis values
============================  ===============================  ==============
daily T2M (MERRA-2)           ``days since 1980-12-31``        15737, 15738…
daily solar 2024 (SYN1deg)    ``days since 2000-12-31``        8432, 8433…
daily solar 1985 (SRB era)    ``days since 1984-01-01``        …
hourly                        ``hours since 1980-12-31``       …
**monthly**                   **absent entirely**              **202001…202013**
============================  ===============================  ==============

So the epoch varies **per parameter and per era**, and the monthly case has no
epoch at all -- its raw values simply *are* ``YYYYMM``. Two consequences:

* Never hardcode an epoch, and never merge two responses on raw index. Two
  tiles of the same parameter share an epoch; a solar tile and a met tile do
  not, and their band 1 values differ by thousands.
* The monthly axis carries ``YYYY13`` -- that year's **annual mean**, not a
  thirteenth month. A 2020-2021 request returns 26 bands for 24 months, and a
  naive pass-through renders two annual means as if they were data.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from nasa_power.core.timeaxis import decode_monthly_stamp

#: ``<unit> since <ISO-ish date>``. POWER writes the date several ways
#: (``1980-12-31``, ``1980-12-31 00:00:00``, with or without a ``T``), so the
#: tail is parsed leniently rather than by one strptime format.
_UNITS_RE = re.compile(
    r"^\s*(?P<unit>\w+)\s+since\s+(?P<epoch>.+?)\s*$", re.IGNORECASE
)

_UNIT_DELTA = {
    "days": timedelta(days=1),
    "day": timedelta(days=1),
    "hours": timedelta(hours=1),
    "hour": timedelta(hours=1),
    "minutes": timedelta(minutes=1),
    "seconds": timedelta(seconds=1),
}


class CfTimeError(ValueError):
    """A ``time#units`` string that does not parse as a CF epoch."""


def parse_units(units: str) -> tuple[timedelta, datetime]:
    """Split ``"days since 1980-12-31"`` into a step and an epoch."""
    match = _UNITS_RE.match(units or "")
    if not match:
        raise CfTimeError(f"Not a CF time units string: {units!r}")

    unit = match.group("unit").lower()
    if unit not in _UNIT_DELTA:
        raise CfTimeError(
            f"Unsupported CF time unit {unit!r} in {units!r}. "
            f"Known: {', '.join(sorted(_UNIT_DELTA))}."
        )

    epoch_text = match.group("epoch").strip()
    # Trailing timezone designators appear occasionally; the axis is UTC.
    # Stripped *before* the ISO separator is normalised: a blanket
    # ``"T" -> " "`` replacement turns ``UTC`` into ``U C``, which leaves a
    # designator this pattern can no longer see and makes its own ``UTC``
    # branch unreachable.
    epoch_text = re.sub(r"\s*(UTC|Z|\+00:?00)$", "", epoch_text, flags=re.IGNORECASE)
    # Only the separator between the date and the time, never a letter inside
    # a word.
    epoch_text = re.sub(r"(?<=\d)[Tt](?=\d)", " ", epoch_text).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            epoch = datetime.strptime(epoch_text, fmt)
            break
        except ValueError:
            continue
    else:
        raise CfTimeError(f"Could not read the epoch out of {units!r}")

    return _UNIT_DELTA[unit], epoch.replace(tzinfo=timezone.utc)


def decode_axis(
    values, units: str | None, temporal: str
) -> list[tuple[datetime, datetime] | None]:
    """Decode raw axis values into half-open ``[start, end)`` UTC intervals.

    ``None`` marks a value that is not a timestep -- the ``YYYY13`` annual
    means. Callers drop those bands rather than rendering them.

    Falls through to raw ``YYYYMM`` when ``units`` is missing, which is the
    monthly case and is not an error.
    """
    if temporal == "monthly" or not units:
        return [decode_monthly_stamp(int(round(float(v)))) for v in values]

    step, epoch = parse_units(units)
    out: list[tuple[datetime, datetime] | None] = []
    starts = [epoch + step * float(v) for v in values]

    for index, start in enumerate(starts):
        if index + 1 < len(starts):
            end = starts[index + 1]
        else:
            # The last band has no successor to bound it. Reuse the previous
            # spacing so the final frame is the same length as the others; with
            # a single band, fall back to the axis unit itself.
            end = start + (starts[-1] - starts[-2] if len(starts) > 1 else step)
        out.append((start, end))
    return out


def units_of(dataset) -> str | None:
    """The ``time#units`` string, or ``None`` if the dataset has none.

    ``None`` is a real answer: the monthly endpoint's ``time`` variable has no
    ``units`` attribute at all.
    """
    return dataset.GetMetadata().get("time#units")


def band_times(
    dataset, temporal: str, units: str | None = None
) -> list[tuple[datetime, datetime] | None]:
    """Decode every band of an open GDAL dataset.

    ``units`` must be passed when ``dataset`` is a **VRT**: ``gdal.BuildVRT``
    carries per-band ``NETCDF_DIM_time`` through but drops dataset-level
    metadata, so the VRT has no ``time#units``. Reading it from the VRT would
    make every daily response look like the units-less monthly case and try to
    parse ``15737`` as ``YYYYMM``. Take it from a source tile instead.
    """
    if units is None:
        units = units_of(dataset)
    raw = []
    for index in range(1, dataset.RasterCount + 1):
        value = dataset.GetRasterBand(index).GetMetadataItem("NETCDF_DIM_time")
        if value is None:
            raise CfTimeError(
                f"Band {index} has no NETCDF_DIM_time; this does not look like a "
                f"POWER regional NetCDF."
            )
        raw.append(value)
    return decode_axis(raw, units, temporal)


def keep_bands(
    times: list[tuple[datetime, datetime] | None]
) -> tuple[list[int], list[tuple[datetime, datetime]]]:
    """Split decoded times into the bands to keep and their intervals.

    Band numbers are **1-based**, matching GDAL and QGIS.
    """
    keep: list[int] = []
    intervals: list[tuple[datetime, datetime]] = []
    for index, span in enumerate(times, start=1):
        if span is None:
            continue
        keep.append(index)
        intervals.append(span)
    return keep, intervals
