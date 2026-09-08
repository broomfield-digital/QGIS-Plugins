#!/bin/sh
# Run QGIS's bundled Python with the environment it needs to import qgis/numpy/GDAL.
#
# Three things are load-bearing and each was measured on this machine:
#
#   * lib-dynload MUST be on PYTHONPATH. Without it the stdlib C extensions
#     (_struct, _datetime) are missing, so numpy dies with
#     "PyCapsule_Import could not import module \"datetime\"" and qgis.core
#     with it.
#   * PYTHONHOME must stay UNSET. Setting it points Python at
#     <prefix>/lib/python3.12, but this bundle keeps the stdlib at
#     Resources/python3.12, so the interpreter cannot find "encodings" and
#     aborts before running anything.
#   * PROJ_DATA must point at the bundle's proj.db or pyproj warns and CRS
#     operations silently degrade.
#
# Override QGIS_APP to test against a different QGIS install.
set -eu

QGIS_APP="${QGIS_APP:-/Users/Dfillmor/Applications/QGIS-final-4_2_2.app}"
RES="$QGIS_APP/Contents/Resources"
PY="$RES/python3.12"

if [ ! -x "$QGIS_APP/Contents/MacOS/python3.12" ]; then
    echo "qgis_python.sh: no bundled interpreter at $QGIS_APP" >&2
    echo "  set QGIS_APP to your QGIS .app bundle" >&2
    exit 1
fi

unset PYTHONHOME
PYTHONPATH="$PY:$PY/lib-dynload:$PY/site-packages"
# Put the repo root first so `import nasa_power` resolves to the working tree
# rather than to whatever is symlinked into the QGIS profile.
REPO_ROOT="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
PYTHONPATH="$REPO_ROOT:$PYTHONPATH"
export PYTHONPATH
export PROJ_DATA="${PROJ_DATA:-$RES/qgis/proj}"

exec "$QGIS_APP/Contents/MacOS/python3.12" "$@"
