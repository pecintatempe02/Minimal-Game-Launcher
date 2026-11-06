#!/bin/sh
# Build the Minimal Game Launcher AppDir staging directory.
# Does NOT create .AppImage (requires appimagetool + runtime binary).

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
STAGING="${MGL_APPDIR:-$ROOT/build/appdir}"
APP_ID="io.github.minimal_game_launcher.MinimalGameLauncher"

mkdir -p -- "$STAGING/usr/bin" "$STAGING/usr/lib/minimal-game-launcher" "$STAGING/usr/share/applications" "$STAGING/usr/share/icons/hicolor/scalable/apps"

# Clean previous staging contents (but keep empty dirs)
find "$STAGING" -mindepth 1 -delete 2>/dev/null || true
mkdir -p -- "$STAGING/usr/bin" "$STAGING/usr/lib/minimal-game-launcher" "$STAGING/usr/share/applications" "$STAGING/usr/share/icons/hicolor/scalable/apps"

# Install complete Python package (preserve directory structure)
cp -R -- "$ROOT/src/minimal_game_launcher" "$STAGING/usr/lib/minimal-game-launcher/"
find "$STAGING/usr/lib/minimal-game-launcher" -name __pycache__ -type d -prune -exec rm -rf -- {} + 2>/dev/null || true

# Copy bundled icon (used by application fallback and embedded theme)
cp -- "$ROOT/src/minimal_game_launcher/ui/$APP_ID.svg" "$STAGING/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg"

# Install desktop entry template with a relocatable command name. The root
# entry uses Exec=AppRun for AppImage launch; this installed copy must not
# embed the build directory.
{
  while IFS= read -r line; do
    case $line in
      Exec=@EXEC@) printf 'Exec=minimal-game-launcher\n' ;;
      *) printf '%s\n' "$line" ;;
    esac
  done < "$ROOT/packaging/$APP_ID.desktop"
} > "$STAGING/usr/share/applications/$APP_ID.desktop"

# Also install the canonical desktop entry for the AppDir. appimagetool uses
# this single desktop ID; no alternate basename entry is generated.
sed 's|^Exec=.*$|Exec=AppRun|' "$STAGING/usr/share/applications/$APP_ID.desktop" > "$STAGING/$APP_ID.desktop"

# Create wrapper script that preserves PYTHONPATH and environment
cat > "$STAGING/usr/bin/minimal-game-launcher" <<'WRAPPER'
#!/bin/sh
# Minimal Game Launcher wrapper for portable/AppDir execution.
# Preserves user XDG directories and does not bundle user data.
STAGING_DIR="${APPDIR:-}"
# Determine PYTHONPATH relative to AppDir or install prefix
if [ -n "${STAGING_DIR:-}" ]; then
  PYTHONPATH="$STAGING_DIR/usr/lib/minimal-game-launcher${PYTHONPATH:+:$PYTHONPATH}"
else
  PYTHONPATH="/usr/lib/minimal-game-launcher${PYTHONPATH:+:$PYTHONPATH}"
fi
export PYTHONPATH
export PYTHONHOME="${PYTHONHOME:-/usr}"
# Preserve host XDG paths; never redirect them into the AppDir
exec /usr/bin/python3 -m minimal_game_launcher "$@"
WRAPPER
chmod 755 -- "$STAGING/usr/bin/minimal-game-launcher"

# Create AppRun launcher pointing to wrapper
cat > "$STAGING/AppRun" <<'APPRUN'
#!/bin/sh
export APPDIR="$(dirname "$(readlink -f "$0")")"
export PYTHONPATH="$APPDIR/usr/lib/minimal-game-launcher${PYTHONPATH:+:$PYTHONPATH}"
exec "$APPDIR/usr/bin/minimal-game-launcher" "$@"
APPRUN
chmod +x -- "$STAGING/AppRun"

# Minimal .DirIcon symlink (optional but improves desktop integration)
if [ -f "$STAGING/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg" ]; then
  ln -sf "$STAGING/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg" "$STAGING/.DirIcon"
fi

echo "AppDir staged at: $STAGING"
echo "Contents:"
find "$STAGING" -type f | sort
