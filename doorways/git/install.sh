#!/usr/bin/env bash
# Install the Strawberry git doorway globally (WIRING.md §5).
#
#   doorways/git/install.sh            symlink hooks into ~/.config/git/hooks, set core.hooksPath
#   doorways/git/install.sh --remove   undo both
#
# Symlinks, not copies: editing the hooks in the repo updates the live ones.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC="$ROOT/doorways/git"
HOOKS_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/git/hooks"
HOOKS=(post-commit pre-push strawberry-git-event)

if [ "${1:-}" = "--remove" ]; then
  for h in "${HOOKS[@]}"; do
    [ -L "$HOOKS_DIR/$h" ] && rm -f "$HOOKS_DIR/$h" && echo "removed $HOOKS_DIR/$h"
  done
  if [ "$(git config --global core.hooksPath || true)" = "$HOOKS_DIR" ]; then
    git config --global --unset core.hooksPath
    echo "unset core.hooksPath"
  fi
  exit 0
fi

current="$(git config --global core.hooksPath || true)"
if [ -n "$current" ] && [ "$current" != "$HOOKS_DIR" ]; then
  echo "core.hooksPath is already $current; not changing it." >&2
  echo "Either move your hooks into $HOOKS_DIR or add the Strawberry hooks to $current by hand." >&2
  exit 1
fi

mkdir -p "$HOOKS_DIR"
for h in "${HOOKS[@]}"; do
  chmod +x "$SRC/$h"
  if [ -e "$HOOKS_DIR/$h" ] && [ ! -L "$HOOKS_DIR/$h" ]; then
    echo "$HOOKS_DIR/$h exists and is not a symlink; leaving it alone." >&2
    exit 1
  fi
  ln -sfn "$SRC/$h" "$HOOKS_DIR/$h"
  echo "linked $HOOKS_DIR/$h -> $SRC/$h"
done
git config --global core.hooksPath "$HOOKS_DIR"
echo "core.hooksPath = $HOOKS_DIR"
echo "Every commit and push on this machine now pings strawberryd (if it is running)."
