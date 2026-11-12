#!/bin/sh
# MGL native Rust AppImage packager.
#
# This is deliberately independent of the legacy Python/GTK pipeline:
#   * it never reads or writes build.sh, scripts/build-appdir.sh,
#     scripts/package-appimage.sh, or build/appdir;
#   * it stages only the native binary and packaging metadata;
#   * it writes its own AppDir under build/rust-appdir and its artifact under
#     dist/MGL-<version>-x86_64.AppImage.
#
# Usage:
#   ./build-rust-appimage.sh [--skip-build] [--offline]
#
# Environment overrides:
#   APPIMAGETOOL           appimagetool executable
#   APPIMAGE_RUNTIME       AppImage type-2 runtime file
#   MGL_ICON               SVG icon source
#   MGL_RUST_BUILD_DIR     staging directory (default build/rust-appdir)
#   MGL_RUST_DIST          output directory (default dist)
#   MGL_CARGO_OFFLINE=1    add --offline to cargo
#   MGL_APPIMAGE_EXTRACT_AND_RUN=1
#                          run appimagetool with --appimage-extract-and-run

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
NATIVE="$ROOT/native"
APP_ID="mgl"
APP_NAME="MGL"
BUILD_DIR="${MGL_RUST_BUILD_DIR:-$ROOT/build/rust-appdir}"
DIST="${MGL_RUST_DIST:-$ROOT/dist}"
ICON_SOURCE="${MGL_ICON:-$NATIVE/assets/mgl.svg}"
ICON_PNG_SOURCE="${MGL_ICON_PNG:-$NATIVE/assets/mgl-256.png}"

SKIP_BUILD=0
CARGO_OFFLINE="${MGL_CARGO_OFFLINE:-0}"

usage() {
    sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --skip-build) SKIP_BUILD=1 ;;
        --offline) CARGO_OFFLINE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

VERSION=$(sed -n 's/^version = "\([^"]*\)"$/\1/p' "$NATIVE/Cargo.toml" | head -n 1)
if [ -z "$VERSION" ]; then
    echo "ERROR: could not read the package version from $NATIVE/Cargo.toml" >&2
    exit 1
fi

ARTIFACT="$APP_NAME-$VERSION-x86_64.AppImage"
APPIMAGE="$DIST/$ARTIFACT"

# ---------------------------------------------------------------- native build
if [ "$SKIP_BUILD" -eq 0 ]; then
    if ! command -v cargo >/dev/null 2>&1; then
        echo "ERROR: cargo was not found in PATH." >&2
        exit 1
    fi
    echo "+ Building native release binary (version $VERSION)"
    (
        cd "$NATIVE"
        if [ "$CARGO_OFFLINE" = "1" ]; then
            cargo build --release --locked --offline
        else
            cargo build --release --locked
        fi
    )
else
    echo "+ Skipping cargo build (--skip-build)"
fi

BINARY="$NATIVE/target/release/mgl"
if [ ! -f "$BINARY" ]; then
    echo "ERROR: native binary not found at $BINARY" >&2
    exit 1
fi
if [ ! -x "$BINARY" ]; then
    echo "ERROR: native binary is not executable: $BINARY" >&2
    exit 1
fi

# -------------------------------------------------------------------- AppDir
echo "+ Staging AppDir at $BUILD_DIR"
rm -rf -- "$BUILD_DIR"
mkdir -p -- \
    "$BUILD_DIR/usr/bin" \
    "$BUILD_DIR/usr/share/applications" \
    "$BUILD_DIR/usr/share/icons/hicolor/scalable/apps" \
    "$BUILD_DIR/usr/share/icons/hicolor/256x256/apps"

install -m 0755 -- "$BINARY" "$BUILD_DIR/usr/bin/mgl"

if [ ! -f "$ICON_SOURCE" ]; then
    echo "ERROR: icon source not found: $ICON_SOURCE" >&2
    exit 1
fi
install -m 0644 -- "$ICON_SOURCE" \
    "$BUILD_DIR/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg"

# appimagetool requires .DirIcon (and ideally the root icon) to be PNG. The
# pre-rendered 256px asset avoids a build-time SVG rasteriser dependency.
if [ ! -f "$ICON_PNG_SOURCE" ]; then
    echo "ERROR: PNG icon source not found: $ICON_PNG_SOURCE" >&2
    exit 1
fi
install -m 0644 -- "$ICON_PNG_SOURCE" \
    "$BUILD_DIR/usr/share/icons/hicolor/256x256/apps/$APP_ID.png"
install -m 0644 -- "$ICON_PNG_SOURCE" "$BUILD_DIR/$APP_ID.png"

# Installed desktop entry: the command name resolves through the AppImage's
# usr/bin on PATH. No build-time absolute path is embedded.
cat > "$BUILD_DIR/usr/share/applications/$APP_ID.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Version=1.5
X-AppImage-Version=$VERSION
Name=$APP_NAME
GenericName=Game Launcher
Comment=Minimal game library and launcher (native Rust)
Exec=$APP_ID
Icon=$APP_ID
Terminal=false
Categories=Game;Utility;
Keywords=games;library;wine;launcher;
StartupNotify=true
StartupWMClass=$APP_ID
X-MGL-Managed=true
DESKTOP

# AppDir root entry is the one appimagetool reads; it must launch AppRun.
sed 's|^Exec=.*$|Exec=AppRun|' \
    "$BUILD_DIR/usr/share/applications/$APP_ID.desktop" \
    > "$BUILD_DIR/$APP_ID.desktop"

# AppRun intentionally does not touch HOME, XDG_*_HOME, WINEPREFIX, PATH (unless
# it is empty), or any library path. Host tools such as wine, wineserver,
# flatpak, gamescope, firefox, zenity/kdialog, wl-copy/wl-paste, ffmpeg and
# ImageMagick therefore remain visible and are never shadowed by bundled copies.
cat > "$BUILD_DIR/AppRun" <<'APPRUN'
#!/bin/sh
set -eu

# APPDIR is normally set by the AppImage runtime. Resolve it from this script
# for the extracted/installed AppDir case.
if [ -z "${APPDIR:-}" ]; then
    APPDIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
fi
export APPDIR

# Never redirect user data into the mounted (read-only) AppImage. XDG paths are
# inherited from the desktop session so the database, covers, settings and logs
# stay in the user's normal locations.
: "${HOME:?HOME is not set; cannot locate user data}"

# Preserve the host PATH. Only supply a conventional default when a launcher
# started with a completely empty environment.
if [ -z "${PATH:-}" ]; then
    PATH=/usr/local/bin:/usr/bin:/bin
    export PATH
fi

exec "$APPDIR/usr/bin/mgl" "$@"
APPRUN
chmod 0755 -- "$BUILD_DIR/AppRun"

# Relative .DirIcon keeps the AppDir relocatable. appimagetool requires PNG.
ln -sfn "$APP_ID.png" "$BUILD_DIR/.DirIcon"

echo "+ AppDir contents:"
find "$BUILD_DIR" -mindepth 1 -printf '%P\n' | sort | sed 's/^/    /'

# --------------------------------------------------------------- appimagetool
if [ -n "${APPIMAGETOOL:-}" ]; then
    APPIMAGETOOL_CMD="$APPIMAGETOOL"
elif command -v appimagetool >/dev/null 2>&1; then
    APPIMAGETOOL_CMD=$(command -v appimagetool)
elif [ -x "$HOME/tools/appimage/appimagetool-x86_64.AppImage" ]; then
    APPIMAGETOOL_CMD="$HOME/tools/appimage/appimagetool-x86_64.AppImage"
else
    echo "ERROR: appimagetool was not found." >&2
    echo "Install it, put it on PATH, or set APPIMAGETOOL." >&2
    exit 1
fi
if [ ! -x "$APPIMAGETOOL_CMD" ]; then
    echo "ERROR: appimagetool is not executable: $APPIMAGETOOL_CMD" >&2
    exit 1
fi

RUNTIME="${APPIMAGE_RUNTIME:-}"
if [ -z "$RUNTIME" ]; then
    for candidate in \
        "$(dirname -- "$APPIMAGETOOL_CMD")/runtime-x86_64" \
        "$HOME/tools/appimage/runtime-x86_64" \
        "/usr/lib/appimage/runtime-x86_64"
    do
        if [ -f "$candidate" ] && [ -x "$candidate" ]; then
            RUNTIME="$candidate"
            break
        fi
    done
fi
if [ -z "$RUNTIME" ] || [ ! -f "$RUNTIME" ] || [ ! -x "$RUNTIME" ]; then
    echo "ERROR: no executable AppImage runtime found." >&2
    echo "Set APPIMAGE_RUNTIME to a type-2 runtime file." >&2
    exit 1
fi

mkdir -p -- "$DIST"
rm -f -- "$APPIMAGE" "$APPIMAGE.sha256"

echo "+ Packaging with: $APPIMAGETOOL_CMD"
echo "+ Runtime:        $RUNTIME"
echo "+ Output:         $APPIMAGE"

EXTRACT_AND_RUN=0
if [ "${MGL_APPIMAGE_EXTRACT_AND_RUN:-0}" = "1" ]; then
    EXTRACT_AND_RUN=1
elif [ ! -e /dev/fuse ]; then
    # appimagetool is commonly distributed as an AppImage itself.
    EXTRACT_AND_RUN=1
fi

if [ "$EXTRACT_AND_RUN" -eq 1 ]; then
    ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 \
        "$APPIMAGETOOL_CMD" --runtime-file "$RUNTIME" "$BUILD_DIR" "$APPIMAGE"
else
    ARCH=x86_64 \
        "$APPIMAGETOOL_CMD" --runtime-file "$RUNTIME" "$BUILD_DIR" "$APPIMAGE"
fi

chmod 0755 -- "$APPIMAGE"

if command -v sha256sum >/dev/null 2>&1; then
    (
        cd "$DIST"
        sha256sum "$ARTIFACT" > "$ARTIFACT.sha256"
    )
    echo "+ SHA-256:        $APPIMAGE.sha256"
fi

SIZE=$(du -h -- "$APPIMAGE" | cut -f1)
echo "+ Size:           $SIZE"
echo "+ Artifact:       $APPIMAGE"
echo "Packaging complete."
