#!/usr/bin/env bash
# Tray acceptance (WIRING.md §14): register the StatusNotifierItem on the real session bus and
# ask busctl what is there. The unit tests build the messages by hand; this one proves the item
# is on the bus, answers its properties, hands out its menu, and is picked up by the watcher.
#
#   scripts/check_tray.sh            # own daemon on :8772, tray with --no-children
#   PORT=8770 scripts/check_tray.sh  # against the daemon that is already running (read-only)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${PORT:-8772}"          # off the default port so a running daemon is left alone
BASE="http://127.0.0.1:$PORT"
D="$ROOT/strawberryd/.venv/bin/strawberryd"
OWN_DAEMON=""

command -v busctl >/dev/null || { echo "busctl (systemd) is needed for this check" >&2; exit 2; }
[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ] || [ -S "/run/user/$(id -u)/bus" ] || {
  echo "no session bus in this environment" >&2; exit 2; }

cleanup() {
  [ -n "${TRAY_PID:-}" ] && kill "$TRAY_PID" 2>/dev/null || true
  [ -n "$OWN_DAEMON" ] && kill "$OWN_DAEMON" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT

if ! curl -fsS "$BASE/health" >/dev/null 2>&1; then
  echo "== starting a daemon of our own on :$PORT"
  "$D" --port "$PORT" --config "$ROOT/scripts/check_config.toml" >/tmp/strawberry-check-tray.log 2>&1 &
  OWN_DAEMON=$!
  for _ in $(seq 1 60); do curl -fsS "$BASE/health" >/dev/null 2>&1 && break; sleep 0.2; done
fi
curl -fsS "$BASE/health" >/dev/null || { echo "no daemon on :$PORT; see /tmp/strawberry-check-tray.log" >&2; exit 1; }

echo "== starting the tray (--no-children) against :$PORT"
"$D" --tray --no-children --port "$PORT" &
TRAY_PID=$!
NAME="org.kde.StatusNotifierItem-$TRAY_PID-1"
for _ in $(seq 1 50); do busctl --user status "$NAME" >/dev/null 2>&1 && break; sleep 0.1; done
busctl --user status "$NAME" >/dev/null || { echo "the tray did not take $NAME" >&2; exit 1; }

echo
echo "== the item's properties (IconPixmap shown by size, not by pixel)"
# cut first: the pixmaps are 33 kB of bytes on one line and busctl prints every one of them.
busctl --user introspect "$NAME" /StatusNotifierItem org.kde.StatusNotifierItem | cut -c1-110 | grep -v IconPixmap
# The pixmaps are ARGB32 bytes; show the head of the reply (count, then width height length …).
# cut, not head: head closes the pipe early and busctl dies of SIGPIPE under `set -o pipefail`.
PIXMAP="$(busctl --user get-property "$NAME" /StatusNotifierItem org.kde.StatusNotifierItem IconPixmap | cut -c1-70)"
echo "  IconPixmap: $PIXMAP … (the sizes, then the ARGB32 bytes)"
echo
echo "== the menu (com.canonical.dbusmenu GetLayout; every row and submenu row, in order)"
# recursionDepth is 1 rather than -1 because busctl reads a leading dash as an option of its own.
busctl --user call "$NAME" /MenuBar com.canonical.dbusmenu GetLayout iias 0 1 0 \
  | grep -o '"label" s "[^"]*"' | sed 's/"label" s /  menu row: /' || true
echo
echo "== the watcher"
if busctl --user get-property org.kde.StatusNotifierWatcher /StatusNotifierWatcher \
     org.kde.StatusNotifierWatcher RegisteredStatusNotifierItems 2>/dev/null | tr ' ' '\n' | grep -q "$TRAY_PID"; then
  echo "  registered with org.kde.StatusNotifierWatcher as $NAME"
else
  echo "  NOT in the watcher's list (no AppIndicator extension? she still runs; the right-click menu covers it)"
fi
echo
echo "== a click on Quit is not sent (this is a read-only check); asking AboutToShow instead"
busctl --user call "$NAME" /MenuBar com.canonical.dbusmenu AboutToShow i 0
echo
echo "tray check done"
