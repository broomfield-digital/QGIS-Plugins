"""``nasapower:powerregional`` -- fetch a POWER grid over an extent.

One parameter per run, because POWER's regional endpoint serves exactly one
per request and the parents are not co-registered anyway: solar arrives on
CERES's 1 degree grid and meteorology on MERRA-2's 0.5 x 0.625, so two
parameters could not share a raster even if the API allowed it.

The extent is tiled to POWER's 2-10 degree window, mosaicked, and written as an
EPSG:4326 multi-band GeoTIFF with one band per timestep and a readable band
name. A ``.qmd`` sidecar carries the provenance and citation, so the raster can
explain itself away from this session.
"""

from __future__ import annotations

from pathlib import Path

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsProcessingException,
    QgsProcessingParameterExtent,
    QgsProcessingParameterRasterDestination,
    QgsProcessingParameterString,
)

from nasa_power.core.api import fetch_to_cache, plan_requests
from nasa_power.core.citation import build_citation
from nasa_power.core.decode import ResponseFacts
from nasa_power.core.provenance import expected_sources, family_of, grid_for
from nasa_power.core.qa import Level, QaFinding, QaReport, check_valid_range, preflight
from nasa_power.gdalio.mosaic import MosaicError, mosaic
from nasa_power.processing.base import PowerAlgorithm
from nasa_power.qgis_bridge import paths
from nasa_power.qgis_bridge.layer_metadata import build_metadata

P_PARAMETER = "PARAMETER"
P_EXTENT = "EXTENT"
P_OUTPUT = "OUTPUT"


class PowerRegionalAlgorithm(PowerAlgorithm):
    """Fetch a gridded POWER parameter over an extent."""

    def name(self) -> str:
        return "powerregional"

    def displayName(self) -> str:  # noqa: N802 - QGIS API
        return "NASA POWER over an extent"

    def shortHelpString(self) -> str:  # noqa: N802 - QGIS API
        return (
            "Fetches one gridded NASA POWER parameter over an extent and writes "
            "a multi-band EPSG:4326 GeoTIFF, one band per timestep.\n\n"
            "<b>One parameter per run.</b> POWER's gridded endpoint serves "
            "exactly one, and each parameter arrives on its parent's own grid — "
            "CERES solar at 1.0°, MERRA-2 meteorology at 0.5° × 0.625° — so two "
            "could not share a raster.\n\n"
            "<b>No hourly.</b> POWER has no hourly gridded endpoint; it returns "
            "an HTML 404. Use daily, or fetch at points instead.\n\n"
            "The extent is tiled to POWER's 2°–10° window and mosaicked back "
            "together. Monthly requests carry an annual mean alongside the "
            "months; it is dropped rather than rendered as a thirteenth frame."
        )

    def initAlgorithm(self, config=None) -> None:  # noqa: N802 - QGIS API
        self.addParameter(
            QgsProcessingParameterString(
                P_PARAMETER, "POWER parameter", defaultValue="T2M"
            )
        )
        self.addParameter(
            QgsProcessingParameterExtent(P_EXTENT, "Extent (at least 2° on both axes)")
        )
        self.add_common_parameters(gridded=True)
        self.addParameter(
            QgsProcessingParameterRasterDestination(P_OUTPUT, "POWER raster")
        )

    # ------------------------------------------------------------------ #

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        common = self.read_common(parameters, context)
        parameter = self.parameterAsString(parameters, P_PARAMETER, context).strip().upper()
        if not parameter:
            raise QgsProcessingException("Give a POWER parameter, e.g. T2M.")

        # Reprojected to degrees: POWER accepts nothing else, and an extent in
        # a projected CRS would be metres in the millions.
        rectangle = self.parameterAsExtent(
            parameters, P_EXTENT, context, QgsCoordinateReferenceSystem("EPSG:4326")
        )
        bbox = {
            "lat_min": rectangle.yMinimum(),
            "lat_max": rectangle.yMaximum(),
            "lon_min": rectangle.xMinimum(),
            "lon_max": rectangle.xMaximum(),
        }

        report = preflight(
            temporal=common["temporal"],
            mode="regional",
            parameters=[parameter],
            start=common["start"],
            end=common["end"],
            bbox=bbox,
            time_standard=common["time_standard"],
            community=common["community"],
        )
        self.refuse_if_blocked(report, feedback)

        requests = plan_requests(
            common["temporal"],
            "regional",
            [parameter],
            start=common["start"],
            end=common["end"],
            bbox=bbox,
            community=common["community"],
            fmt="NETCDF",
            time_standard=common["time_standard"],
        )
        feedback.pushInfo(f"{len(requests)} tile request(s) planned.")

        cache_dir = paths.resolve_cache_dir()
        fetcher = self.fetcher(feedback)
        tiles = []
        for index, request in enumerate(requests):
            if feedback.isCanceled():
                return {}
            path, was_cached = fetch_to_cache(request, cache_dir, fetcher)
            tiles.append(path)
            feedback.pushInfo(
                f"  tile {index + 1}/{len(requests)}"
                + (" (cached)" if was_cached else "")
            )
            feedback.setProgress(90.0 * (index + 1) / len(requests))

        output = self.parameterAsOutputLayer(parameters, P_OUTPUT, context)
        try:
            # NetCDF carries no `header.sources`, unlike the JSON path, so
            # the parent is predicted from the parameter family and the window
            # -- which is also what surfaces a window straddling the 2001
            # SRB-to-CERES seam.
            result = mosaic(
                tiles,
                output,
                temporal=common["temporal"],
                parameter=parameter,
                sources=expected_sources(
                    family_of(parameter), common["start"], common["end"]
                ),
            )
        except MosaicError as exc:
            raise QgsProcessingException(str(exc)) from exc

        report.extend(
            check_valid_range(
                parameter, result.valid_range, result.out_of_range, requests[0].url
            )
        )
        if result.dropped_bands:
            report.add(
                QaFinding(
                    Level.INFO,
                    "YYYY13_DROPPED",
                    f"Dropped {len(result.dropped_bands)} annual-mean band(s).",
                    detail=(
                        "POWER's monthly axis carries a thirteenth value per year "
                        "that is that year's mean, not a month."
                    ),
                    affected=tuple(str(b) for b in result.dropped_bands),
                )
            )
        self.report(report, feedback)

        # The real report and the sources the mosaic recorded, not an empty
        # one: a sidecar that omits the findings the run just printed is a
        # layer quietly disagreeing with its own provenance.
        self._write_sidecar(output, parameter, common, result, requests, report)
        feedback.pushInfo(
            f"Wrote {result.band_count} band(s), {result.width}x{result.height}, "
            f"EPSG:4326, units {result.units or 'unreported'}."
        )
        return {P_OUTPUT: output}

    # ------------------------------------------------------------------ #

    def _write_sidecar(
        self, output, parameter, common, result, requests, report: QaReport
    ) -> None:
        """Write a ``.qmd`` beside the raster carrying provenance and citation.

        QGIS reads a ``.qmd`` automatically when the raster is loaded, so the
        metadata survives the algorithm ending -- which is the whole point of
        recording it.
        """
        from qgis.core import QgsRasterLayer

        layer = QgsRasterLayer(str(output), Path(output).stem, "gdal")
        if not layer.isValid():
            return
        layer.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))

        grid = grid_for(family_of(parameter))
        facts = ResponseFacts(
            time_standard=common["time_standard"],
            units={parameter: result.units},
            sources=tuple(result.sources),
        )
        layer.setMetadata(
            build_metadata(
                facts,
                report,
                title=Path(output).stem,
                parameters=[parameter],
                temporal=common["temporal"],
                urls=[r.url for r in requests],
                grid_label=grid.label if grid else "",
            )
        )
        layer.saveNamedMetadata(str(Path(output).with_suffix(".qmd")))
