"""Stitch POWER regional tiles into one georeferenced, time-stamped GeoTIFF.

Four things this has to get right, each measured:

* **The duplicate edge row.** POWER's bounding box is inclusive at both ends,
  so whether two adjacent tiles share a row depends on the parent grid.
  MERRA-2's 0.5 degree grid has a node exactly on integer degrees, so tiles
  meeting at latitude 40 both return it (5 + 5 rows must become 9, not 10);
  CERES's 1 degree grid is centred on half-degrees, so an integer boundary
  falls between cells and nothing is shared (2 + 2 becomes 4). ``BuildVRT``
  resolves this geometrically -- the tiles are pixel-aligned to the same grid,
  so the shared row lands on the same VRT pixel and the values are identical.
* **The missing CRS.** Every POWER regional NetCDF has ``GetProjection() ==
  ''``. The layer still loads and draws, so in a WGS84 project the omission is
  invisible; in a projected one it is gross mis-registration. EPSG:4326 is
  written into the output here *and* set on the layer.
* **``YYYY13``.** A two-year monthly response has 26 bands, two of which are
  annual means. They are dropped here, once, so nothing downstream renders
  them as months.
* **Cross-family mosaicking is refused.** Solar (1 degree) and meteorology
  (0.5 x 0.625) are not co-registered, so a VRT of the two would be
  misaligned. That is a hard error, not a warning.

The output is written band by band rather than by ``gdal.Translate`` of the
VRT. Translate carries ``NETCDF_DIM_time`` through, so QGIS band names come out
as ``Band 1: time=15737 (days since 1980-12-31)``, and the NaN nodata trips
``TIFFTAG_GDAL_NODATA``. Ten explicit lines buy readable band names, a stamped
CRS, and somewhere to drop the annual-mean bands.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Sequence

import numpy as np
from osgeo import gdal, osr

from nasa_power.core.provenance import Family, family_of
from nasa_power.gdalio.cf import band_times, keep_bands, units_of

gdal.UseExceptions()

#: POWER's NetCDF uses NaN, unlike its JSON, which uses -999.0.
NODATA = float("nan")

CREATION_OPTIONS = ["COMPRESS=DEFLATE", "TILED=YES", "PREDICTOR=3"]


class MosaicError(RuntimeError):
    """Tiles that cannot be combined into one raster."""


@dataclass
class MosaicResult:
    """The written raster and what had to be said about it."""

    path: Path
    band_count: int
    width: int
    height: int
    intervals: list[tuple[datetime, datetime]] = field(default_factory=list)
    dropped_bands: list[int] = field(default_factory=list)
    units: str = ""
    parameter: str = ""
    sources: tuple[str, ...] = ()
    #: ``valid_min``/``valid_max`` as the source declared them, in native units.
    #: Present only in the NetCDF -- the JSON responses carry no such range --
    #: so this is the one path that can check values against the API's own
    #: idea of plausible.
    valid_range: tuple[float, float] | None = None
    #: How many written pixels fell outside that range. Non-zero points at a
    #: decoding or scaling error rather than at unusual weather.
    out_of_range: int = 0


def _open(path: str | Path) -> gdal.Dataset:
    dataset = gdal.Open(str(path))
    if dataset is None:  # pragma: no cover - UseExceptions raises first
        raise MosaicError(f"GDAL could not open {path}")
    return dataset


def _grid_signature(dataset: gdal.Dataset) -> tuple[float, float]:
    """Pixel size, rounded, as a co-registration key."""
    transform = dataset.GetGeoTransform()
    return round(transform[1], 9), round(abs(transform[5]), 9)


def check_same_family(paths: Sequence[str | Path], parameter: str = "") -> None:
    """Refuse tiles that are not on one grid.

    Two failure modes, both real: mixing parameters from different parents
    (solar 1 degree with meteorology 0.5 x 0.625), and a POWER change that
    silently alters a grid. Either way a mosaic would be misaligned, and a
    misaligned raster looks plausible.
    """
    signatures = {}
    for path in paths:
        dataset = _open(path)
        signatures.setdefault(_grid_signature(dataset), []).append(str(path))
        dataset = None

    if len(signatures) > 1:
        detail = "; ".join(
            f"{sig[0]}x{sig[1]} deg: {', '.join(os.path.basename(p) for p in files)}"
            for sig, files in signatures.items()
        )
        raise MosaicError(
            "CROSS_FAMILY_MOSAIC: these tiles are on different grids and are not "
            "co-registered, so mosaicking them would misalign the result. POWER "
            "serves each parameter on its parent's native grid -- CERES solar at "
            f"1.0 deg, MERRA-2 meteorology at 0.5 x 0.625 deg. Found {detail}. "
            "Build one raster per parameter."
        )

    if parameter:
        family = family_of(parameter)
        if family is Family.SOLAR_GEOMETRY:
            raise MosaicError(
                f"{parameter} is computed from geometry and has no grid to mosaic."
            )


def mosaic(
    tile_paths: Sequence[str | Path],
    output_path: str | Path,
    *,
    temporal: str,
    parameter: str = "",
    sources: Sequence[str] = (),
    vrt_path: str | Path | None = None,
) -> MosaicResult:
    """Combine ``tile_paths`` into one EPSG:4326 GeoTIFF with stamped bands."""
    if not tile_paths:
        raise MosaicError("No tiles to mosaic.")

    paths = [str(p) for p in tile_paths]
    check_same_family(paths, parameter)

    # From a source tile, not the VRT: BuildVRT keeps per-band NETCDF_DIM_time
    # but drops dataset metadata, so the VRT has no time#units and every daily
    # response would be mistaken for the units-less monthly case.
    source = _open(paths[0])
    time_units = units_of(source)
    # BuildVRT drops the band unit type as well, so "C" would become "" and the
    # output raster would carry no units at all -- which is how a temperature
    # map ends up unlabelled and a reader guesses.
    source_units = source.GetRasterBand(1).GetUnitType() or ""
    band_md = source.GetRasterBand(1).GetMetadata()
    try:
        valid_range = (
            float(band_md["valid_min"]),
            float(band_md["valid_max"]),
        )
    except (KeyError, TypeError, ValueError):
        valid_range = None
    source = None

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    vrt_target = str(vrt_path) if vrt_path else str(output_path.with_suffix(".vrt"))

    # What keeps a nodata pixel in one overlapping tile from punching a hole
    # through a valid neighbour is the *source* tile's own declared nodata,
    # which every POWER NetCDF has (NaN) -- measured: with the tiles declaring
    # it the merged row is identical in either order and with or without this
    # argument, and with sources that declare none the hole appears and this
    # argument does not close it. VRTNodata stamps the same sentinel on the VRT
    # band so what the VRT reports matches what the tiles carry.
    vrt = gdal.BuildVRT(vrt_target, paths, VRTNodata="nan")
    if vrt is None:
        raise MosaicError("GDAL could not build a VRT from the tiles.")
    vrt.FlushCache()

    times = band_times(vrt, temporal, time_units)
    keep, intervals = keep_bands(times)
    dropped = [i for i, span in enumerate(times, start=1) if span is None]
    if not keep:
        raise MosaicError(
            "Every band in the response is an annual mean; there are no timesteps "
            "to render."
        )

    units = source_units
    width, height = vrt.RasterXSize, vrt.RasterYSize

    driver = gdal.GetDriverByName("GTiff")
    out = driver.Create(
        str(output_path), width, height, len(keep), gdal.GDT_Float32, CREATION_OPTIONS
    )
    out.SetGeoTransform(vrt.GetGeoTransform())

    # The one thing POWER never provides. Without it QGIS prompts the user, or
    # silently mis-places the layer in a projected project.
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    out.SetProjection(srs.ExportToWkt())

    out.SetMetadata(
        {
            "POWER_PARAMETER": parameter,
            "POWER_UNITS": units,
            "POWER_SOURCES": ",".join(sources),
            "POWER_TEMPORAL": temporal,
            "POWER_TILES": str(len(paths)),
        }
    )

    out_of_range = 0
    for out_index, (source_index, (start, end)) in enumerate(
        zip(keep, intervals), start=1
    ):
        data = vrt.GetRasterBand(source_index).ReadAsArray()
        if valid_range is not None:
            finite = np.asarray(data, dtype="float64")
            finite = finite[np.isfinite(finite)]
            out_of_range += int(
                ((finite < valid_range[0]) | (finite > valid_range[1])).sum()
            )
        band = out.GetRasterBand(out_index)
        band.WriteArray(np.asarray(data, dtype="float32"))
        band.SetNoDataValue(NODATA)
        band.SetUnitType(units)
        # Readable in the QGIS band list, unlike the raw axis value Translate
        # would have carried through.
        label = start.strftime("%Y-%m-%d" if temporal != "hourly" else "%Y-%m-%d %H:%M")
        band.SetDescription(f"{parameter} {label}".strip())
        band.SetMetadataItem("POWER_DATETIME_START", start.isoformat())
        band.SetMetadataItem("POWER_DATETIME_END", end.isoformat())

    out.FlushCache()
    out = None
    vrt = None

    if vrt_path is None:
        # The VRT was scaffolding and points at cache files that may be
        # cleaned; leaving it behind invites someone to load a layer that
        # breaks later.
        Path(vrt_target).unlink(missing_ok=True)

    return MosaicResult(
        path=output_path,
        band_count=len(keep),
        width=width,
        height=height,
        intervals=intervals,
        dropped_bands=dropped,
        units=units,
        parameter=parameter,
        sources=tuple(sources),
        valid_range=valid_range,
        out_of_range=out_of_range,
    )


def band_statistics(path: str | Path) -> tuple[float, float] | None:
    """Min and max across **every** band, for a stretch that does not pulse.

    Per-band limits make an animation shimmer: the colours move because the
    scale moved, not because the weather did. Returns ``None`` when every band
    is nodata, which is a legal POWER response (an ocean-only box for a land
    parameter).
    """
    dataset = _open(path)
    low = high = None
    for index in range(1, dataset.RasterCount + 1):
        data = dataset.GetRasterBand(index).ReadAsArray()
        finite = np.asarray(data, dtype="float64")
        finite = finite[np.isfinite(finite)]
        if finite.size == 0:
            continue
        band_low, band_high = float(finite.min()), float(finite.max())
        low = band_low if low is None else min(low, band_low)
        high = band_high if high is None else max(high, band_high)
    dataset = None
    if low is None:
        return None
    return low, high
