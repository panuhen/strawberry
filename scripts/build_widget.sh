#!/usr/bin/env bash
# Export the widget as one standalone binary (PACKAGING.md step 4, WIRING.md §13).
#
#   scripts/build_widget.sh            -> dist/strawberry-widget-<version>-linux-x86_64 (+ .sha256)
#   on Windows (Git Bash)              -> dist/strawberry-widget-<version>-windows-x86_64.exe (+ .sha256)
#   TARGET=windows|linux ...           the other system's binary (the templates hold both)
#
# The version is the package's (pyproject.toml), stamped into application/config/version of a
# staging copy of widget/, so the checkout's project.godot stays unversioned and a source run
# reports "dev" in its hello (WIRING.md §1). Needs Godot 4.7.2 (GODOT=..., default `godot` on
# PATH) and its official export templates in ~/.local/share/godot/export_templates/4.7.2.stable/
# (on Windows %APPDATA%\Godot\export_templates\4.7.2.stable\).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GODOT="${GODOT:-godot}"
GODOT_VERSION="4.7.2.stable"
DIST="${DIST:-$ROOT/dist}"
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) HOST=windows ;;
  *) HOST=linux ;;
esac
TARGET="${TARGET:-$HOST}"
# Paths handed to Godot: Windows form under Git Bash, as they are elsewhere.
native() { if [ "$HOST" = windows ]; then cygpath -m "$1"; else printf '%s\n' "$1"; fi; }

VERSION="$(sed -n 's/^version *= *"\([^"]*\)".*/\1/p' "$ROOT/pyproject.toml" | head -n 1)"
[ -n "$VERSION" ] || { echo "no version in $ROOT/pyproject.toml" >&2; exit 1; }
case "$TARGET" in
  linux) PRESET=Linux; NAME="strawberry-widget-$VERSION-linux-x86_64"; TEMPLATE=linux_release.x86_64 ;;
  windows) PRESET=Windows; NAME="strawberry-widget-$VERSION-windows-x86_64.exe"; TEMPLATE=windows_release_x86_64.exe ;;
  *) echo "TARGET=$TARGET: linux or windows" >&2; exit 1 ;;
esac

command -v "$GODOT" >/dev/null || { echo "godot not found (set GODOT=/path/to/godot 4.7.2)" >&2; exit 1; }
HAVE="$("$GODOT" --version | head -n 1)"
case "$HAVE" in
  "$GODOT_VERSION"*) ;;
  *) echo "Godot $GODOT_VERSION needed, $GODOT is $HAVE" >&2; exit 1 ;;
esac
if [ "$HOST" = windows ]; then
  TEMPLATES="$(cygpath -u "$APPDATA")/Godot/export_templates/$GODOT_VERSION"
else
  TEMPLATES="${XDG_DATA_HOME:-$HOME/.local/share}/godot/export_templates/$GODOT_VERSION"
fi
[ -f "$TEMPLATES/$TEMPLATE" ] || {
  echo "export templates missing: $TEMPLATES/$TEMPLATE" >&2
  echo "(Godot_v4.7.2-stable_export_templates.tpz from the godotengine GitHub release, checked against its SHA512-SUMS.txt)" >&2
  exit 1
}

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
echo "== staging widget/ (version $VERSION)"
# The import cache comes along when there is one: re-importing the GLB is the slow part.
tar -C "$ROOT/widget" --exclude='*_checks.json' --exclude='capture_wave.png' \
    --exclude='hat_*.png' --exclude='sleep_*.png' -cf - . | tar -C "$STAGE" -xf -
# A Windows checkout may have CRLF line ends (core.autocrlf); the stamp matches whole lines.
sed -i 's/\r$//' "$STAGE/project.godot"
sed -i "s/^\[application\]$/[application]\n\nconfig\/version=\"$VERSION\"/" "$STAGE/project.godot"
grep -q "^config/version=\"$VERSION\"$" "$STAGE/project.godot" || { echo "could not stamp the version" >&2; exit 1; }

echo "== importing"
"$GODOT" --headless --path "$(native "$STAGE")" --import >"$STAGE/import.log" 2>&1 || { cat "$STAGE/import.log" >&2; exit 1; }

echo "== exporting $NAME"
mkdir -p "$DIST"
OUT="$DIST/$NAME"
rm -f "$OUT" "$OUT.sha256"
"$GODOT" --headless --path "$(native "$STAGE")" --export-release "$PRESET" "$(native "$OUT")" >"$STAGE/export.log" 2>&1 || true
if [ ! -s "$OUT" ] || grep -q "^ERROR" "$STAGE/export.log"; then
  cat "$STAGE/export.log" >&2
  echo "export failed" >&2
  exit 1
fi
chmod +x "$OUT"
# Git Bash's sha256sum marks the file binary ("<hex> *<name>"); the same "<hex>  <name>" everywhere.
(cd "$DIST" && sha256sum "$NAME" | sed 's/ \*/  /' > "$NAME.sha256")
ls -l "$OUT"
cat "$OUT.sha256"
