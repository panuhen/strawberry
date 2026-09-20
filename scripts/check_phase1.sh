#!/usr/bin/env bash
# Phase 1 acceptance (WIRING.md §11): daemon unit tests, then a headless widget talking
# to a real strawberryd over the real websocket. Prints widget/widget_checks.json.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${STRAWBERRYD_PORT:-8771}"   # off the default port so a running daemon is left alone
BASE="http://127.0.0.1:$PORT"

echo "== strawberryd unit tests"
(cd "$ROOT/strawberryd" && uv sync --quiet && uv run --quiet pytest -q)

if [ ! -d "$ROOT/widget/.godot" ]; then
  echo "== importing widget project"
  godot --headless --path "$ROOT/widget" --editor --import >/dev/null 2>&1 || true
fi

echo "== starting strawberryd on :$PORT"
"$ROOT/strawberryd/.venv/bin/strawberryd" --port "$PORT" &
DPID=$!
cleanup() { kill "$DPID" 2>/dev/null || true; wait "$DPID" 2>/dev/null || true; }
trap cleanup EXIT
for _ in $(seq 1 50); do
  curl -fsS "$BASE/health" >/dev/null 2>&1 && break
  sleep 0.1
done
curl -fsS "$BASE/health" >/dev/null || { echo "strawberryd did not start" >&2; exit 1; }

echo "== widget end-to-end (headless)"
STATUS=0
godot --headless --path "$ROOT/widget" --script res://validate_widget.gd -- \
  --daemon="$BASE" --ws="ws://127.0.0.1:$PORT/ws" || STATUS=$?

cat "$ROOT/widget/widget_checks.json"
echo
exit $STATUS
