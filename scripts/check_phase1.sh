#!/usr/bin/env bash
# Phase 1 acceptance (WIRING.md §11): daemon unit tests, then a headless widget talking
# to a real strawberryd over the real websocket, then the preferences checks. Prints the report.
#
#   scripts/check_phase1.sh                                   the source widget (godot on PATH)
#   WIDGET=dist/strawberry-widget-0.1.0-linux-x86_64 scripts/check_phase1.sh   the exported binary
#   SKIP_UNIT_TESTS=1 ...                                     the widget checks only (CI ran the tests)
#
# The exported binary has no --script (release templates drop it), so it runs the same
# validators through `-- --acceptance=res://validate_*.gd` (widget.gd hands the SceneTree over).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${STRAWBERRYD_PORT:-8771}"   # off the default port so a running daemon is left alone
BASE="http://127.0.0.1:$PORT"

if [ -z "${SKIP_UNIT_TESTS:-}" ]; then
  echo "== strawberryd unit tests"
  (cd "$ROOT" && uv sync --quiet --inexact --group gpu && .venv/bin/python -m pytest -q)
fi

WIDGET="${WIDGET:-}"
if [ -n "$WIDGET" ]; then
  WIDGET="$(realpath "$WIDGET")"
  [ -x "$WIDGET" ] || { echo "WIDGET=$WIDGET is not an executable" >&2; exit 1; }
elif [ ! -d "$ROOT/widget/.godot" ]; then
  echo "== importing widget project"
  godot --headless --path "$ROOT/widget" --editor --import >/dev/null 2>&1 || true
fi

echo "== starting strawberryd on :$PORT"
# Its own state dir, with the first-run privacy note marked as shown: the validators expect
# exactly the performances they ask for, not her one-time bubble (firstrun.py).
DSTATE="$(mktemp -d)"
mkdir -p "$DSTATE/strawberry" && touch "$DSTATE/strawberry/privacy-notice-shown"
XDG_STATE_HOME="$DSTATE" "$ROOT/.venv/bin/strawberryd" --port "$PORT" --config "$ROOT/scripts/check_config.toml" &
DPID=$!
cleanup() { kill "$DPID" 2>/dev/null || true; wait "$DPID" 2>/dev/null || true; rm -rf "$DSTATE"; }
trap cleanup EXIT
for _ in $(seq 1 50); do
  curl -fsS "$BASE/health" >/dev/null 2>&1 && break
  sleep 0.1
done
curl -fsS "$BASE/health" >/dev/null || { echo "strawberryd did not start" >&2; exit 1; }

STATUS=0
# validate_prefs.gd runs the widget's "Settings file…" path: `$STRAWBERRY_CLI config --init`.
export STRAWBERRY_CLI="$ROOT/.venv/bin/strawberry"
if [ -n "$WIDGET" ]; then
  REPORT="$(mktemp --suffix=.json)"
  echo "== widget end-to-end (headless, $(basename "$WIDGET"))"
  "$WIDGET" --headless -- --acceptance=res://validate_widget.gd \
    --daemon="$BASE" --ws="ws://127.0.0.1:$PORT/ws" --report="$REPORT" || STATUS=$?
  echo "== widget preferences"
  "$WIDGET" --headless -- --acceptance=res://validate_prefs.gd || STATUS=$?
else
  REPORT="$ROOT/widget/widget_checks.json"
  echo "== widget end-to-end (headless)"
  godot --headless --path "$ROOT/widget" --script res://validate_widget.gd -- \
    --daemon="$BASE" --ws="ws://127.0.0.1:$PORT/ws" || STATUS=$?
  echo "== widget preferences"
  godot --headless --path "$ROOT/widget" --script res://validate_prefs.gd || STATUS=$?
fi

cat "$REPORT"
echo
exit $STATUS
