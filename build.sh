#!/bin/sh
# Minimal Game Launcher — AppImage build entry point.
# Produces build/appdir (always) and attempts dist/Minimal-Game-Launcher.AppImage.
# BLOCKER: final packaging requires appimagetool + a runtime binary.

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DIST="$ROOT/dist"
STAGING="$ROOT/build/appdir"

mkdir -p -- "$DIST"

# Step 1 — build AppDir
if [ -f "$STAGING/AppRun" ]; then
  echo "+ AppDir already exists at $STAGING (cleaning first)"
fi

# Clean only our build outputs, not user/home paths
rm -rf -- "$STAGING" "build/"
mkdir -p -- "$STAGING"

sh -c 'exec "$@"' sh "$ROOT/scripts/build-appdir.sh"

# Step 2 — attempt packaging
if [ -f "$STAGING/AppRun" ]; then
  echo "+ AppDir complete. Contents:"
  find "$STAGING" -type f | sort
else
  echo "ERROR: AppDir build failed." >&2
  exit 1
fi

# Step 3 — package (will report blocker clearly)
echo "+ Attempting .AppImage packaging..."
sh -c 'exec "$@"' sh "$ROOT/scripts/package-appimage.sh"
