# QGIS-Plugins

QGIS plugins by David Fillmore (NSF NCAR).

## `nasa_power` — NASA POWER in QGIS

Fetches [NASA POWER](https://power.larc.nasa.gov/) solar and meteorological data over its REST API and
puts it on the map. No login, no granule staging, and no Python dependencies beyond what QGIS already
ships.

- **Point mode** — pick a location by clicking the map, typing lat/lon, or taking every feature in a point
  layer. The result is a long-form vector layer that the Temporal Controller animates, with a time-series
  chart in the dock.
- **Regional mode** — drag an extent. Requests are tiled to the API's 2–10° window, mosaicked, and written
  as a multi-band EPSG:4326 GeoTIFF that animates band by band.
- **Provenance that is not a guess.** POWER's solar parameters come from CERES SYN1deg — but only from
  2001-01-01; before that they come from GEWEX SRB, and POWER's own CSV and JSON headers disagree about it.
  Its meteorology *is* MERRA-2, on MERRA-2's own grid. Every layer records which, in its name and its
  metadata.

Requires **QGIS 4.0+** (Qt6). Developed against QGIS 4.2.2 on macOS.

### Install

```sh
make link
```

Then enable *NASA POWER* in *Plugins → Manage and Install Plugins → Installed*. See
[docs/DEVELOPING.md](docs/DEVELOPING.md) for reloading, tests, and the QGIS-4 gotchas worth knowing.

### Citing the data

POWER asks that you acknowledge it in any publication using its data. The plugin builds the citation for
you — including the API name and version that actually served your request, which differs per endpoint and
changes over time — and attaches it to each layer's metadata as its rights statement. *Copy citation* in
the dock's QA panel puts it on the clipboard.

The general form POWER asks for:

> These data were obtained from the NASA Langley Research Center (LaRC) POWER Project funded through the
> NASA Earth Science/Applied Science Program.

**POWER is not ground truth.** Its meteorology is reanalysis (MERRA-2/GEOS) and its solar half is
satellite-derived analysis (CERES SYN1deg, GEWEX SRB before 2001). Comparing a model against POWER
meteorology is a comparison against MERRA-2, which may be exactly what you want or may be circular —
the plugin surfaces the provenance so the choice is yours to make knowingly.

### License

GPL-2.0-or-later, matching QGIS. See [LICENSE](LICENSE).
