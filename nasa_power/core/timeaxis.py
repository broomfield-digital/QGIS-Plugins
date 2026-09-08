"""Decode POWER's time keys into half-open UTC intervals.

Four key shapes, one per temporal level, measured against live responses:

============  ===============  ======================================
temporal      key              also present
============  ===============  ======================================
hourly        ``YYYYMMDDHH``   --
daily         ``YYYYMMDD``     --
monthly       ``YYYYMM``       **``YYYY13``** -- the year's annual mean
climatology   ``JAN``...``DEC``  **``ANN``** -- the annual mean
============  ===============  ======================================

The last column is the trap. ``YYYY13`` is not a thirteenth month and ``ANN``
is not a thirteenth climatological period: both are annual means sitting in the
same dictionary as the monthly values. A 2020-2021 monthly request returns
**26** keys for 24 months. Left in, they are a spurious point every thirteenth
step, contaminating any series, any statistic, and -- on the raster path --
rendering as if they were real months. Both are dropped here, once, so no
downstream code has to remember.

Everything returned is a **half-open** interval ``[start, end)`` in UTC. Half
open matters for the QGIS temporal controller: with closed intervals two
adjacent days both match an instant on their shared boundary, and the animation
flickers between them.

Why UTC specifically: POWER's own default time standard is Local Solar Time,
which at Boulder puts the solar peak seven hours away from where a UTC-based
project expects it. The client always requests UTC, and the reported standard
is carried alongside the data so a mismatch is visible rather than silent.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta, timezone

#: The annual-mean pseudo-month on the monthly endpoint.
MONTHLY_ANNUAL_MEAN_MONTH = 13
#: The annual-mean key on the climatology endpoint.
CLIMATOLOGY_ANNUAL_MEAN_KEY = "ANN"

#: Climatology keys, in order. POWER returns them as three-letter uppercase
#: month abbreviations plus ``ANN``.
CLIMATOLOGY_MONTHS = (
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
)

_HOURLY_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(\d{2})$")
_DAILY_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_MONTHLY_RE = re.compile(r"^(\d{4})(\d{2})$")

TEMPORAL_LEVELS = ("hourly", "daily", "monthly", "climatology")


class TimeKeyError(ValueError):
    """A key that does not match any known shape for its temporal level."""


def _utc(year: int, month: int = 1, day: int = 1, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


def decode_time_key(
    key: str,
    temporal: str,
    *,
    climatology_year: int = 2001,
) -> tuple[datetime, datetime] | None:
    """Decode one key into ``(start, end)`` UTC, or ``None`` to drop it.

    ``None`` means "this key is an annual mean, not a timestep" -- the
    ``YYYY13`` and ``ANN`` cases. Callers drop those rather than plotting them.

    Parameters
    ----------
    climatology_year
        Climatology has no year of its own, so its months are stamped into a
        nominal one purely to give the temporal controller something to order.
        Defaults to 2001 -- a non-leap year, so February is unambiguous.

    Raises
    ------
    TimeKeyError
        If the key does not match the shape its temporal level uses. This is a
        parse failure, not a drop: it means the response schema changed.
    """
    key = key.strip()

    if temporal == "hourly":
        match = _HOURLY_RE.match(key)
        if not match:
            raise TimeKeyError(f"Not an hourly key (expected YYYYMMDDHH): {key!r}")
        year, month, day, hour = (int(g) for g in match.groups())
        start = _utc(year, month, day, hour)
        return start, start + timedelta(hours=1)

    if temporal == "daily":
        match = _DAILY_RE.match(key)
        if not match:
            raise TimeKeyError(f"Not a daily key (expected YYYYMMDD): {key!r}")
        year, month, day = (int(g) for g in match.groups())
        start = _utc(year, month, day)
        return start, start + timedelta(days=1)

    if temporal == "monthly":
        match = _MONTHLY_RE.match(key)
        if not match:
            raise TimeKeyError(f"Not a monthly key (expected YYYYMM): {key!r}")
        year, month = (int(g) for g in match.groups())
        if month == MONTHLY_ANNUAL_MEAN_MONTH:
            # YYYY13 is that year's annual mean, not a month.
            return None
        if not 1 <= month <= 12:
            raise TimeKeyError(f"Month out of range in monthly key {key!r}")
        start = _utc(year, month)
        end = _utc(year + 1, 1) if month == 12 else _utc(year, month + 1)
        return start, end

    if temporal == "climatology":
        upper = key.upper()
        if upper == CLIMATOLOGY_ANNUAL_MEAN_KEY:
            return None
        try:
            month = CLIMATOLOGY_MONTHS.index(upper) + 1
        except ValueError:
            raise TimeKeyError(
                f"Not a climatology key (expected one of "
                f"{', '.join(CLIMATOLOGY_MONTHS)} or {CLIMATOLOGY_ANNUAL_MEAN_KEY}): {key!r}"
            ) from None
        start = _utc(climatology_year, month)
        end = (
            _utc(climatology_year + 1, 1)
            if month == 12
            else _utc(climatology_year, month + 1)
        )
        return start, end

    raise TimeKeyError(
        f"Unknown temporal level {temporal!r}. Known: {', '.join(TEMPORAL_LEVELS)}"
    )


def decode_time_keys(
    keys: list[str] | tuple[str, ...],
    temporal: str,
    *,
    climatology_year: int = 2001,
) -> list[tuple[str, datetime, datetime]]:
    """Decode and sort many keys, dropping annual means.

    Returns ``(key, start, end)`` triples ordered by ``start``. The original key
    is kept so a value can be looked back up in the response dictionary without
    re-deriving it.
    """
    decoded: list[tuple[str, datetime, datetime]] = []
    for key in keys:
        span = decode_time_key(key, temporal, climatology_year=climatology_year)
        if span is None:
            continue
        decoded.append((key, span[0], span[1]))
    decoded.sort(key=lambda item: item[1])
    return decoded


def dropped_keys(keys: list[str] | tuple[str, ...], temporal: str) -> list[str]:
    """The keys :func:`decode_time_keys` would discard as annual means.

    Reported as a QA finding rather than dropped in silence -- a user who asked
    for 2020-2021 and gets 24 values from a 26-key response deserves to be told
    which two went and why.
    """
    return [
        key for key in keys
        if decode_time_key(key, temporal) is None
    ]


def decode_monthly_stamp(stamp: int | str) -> tuple[datetime, datetime] | None:
    """Decode a raw ``YYYYMM`` integer from a NetCDF time axis.

    The monthly NetCDF is the awkward case: its ``time`` variable has **no**
    ``units`` attribute at all, so there is no CF epoch to decode against --
    the raw values simply *are* ``YYYYMM``. Verified on a 2020-2021 regional
    response: ``NETCDF_DIM_time`` runs ``202001 ... 202013, 202101 ... 202113``
    across 26 bands. Returns ``None`` for the two annual means.
    """
    text = str(stamp).strip()
    match = _MONTHLY_RE.match(text)
    if not match:
        raise TimeKeyError(f"Not a YYYYMM stamp: {stamp!r}")
    return decode_time_key(text, "monthly")


def month_end(year: int, month: int) -> date:
    """Last calendar day of ``month``. Used to render a monthly request window."""
    return date(year, month, calendar.monthrange(year, month)[1])
