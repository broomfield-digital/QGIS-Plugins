#!/bin/sh
# Symlink the plugin package into the QGIS user profile so QGIS loads the
# working tree directly -- edit, reload, see the change. No copying, no zip.
#
# The profile directory is QGIS4 (not QGIS3) for QGIS 4.x.
set -eu

PROFILE="${QGIS_PROFILE:-default}"
PLUGIN_DIR="$HOME/Library/Application Support/QGIS/QGIS4/profiles/$PROFILE/python/plugins"
REPO_ROOT="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
SRC="$REPO_ROOT/nasa_power"
DEST="$PLUGIN_DIR/nasa_power"

case "${1:-link}" in
    link)
        [ -d "$SRC" ] || { echo "install_link.sh: no package at $SRC" >&2; exit 1; }
        mkdir -p "$PLUGIN_DIR"
        # -n so an existing symlink is replaced rather than followed, which
        # would otherwise nest the link inside the directory it points at.
        ln -sfn "$SRC" "$DEST"
        echo "linked $DEST -> $SRC"
        echo "Enable it in QGIS: Plugins > Manage and Install Plugins > Installed > NASA POWER"
        ;;
    unlink)
        if [ -L "$DEST" ]; then
            rm "$DEST"
            echo "removed $DEST"
        elif [ -e "$DEST" ]; then
            echo "install_link.sh: $DEST is not a symlink; refusing to delete it" >&2
            exit 1
        else
            echo "nothing linked at $DEST"
        fi
        ;;
    *)
        echo "usage: $0 [link|unlink]" >&2
        exit 2
        ;;
esac
