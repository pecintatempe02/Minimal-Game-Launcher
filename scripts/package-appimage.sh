#!/bin/sh
# Package the staged AppDir into dist/Minimal-Game-Launcher.AppImage.
# Runtime resolution order:
#   1. APPIMAGE_RUNTIME env
#   2. runtime-x86_64 beside detected appimagetool executable
#   3. ~/tools/appimage/runtime-x86_64
#   4. /usr/lib/appimage/runtime-x86_64

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
STAGING="$ROOT/build/appdir"
DIST="$ROOT/dist"
APPIMAGE="$DIST/Minimal-Game-Launcher.AppImage"

mkdir -p -- "$DIST"

if [ ! -d "$STAGING" ]; then
  echo "ERROR: AppDir not found at $STAGING. Run ./scripts/build-appdir.sh first." >&2
  exit 1
fi

# 2. Detect appimagetool through PATH
if ! command -v appimagetool >/dev/null 2>&1; then
  echo "ERROR: appimagetool was not found in PATH. Install appimagetool and ensure its executable is on PATH." >&2
  exit 1
fi
APPIMAGETOOL_CMD="$(command -v appimagetool)"

# 3. Detect the runtime in order
RUNTIME=""

if [ -n "${APPIMAGE_RUNTIME:-}" ]; then
  if [ -f "$APPIMAGE_RUNTIME" ] && [ -x "$APPIMAGE_RUNTIME" ]; then
    RUNTIME="$APPIMAGE_RUNTIME"
  else
    echo "ERROR: APPIMAGE_RUNTIME ($APPIMAGE_RUNTIME) is missing or not executable. Set it to an executable AppImage runtime file." >&2
    exit 1
  fi
fi

if [ -z "$RUNTIME" ]; then
  APPIMAGETOOL_DIR=$(dirname "$APPIMAGETOOL_CMD")
  CANDIDATE="$APPIMAGETOOL_DIR/runtime-x86_64"
  if [ -f "$CANDIDATE" ] && [ -x "$CANDIDATE" ]; then
    RUNTIME="$CANDIDATE"
  fi
fi

if [ -z "$RUNTIME" ]; then
  CANDIDATE="$HOME/tools/appimage/runtime-x86_64"
  if [ -f "$CANDIDATE" ] && [ -x "$CANDIDATE" ]; then
    RUNTIME="$CANDIDATE"
  fi
fi

if [ -z "$RUNTIME" ]; then
  CANDIDATE="/usr/lib/appimage/runtime-x86_64"
  if [ -f "$CANDIDATE" ] && [ -x "$CANDIDATE" ]; then
    RUNTIME="$CANDIDATE"
  fi
fi

# 4. Validate runtime
if [ -z "$RUNTIME" ]; then
  echo "ERROR: runtime-x86_64 was not found or executable." >&2
  echo "Set APPIMAGE_RUNTIME to an executable runtime file, or install it beside appimagetool, at ~/tools/appimage/runtime-x86_64, or at /usr/lib/appimage/runtime-x86_64." >&2
  exit 1
fi

if [ ! -f "$RUNTIME" ]; then
  echo "BLOCKER: selected runtime path missing: $RUNTIME" >&2
  exit 1
fi

if [ ! -x "$RUNTIME" ]; then
  echo "BLOCKER: selected runtime not executable: $RUNTIME" >&2
  exit 1
fi

# Invoke appimagetool with options before the AppDir and output arguments.
echo "Packaging via: $APPIMAGETOOL_CMD"
echo "AppDir: $STAGING"
echo "Output: $APPIMAGE"
echo "Runtime (--runtime-file): $RUNTIME"

ARCH=x86_64 "$APPIMAGETOOL_CMD" --runtime-file "$RUNTIME" "$STAGING" "$APPIMAGE"

echo "Packaging complete: $APPIMAGE"
