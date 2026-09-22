#!/usr/bin/env bash
# Kill and restart strawberryd under a headless widget; the widget must reconnect by itself.
#
#   scripts/check_reconnect.sh          graceful stop (SIGTERM): daemon closes the socket, widget notices at once
#   SIGNAL=KILL scripts/check_reconnect.sh   hard kill: widget must notice via its own liveness ping
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${STRAWBERRYD_PORT:-8774}"
SIGNAL="${SIGNAL:-TERM}"
D=("$ROOT/.venv/bin/strawberryd" --config "$ROOT/scripts/check_config.toml")
RESTART_AFTER=3
DOWN_FOR="${DOWN_FOR:-2}"
PIDFILE="$ROOT/widget/.reconnect_daemon.pid"

wait_gone() { for _ in $(seq 1 100); do kill -0 "$1" 2>/dev/null || return 0; sleep 0.1; done; return 1; }
wait_health() { for _ in $(seq 1 50); do curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && return 0; sleep 0.1; done; return 1; }

"${D[@]}" --port "$PORT" >/dev/null 2>&1 &
echo $! >"$PIDFILE"
cleanup() { [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null || true; rm -f "$PIDFILE"; }
trap cleanup EXIT
wait_health || { echo "daemon did not start" >&2; exit 1; }

(
  sleep "$RESTART_AFTER"
  pid="$(cat "$PIDFILE")"
  start=$(date +%s.%N)
  kill "-$SIGNAL" "$pid"
  wait_gone "$pid" && echo "== daemon gone after SIG$SIGNAL in $(printf '%.1f' "$(echo "$(date +%s.%N) - $start" | bc)")s" || echo "== daemon still alive after 10s!"
  sleep "$DOWN_FOR"
  "${D[@]}" --port "$PORT" >/dev/null 2>&1 &
  echo $! >"$PIDFILE"
  wait_health && echo "== daemon restarted" || echo "== restarted daemon not healthy!"
) &

# A hard kill is only noticed by the widget's own silence timeout (12s), so allow for it.
DROP_TIMEOUT=6; [ "$SIGNAL" = "KILL" ] && DROP_TIMEOUT=18
STATUS=0
godot --headless --path "$ROOT/widget" --script res://validate_reconnect.gd -- \
  --ws="ws://127.0.0.1:$PORT/ws" --restart-after="$RESTART_AFTER" --drop-timeout="$DROP_TIMEOUT" --reconnect-timeout=20 || STATUS=$?
wait
cat "$ROOT/widget/reconnect_checks.json"; echo
exit $STATUS
