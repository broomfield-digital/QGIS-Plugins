# Developing the NASA POWER plugin

Target: **QGIS 4.2.2 "Belém do Pará"** — Qt 6.11.1 / PyQt 6.11.0 / Python 3.12.11 on macOS.

## Install for development

```sh
make link     # symlink nasa_power/ into the QGIS4 profile
make plugins  # ask qgis_process whether it loads
```

`make link` symlinks the working tree into
`~/Library/Application Support/QGIS/QGIS4/profiles/default/python/plugins/nasa_power`, so edits are live —
no copying and no zip. Note the profile directory is **QGIS4**, not QGIS3.

Then in QGIS: *Plugins → Manage and Install Plugins → Installed → NASA POWER*.

`make plugins` prints `*  nasa_power` when the plugin both loads **and** registers a Processing provider.
That single line proves plugin discovery, `classFactory()` and `initProcessing()` all work, without opening
the GUI — the cheapest regression check there is. Set `QGIS_PROFILE` to link into a profile other than
`default`.

## Reload after an edit

In the QGIS Python Console:

```python
import qgis.utils; qgis.utils.reloadPlugin('nasa_power')
```

Reloading deletes the plugin's modules from `sys.modules` and strips its directory from `sys.path`.
Anything still registered with QGIS at that moment outlives the module that created it, which shows up as
duplicate toolbar icons, duplicate Processing toolbox groups, or a segfault from a stale C++ pointer. So
after any change to what the plugin registers, **reload twice and count**:

```python
from qgis.core import QgsApplication
len([p for p in QgsApplication.processingRegistry().providers() if p.id() == 'nasapower'])  # must be 1
```

## Running QGIS's Python from a shell

```sh
./scripts/qgis_python.sh -c "import qgis.core; print(qgis.core.Qgis.QGIS_VERSION)"
```

Three environment facts, each measured on this machine and each load-bearing:

- **`lib-dynload` must be on `PYTHONPATH`.** Without it the stdlib C extensions are missing and `numpy` —
  and therefore `qgis.core` — dies with `PyCapsule_Import could not import module "datetime"`.
- **`PYTHONHOME` must stay unset.** Setting it sends Python to `<prefix>/lib/python3.12`, but this bundle
  keeps its stdlib at `Resources/python3.12`, so the interpreter cannot find `encodings` and aborts.
- **`PROJ_DATA`** must point at `Contents/Resources/qgis/proj` or pyproj warns and CRS handling degrades.

`scripts/qgis_python.sh` sets all three and puts the repo root first on the path, so `import nasa_power`
resolves to the working tree.

**Do not call `QgsApplication.setPrefixPath()` in headless code.** Measured on this build: with no call,
35 providers register (gdal, ogr, mdal, wms …); calling
`setPrefixPath('<app>/Contents/Resources/qgis', True)` drops that to **17** — mdal and wms vanish silently,
because it derives a nonexistent `.../Resources/qgis/Contents/PlugIns/qgis`. `QgsApplication([], False)`
followed by `initQgis()` and nothing else is correct.

## Tests

| Command | What | Needs | Speed |
|---|---|---|---|
| `make test` | ring-1 unit tests | nothing | < 1 s |
| `make guard` | proves ring 1 imports only the stdlib | nothing | < 1 s |
| `make test-qgis` | headless QGIS tests, from fixtures | bundled interpreter | ~10 s |
| `make test-live` | live API contract monitor | network, opt-in | ~30 s |
| `make test-all` | the first three | | |

The runner is stdlib `unittest` — pytest is installed on none of the five Python interpreters on this
machine, and `/usr/bin/python3` is 3.9.6 (too old for ring 1, which needs ≥ 3.10). The suites are plain
`TestCase`s, so pytest would collect them unchanged if it ever appears. `make test` uses miniforge 3.13;
override with `PY=...`.

`make test-live` is a **contract monitor**, not a feature test: one assertion per measured API fact, so the
day POWER changes something the failure names it. It is opt-in (`POWER_LIVE=1`) and never runs on plugin
load. `header.api.version` is logged, not asserted — that drifts by design.

## Architecture: three rings

| Ring | Package | May import |
|---|---|---|
| 1 | `nasa_power/core/` | **stdlib only** |
| 1.5 | `nasa_power/gdalio/` | ring 1 + `osgeo.gdal`, `numpy` |
| 2 | `nasa_power/qgis_bridge/` | rings 1–1.5 + `qgis.core`, `qgis.PyQt` — **no `qgis.gui`, no widgets, no `iface`** |
| 3 | `nasa_power/gui/`, `nasa_power/processing/` | everything below + `qgis.gui` |

Ring 1 holds the entire correctness surface of the POWER API, so it must stay runnable on any Python in
under a second. `make guard` enforces the boundary in a subprocess — a test runner that had already
imported numpy for its own reasons would mask exactly the leak it looks for.

## QGIS 4 / PyQt6 gotchas

- **Import Qt through `qgis.PyQt.*`, never raw `PyQt6`.** The shim smooths over real differences — for
  instance `qgis.PyQt.QtWidgets.QAction` works, though Qt6 moved `QAction` to `QtGui`.
- **Qt enums are fully scoped now.** `Qt.AlignLeft` → `Qt.AlignmentFlag.AlignLeft`,
  `Qt.UTC` → `Qt.TimeSpec.UTC`. `exec_()` and `QRegExp` are gone.
- **`.qrc` / `pyrcc` is dead** — neither the module nor the binary ships in the bundle. Load icons from
  disk with `QIcon(os.path.join(...))` or use `QgsApplication.getThemeIcon(...)`.
- **`supportsQt6` in `metadata.txt` is obsolete.** What matters is `qgisMaximumVersion`: without it a
  plugin is absent from the QGIS 4.2 feed entirely. (That is exactly why the existing
  `nasa_power_downloader` plugin cannot be installed on QGIS 4.)
- **`addSubTask()` must be called before `taskManager().addTask()`**, and the Python `QgsTask` wrapper must
  be kept referenced — C++ owns the task, but a garbage-collected wrapper loses its closures.
- **Avoid `QgsTask.fromFunction`.** It is pure Python, and its `if self.returned_values:` check calls the
  completion callback with different arity when a task returns `0`, `[]` or `{}` — which an all-cached
  fetch does.
- **`Qgis.PlotAxisType` has only `Categorical` and `Interval`** — there is no datetime axis.
- **QtWebEngine is absent from this bundle**, so DataPlotly's plot panel cannot render here. The dock uses
  the native `QgsLineChartPlot`; bundled matplotlib 3.11.1 works under `QT_API='PyQt6'` and is kept behind
  a guarded import as a fallback.

The full catalogue of verified API and QGIS traps, with the file that handles each, is in
[POWER-API-FACTS.md](POWER-API-FACTS.md).
