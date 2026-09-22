#!/usr/bin/env bash
# Export the widget as one standalone Linux binary (PACKAGING.md step 4, WIRING.md §13).
#
#   scripts/build_widget.sh            -> dist/strawberry-widget-<version>-linux-x86_64 (+ .sha256)
#
# The version is the package's (pyproject.toml), stamped into application/config/version of a
# staging copy of widget/, so the checkout's project.godot stays unversioned and a source run
# reports "dev" in its hello (WIRING.md §1). Needs Godot 4.7.2 (GODOT=..., default `godot` on
# PATH) and its official export templates in ~/.local/share/godot/export_templates/4.7.2.stable/.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GODOT="${GODOT:-godot}"
GODOT_VERSION="4.7.2.stable"
DIST="${DIST:-$ROOT/dist}"

VERSION="$(sed -n 's/^version *= *"\([^"]*\)".*/\1/p' "$ROOT/pyproject.toml" | head -n 1)"
[ -n "$VERSION" ] || { echo "no version in $ROOT/pyproject.toml" >&2; exit 1; }
NAME="strawberry-widget-$VERSION-linux-x86_64"

command -v "$GODOT" >/dev/null || { echo "godot not found (set GODOT=/path/to/godot 4.7.2)" >&2; exit 1; }
HAVE="$("$GODOT" --version | head -n 1)"
case "$HAVE" in
  "$GODOT_VERSION"*) ;;
  *) echo "Godot $GODOT_VERSION needed, $GODOT is $HAVE" >&2; exit 1 ;;
esac
TEMPLATES="${XDG_DATA_HOME:-$HOME/.local/share}/godot/export_templates/$GODOT_VERSION"
[ -f "$TEMPLATES/linux_release.x86_64" ] || {
  echo "export templates missing: $TEMPLATES/linux_release.x86_64" >&2
  echo "(Godot_v4.7.2-stable_export_templates.tpz from the godotengine GitHub release, checked against its SHA512-SUMS.txt)" >&2
  exit 1
}

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
echo "== staging widget/ (version $VERSION)"
# The import cache comes along when there is one: re-importing the GLB is the slow part.
tar -C "$ROOT/widget" --exclude='*_checks.json' --exclude='capture_wave.png' \
    --exclude='hat_*.png' --exclude='sleep_*.png' -cf - . | tar -C "$STAGE" -xf -
sed -i "s/^\[application\]$/[application]\n\nconfig\/version=\"$VERSION\"/" "$STAGE/project.godot"
grep -q "^config/version=\"$VERSION\"$" "$STAGE/project.godot" || { echo "could not stamp the version" >&2; exit 1; }

echo "== importing"
"$GODOT" --headless --path "$STAGE" --import >"$STAGE/import.log" 2>&1 || { cat "$STAGE/import.log" >&2; exit 1; }

echo "== exporting $NAME"
mkdir -p "$DIST"
OUT="$DIST/$NAME"
rm -f "$OUT" "$OUT.sha256"
"$GODOT" --headless --path "$STAGE" --export-release Linux "$OUT" >"$STAGE/export.log" 2>&1 || true
if [ ! -s "$OUT" ] || grep -q "^ERROR" "$STAGE/export.log"; then
  cat "$STAGE/export.log" >&2
  echo "export failed" >&2
  exit 1
fi
chmod +x "$OUT"
(cd "$DIST" && sha256sum "$NAME" > "$NAME.sha256")
ls -l "$OUT"
cat "$OUT.sha256"
