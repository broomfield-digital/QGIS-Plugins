# NASA POWER API facts — measured, not assumed

Every row here was verified against the live API or against this machine's QGIS build. Dates say when.
Where a fact is pinned by a committed fixture or a test, the table names it — a fact with no test is a
fact that will quietly stop being true.

**API versions seen:** v2.9.4 / v2.9.5 (2026-07-15, via the DAVINCI project) → v2.9.7 (daily) and
v2.9.8 (monthly) on 2026-09-07. The version differs **per endpoint** and drifts, so nothing here
hardcodes it; `core/citation.py` reads `header.api.version` from each response.

---

## A. Defects found in the DAVINCI client while porting

The client came from `~/NSF-NCAR/DAVINCI/davinci_monet/io/download/power.py`, which was itself
live-verified. These are the five things that still needed changing for this use.

| # | Defect | Evidence | Fix |
|---|---|---|---|
| A-1 | `build_power_url` enforced the 2° regional **minimum** but never the 10° **maximum** — `REGIONAL_MAX_SPAN_DEGREES` was referenced only by the tiler, so a hand-built 12° request reached the API | 12° → 422 `"Please provide a maximum of 10 degree range in latitude."` | `_check_bbox` now checks both. The comparison is a strict `>` so the tiler's own exactly-10.0° tiles still pass |
| A-2 | `PowerRequest` had no `fmt` field and `cache_path` hardcoded `.nc` (`power.py:364`) | `name = f"{label}-{start}-{end}-{digest}.nc"` | `PowerRequest.fmt` + `PowerRequest.suffix`. Harmless in DAVINCI, which only ever asked for NetCDF; here point mode is JSON and a `.nc` name would be a lie on disk |
| A-3 | `_split_span`'s docstring claimed the last two tiles are "rebalanced to share the tail evenly"; the code does a uniform `span/ceil(span/10)` split | read at `power.py:219–238` | Docstring corrected — the behaviour was right, the comment was not |
| A-4 | Coupled to DAVINCI: `DataNotFoundError`, a hardcoded `davinci-stage-power` command string, and `~/.cache/davinci/power` | `power.py:34, 484, 503` | Local `PowerCacheMiss`; the message points at the dock; cache dir injected |
| A-5 | No `User-Agent`, no delay between requests | `stage_power` fired back to back | Named UA in `core/fetcher.py`; `INTER_REQUEST_DELAY = 0.25`; at most 4 concurrent |

---

## B. POWER API traps

Verified 2026-09-07 unless noted.

### Endpoints and limits

| # | Trap | Handled in |
|---|---|---|
| P-1 | **`hourly/regional` does not exist.** It returns a **19 456-byte `text/html`** page, not a JSON API error — so without a local refusal the user sees a wall of markup with no hint the combination is unsupported. Every other temporal × mode combination returns 200. | `api.build_power_url` raises; `qa.HOURLY_REGIONAL` is BLOCKING; the extent control greys out at hourly. Fixture `error_404_hourly_regional.html` |
| P-2 | Point takes **≤ 20** parameters (21 → 422); regional takes **exactly 1** (2 → 422 `"A maximum of 1 parameters are can currently be requested"`, sic) | `build_power_url` + `plan_requests` chunking; `qa.PARAM_COUNT` / `PARAM_CHUNKED` |
| P-3 | A regional bbox must span **≥ 2.0°** *and* **≤ 10.0°** on both axes. 1° → 422 `"Please provide at least a 2 degree range in latitude; otherwise use the point endpoint."`; 12° → 422. Exactly 2.0 and exactly 10.0 are both accepted. **The binding constraint is the minimum**, which the original design missed. | `api._check_bbox`; `qa.BBOX_SPAN`; fixture `error_422_bbox.json` |
| P-4 | Greedy 10° chunking of a 21° span gives 10 + 10 + **1**, and that 1° sliver is itself a 422 | `api._split_span` uses an even split `span/ceil(span/10)`, which cannot produce a piece under 5° for any span over 10° |
| P-17 | No published rate limit and no `RateLimit-*` headers, but POWER's docs warn that a client which "persists in requesting the same relative location" **may be blocked** | The on-disk cache is a *correctness* feature, not a speed one: a hit never issues a request, and that is unit-tested with a fetcher that raises if called. Plus ≤ 4 concurrent, 0.25 s apart, and force-refetch off by default |

### Dates and time

| # | Trap | Handled in |
|---|---|---|
| P-6 | Monthly dates are **year only** (`start=2020`). `start=20200101` → 422 `"Please provide a correct start date formatting."` | `api.format_power_date` |
| P-7 | Monthly keys include **`YYYY13`, which is that year's annual mean**, not a thirteenth month. A 2020–2021 request returns **26** keys for 24 months | `timeaxis.decode_time_key` returns `None`; `qa.YYYY13_DROPPED` reports what went. Fixture `point_monthly_yyyy13.json` |
| P-8 | **The same trap on the raster path, and worse.** A 2-year monthly regional NetCDF has **26 bands**, `NETCDF_DIM_time` carrying `202013`/`202113`, and **no `time#units` attribute at all** — so there is no CF epoch to decode against and the raw values simply *are* `YYYYMM`. A naive `BuildVRT → Translate` renders two annual means as if they were months | `gdalio/cf.decode_monthly_stamp`; the mosaic writes only the keep-list. Fixture `regional_monthly_t2m.nc` |
| P-9 | Climatology keys are `JAN`…`DEC` **plus `ANN`** — the same trap under another name — and its header reports `time_standard: LST` | `timeaxis` handles both. Climatology is otherwise deferred |
| P-10 | **`time-standard` defaults to LST, not UTC.** Measured at Boulder for 2024-06-01 hourly `ALLSKY_SFC_SW_DWN`: the identical peak value **911.15** appears at `2024060117` under UTC and `2024060110` under LST — a clean 7-hour phase shift. Pairing LST data against a UTC model silently moves the diurnal cycle | Every request emits `time-standard`; UTC is the default; `qa.TIME_STANDARD_DRIFT` checks the **returned** `header.time_standard`, not the requested one. Fixtures `point_hourly_utc.json` / `point_hourly_lst.json` |
| P-19 | Adjacent timesteps must use **half-open** intervals. With closed intervals two neighbouring days both match an instant on their shared boundary and the QGIS animation flickers between them | `timeaxis` returns `[start, end)`; the raster path uses `QgsDateTimeRange(t0, t1, True, False)` |
| P-20 | The CF epoch varies **per parameter and per era**: `days since 1980-12-31` (MERRA-2 T2M), `days since 2000-12-31` (CERES solar 2024), `days since 1984-01-01` (CERES solar 1985), `hours since …` for hourly | `gdalio/cf` decodes each response against its own `time#units`; never merge on raw index |

### Units and values

| # | Trap | Handled in |
|---|---|---|
| P-11 | **Two different fill sentinels.** JSON/CSV/ASCII use `-999.0` (declared in `header.fill_value`); NetCDF uses **NaN** (`GetNoDataValue()`) | `decode` reads `header.fill_value` and never a literal; `gdalio` reads `GetNoDataValue()` |
| P-12 | **Mask before scaling.** `-999 × 41.667` is `-41625`, which is not obviously wrong on a colour ramp | `units.to_canonical` and `convert_series` mask first; unit-tested with the neighbouring values asserted intact |
| P-13 | Units depend on **(parameter, temporal, community)**. Daily `ALLSKY_SFC_SW_DWN` is `kW-hr/m^2/day` (RE), `MJ/m^2/day` (AG), `W m-2` (SB); `T2M` is `C` in all three. A table keyed on `(parameter, temporal)` — DAVINCI's `POWER_CATALOG` — is wrong the first time a user changes community | `core/units.py` is keyed on the **returned units string**, which every response carries. 23 distinct strings observed across the three daily dictionaries |
| P-14 | **`Wh/m^2` → `W m-2` is ×1, not ×3600** — a watt-hour accumulated over one hour *is* a watt. The original design assumed hourly solar arrived as `W/m^2`; it does not | `units.UnitRule.per_accumulation`, dividing by `STEP_HOURS[temporal]` |
| P-15 | `T2M_MAX` / `T2M_MIN` **do not exist hourly** (422 `"One of your parameters is incorrect"`) — they are daily aggregates | `dictionary.DAILY_ONLY_PARAMETERS` + `qa.PARAM_NOT_AT_TEMPORAL`, refused locally |
| P-31 | `valid_min` / `valid_max` arrive free in the response attributes, in **native** units (`T2M`: −125…80) | `qa.check_valid_range`; also reused as symbology limits |
| — | `ALLSKY_SFC_UV_INDEX` has units `'W m-2 x 40'`. The served value **is** the index; dividing by 40 would give irradiance but no longer the quantity asked for | `units.UNIT_RULES` maps it to `UV index` with no arithmetic |

### Provenance — the ones that put a wrong label on a right number

| # | Trap | Handled in |
|---|---|---|
| P-24 | **`header.sources` is per request, not per parameter.** A `T2M,ALLSKY_SFC_SW_DWN` request returns `['MERRA2','SYN1DEG']`, attributable to neither value | `provenance.family_groups` splits point requests by parent dataset, so each response has a single source. `qa.MIXED_SOURCES` (ERROR) catches any that slip through |
| P-25 | **POWER's CSV header contradicts its own JSON header.** For a 1985 daily solar request the CSV parameter line names `CERES SYN1deg` while `header.sources` for the identical request says `['SRB']` | `provenance` reads `header.sources` only. Pinned by a unit test so the choice is not silently reversed |
| P-26 | **The SRB → CERES SYN1deg transition is 2001-01-01**, bisected against the live API: 2000-12-31 → `['SRB']`, 2001-01-01 → `['SYN1DEG']`. "CERES POWER" is only true from 2001 onward, and a 1998–2003 solar series is half one and half the other **with nothing in the data marking the seam** | `provenance.SRB_SYN1DEG_TRANSITION`; `qa.MIXED_PROVENANCE` is an **ERROR at plan time**; the layer name gets a `⚠` prefix and the metadata a constraint. Fixtures `point_solar_2000.json` / `point_solar_2001.json` |
| P-27 | Radiation does not exist before **1984-01-01** — but the failure is **asymmetric**. A radiation-only request for that window is a 422; the same request **with a meteorology parameter alongside** returns **HTTP 200**, drops the radiation parameter, and says so only in `messages[]` | `provenance.RADIATION_RECORD_START`, a **separate constant** from P-26 (two different facts). `qa.RECORD_START` is a **WARNING, not BLOCKING** — blocking would refuse a request POWER honours |
| P-28 | **A 200 response can silently omit a requested parameter.** The 1983 `T2M,ALLSKY_SFC_SW_DWN` request returns 200 with only `T2M` in `properties.parameter` | `decode` keeps both the requested and returned lists and diffs them; `qa.MISSING_PARAMETER`. A parser keyed on the requested list would `KeyError`; one keyed on the response would drop a variable in silence. Fixture `point_1983_partial.json` |
| P-29 | `messages[]` is often non-empty on a **200** — provenance notes, dropped parameters, silent substitutions (`PRECTOT` → `PRECTOTCORR`) | `qa.SOFT_MESSAGES`, text surfaced verbatim |
| P-30 | All-fill responses are **legal** — an ocean-only bbox for a land parameter, or a window before the record starts | `qa.ALL_FILL` / `PARTIAL_FILL`; styling guards against an all-NaN band rather than crashing |
| P-23 | **Point responses echo back the coordinate you requested.** POWER serves the **nearest grid cell**, not an interpolation, and never discloses which cell. So a marker at the click point can sit up to 0.25° lat / 0.3125° lon (MERRA-2) or 0.5° (CERES) from where the number came from | `provenance.snap_to_grid` derives the centre locally and it is stored **labelled as derived**; `qa.CELL_SNAP_DERIVED` |
| P-32 | Solar (CERES 1.0°) and meteorology (MERRA-2 0.5° × 0.625°) are **not co-registered**. For an identical bbox, solar returned 2×2 cell centres strictly interior while met returned 3×5 including both boundaries | `gdalio/mosaic` **refuses** a cross-family mosaic (`CROSS_FAMILY_MOSAIC`); one raster layer per parameter, always |

### Shapes and formats

| # | Trap | Handled in |
|---|---|---|
| P-33 | **A point response is `(time, lat=1, lon=1)` — structurally identical to a regional one.** Dispatching on response shape would confuse them | `decode` has separate `parse_point_response` / `parse_regional_response`; dispatch is on the **mode requested** |
| P-22 | GDAL's raster view **cannot open a POWER point NetCDF**: `1-pixel width/height files not supported`, a bogus 512×512 root, no georeferencing | Never request `format=NETCDF` for point mode |
| P-18 | **Regional NetCDF carries no CRS** — `GetProjection()` is `''`. The layer still loads and draws, so in a WGS84 project the omission is invisible; in a projected one it is gross mis-registration | EPSG:4326 baked into the GTiff at write **and** `setCrs()` called unconditionally. The test asserts the raw `.nc` is `''` first, so it proves the fix rather than the default |
| P-21 | The monthly NetCDF `time` variable has **no `units` attribute at all** | `gdalio/cf` falls through to raw `YYYYMM` |
| P-34 | `long_name` runs to 45 characters (`"All Sky Surface Shortwave Downward Irradiance"`), which truncates in the layer tree to exactly the part that does *not* distinguish it from the clear-sky version | `display.DISPLAY_NAMES`, all ≤ 32 chars and unit-tested for it |
| P-35 | Monthly and climatology dictionaries return **1388** and **1634** parameters against daily's **152**, mostly `_00`…`_23` hour-of-day variants | `dictionary.filter_parameters` hides them behind an advanced flag |
| P-36 | The API version drifts **per endpoint** — daily v2.9.7 and monthly v2.9.8 in the same session | `citation` reads it per response. The live suite logs drift rather than failing on it |
| P-37 | The cache key must hash the **full URL**, so a query field added later can never alias onto an existing entry; writes must be atomic or an interrupted one leaves a truncated file that a cache hit would read as valid forever | `api.cache_path` hashes the URL; `fetch_to_cache` writes `.partial` then `Path.replace` |
| — | `format` is a **closed enum**: `csv`, `json`, `ascii`, `netcdf`, `icasa`, `xarray`. **There is no GeoJSON — because `format=JSON` already is GeoJSON**: a `Feature` for point, a `FeatureCollection` of cell centres for regional. It also carries units, fill value, time standard, sources and API version in band, which is why it is the point-mode wire format | `api.FORMATS` |
| — | ASCII rounds values (`3.5455` → `3.55`) and CSV needs three different parsers (daily/hourly long, regional long, monthly wide) plus a free-text header. Both rejected as wire formats | — |

---

## C. QGIS 4.2.2 / PyQt6 traps

Measured on `QGIS-final-4_2_2.app` — Qgis 4.2.2 "Belém do Pará", Qt 6.11.1, PyQt 6.11.0, Python 3.12.11.

| Trap | Detail | Handled in |
|---|---|---|
| **Never call `QgsApplication.setPrefixPath()` headlessly** | No call → **35** providers (gdal, ogr, mdal, wms …). `setPrefixPath('<app>/Contents/Resources/qgis', True)` → **17**; mdal and wms vanish silently, because it derives a nonexistent `.../Resources/qgis/Contents/PlugIns/qgis` | `tests/qgis/qgis_case.py`: `QgsApplication([], False)`, `initQgis()`, nothing else |
| `PYTHONPATH` needs **`lib-dynload`** | Without it the stdlib C extensions are absent, so numpy — and therefore `qgis.core` — dies with `PyCapsule_Import could not import module "datetime"` | `scripts/qgis_python.sh` |
| `PYTHONHOME` must stay **unset** | Setting it points at `<prefix>/lib/python3.12`, but the stdlib is at `Resources/python3.12`, so the interpreter cannot find `encodings` and aborts | `scripts/qgis_python.sh` |
| **pytest is installed on no interpreter here** | And `/usr/bin/python3` is 3.9.6, too old for ring 1 (which needs ≥ 3.10) | stdlib `unittest`; the Makefile pins miniforge 3.13.13 |
| `.qrc` / `pyrcc` is **dead** | Neither module nor binary ships in the bundle | Icons loaded from disk with `QIcon(os.path.join(...))` |
| `supportsQt6` in `metadata.txt` is **obsolete** | What matters is `qgisMaximumVersion` — without it a plugin is absent from the QGIS 4.2 feed entirely. This is exactly why the incumbent `nasa_power_downloader` plugin cannot be installed on QGIS 4 | `nasa_power/metadata.txt` |
| Unscoped Qt enums are gone | `Qt.AlignLeft` → `Qt.AlignmentFlag.AlignLeft`, `Qt.UTC` → `Qt.TimeSpec.UTC`; `exec_()` and `QRegExp` removed. But `qgis.PyQt.QtWidgets.QAction` **works** — the shim rebinds it from QtGui | All Qt imports go through `qgis.PyQt.*`, never raw `PyQt6` |
| Two separate plugin entry points | `startPlugin` calls `initGui()` only; `startProcessingPlugin` (used by `qgis_process`) calls `initProcessing()` only. **Neither calls both** | The provider is registered from `__init__`, which runs on both paths, behind an idempotence flag — mirroring QGIS's own `processing` plugin |
| `addSubTask()` must precede `taskManager().addTask()` | And the Python `QgsTask` wrapper must stay referenced: C++ owns the task, but a garbage-collected wrapper loses its closures | `qgis_bridge/tasks.py`; the dock holds `self._task` |
| **`QgsNetworkAccessManager` silently discards your `User-Agent`.** `createRequest` assigns the raw header unconditionally, so `request.setHeader(UserAgentHeader, …)` never leaves the process. Measured: POWER received `Mozilla/5.0 QGIS/40202/macOS Tahoe (26.6.2)` and nothing identifying this client — which is exactly what POWER asks clients not to do | Also measured: **request preprocessors run *after* that assignment**, so `QgsNetworkAccessManager.setRequestPreprocessor` is the only remaining hook | `qgis_bridge/net.register_user_agent()` — host-scoped so it is inert for every other request in the process, idempotent so a reload cannot double the token, and `unregister_user_agent()` from `plugin.unload()` because a preprocessor is registered **process-wide** and would otherwise outlive the module holding it |
| Avoid `QgsTask.fromFunction` | It is pure Python (`qgis/core/additions/qgstaskwrapper.py`) and its `if self.returned_values:` check calls the completion callback with different arity when a task returns `0`, `[]` or `{}` — which an all-cached fetch does | Subclass `QgsTask` |
| A cancelled `QgsBlockingNetworkRequest` can return **`NoError` with an empty body** | Which would be cached as a zero-byte file and, since hits never re-fetch, read as valid data forever | `UrllibFetcher` and `QgisFetcher` both raise on an empty body; writes are `.partial` + `replace` |
| `QgsRendererRange.symbol()` borrows from the range | `layer.renderer().ranges()[0].symbol().color().name()` as one chain frees the temporary range before the colour is read and returns **`#000000`** — no crash, no warning. Binding `ranges` (or the range) to a name first gives the real `#0571b0` | `tests/qgis/test_styling.py` binds `ranges` before every colour assertion |
| `QgsColorRampShader.ClassificationMode.Continuous` **ignores the class count** | Asked for 11, produced 5 | Use `EqualInterval` |
| Colour ramp names are **capitalised** | `'Viridis'`, `'RdBu'` — not lowercase. 35 exist in this build | `styling.py`, with a test asserting each named ramp resolves non-`None` |
| `Qgis.PlotAxisType` has **only** `Categorical` and `Interval` | There is no datetime axis in the native charting API | `plot_widget` uses Categorical with thinned labels, Interval with a month index |
| **QtWebEngine is absent from the bundle** | So DataPlotly's plot panel cannot render here at all | Native `QgsLineChartPlot`; bundled matplotlib 3.11.1 works under `QT_API='PyQt6'` and stays a guarded fallback |
| Reload strips the plugin from `sys.modules` and `sys.path` | Anything still registered outlives the module that made it: duplicate icons, duplicate toolbox groups, stale-pointer crashes | Full teardown in `unload()`; every milestone verifies reload-twice-and-count |
| Headless `qgisSettingsDirPath()` drops the `QGIS4` segment | So a re-derived cache path differs between the desktop and `qgis_process`, and every CLI run would re-fetch what the dock already downloaded | `paths.resolve_cache_dir()` resolves once into a persisted setting |
| `QgsVectorLayerTemporalProperties` reads **fields, not field names** | So a wide layout with one column per timestep cannot animate at all; and `InstantFromField` + a fixed duration widens the window and over-selects | Long form + `FeatureDateTimeStartAndEndFromFields` |

---

## D. Re-verifying

`make test-live` (needs `POWER_LIVE=1`) is a **contract monitor**: one assertion per fact above, so
the day POWER changes something the failure names it. It is opt-in and never runs on plugin load.

`./scripts/refresh_fixtures.py --list` prints every fixture with its URL and the trap it pins.
`./scripts/refresh_fixtures.py` re-downloads them all, one at a time with a pause.
