#!/bin/bash
# install_auto_update.sh — schedule the OFFICIAL `hermes update` to run daily.
#
# This replaces the old custom auto_update.sh (removed). That script reinvented
# the updater and cloned to the wrong place, which broke a real install.
# `hermes update` updates the REAL install dir, has built-in snapshot/rollback,
# and is cross-platform — so the right thing is to just SCHEDULE it.
#
# Prerequisite: the install's git `origin` must point at THIS fork so that
# `hermes update` pulls evolution (and `upstream` at NousResearch). See
# AUTO_UPGRADE.md "Switch an existing install onto the fork".
#
# Usage:
#   scripts/install_auto_update.sh                       # install (daily ~04:17)
#   AUTO_UPDATE_SCHEDULE="30 5 * * *" scripts/install_auto_update.sh
#   scripts/install_auto_update.sh --remove
#
# Windows: cron does not exist — use Task Scheduler to run `hermes update --yes`
# daily (see AUTO_UPGRADE.md).

set -euo pipefail

MARKER="hermes-evolution-auto-update"
# Off-zero minute on purpose (avoid the :00 thundering herd).
SCHEDULE="${AUTO_UPDATE_SCHEDULE:-17 4 * * *}"
LOG="${HERMES_HOME:-$HOME/.hermes}/logs/auto-update.log"

if ! command -v crontab >/dev/null 2>&1; then
    echo "❌ 'crontab' not found." >&2
    echo "   Windows: use Task Scheduler to run 'hermes update --yes' daily." >&2
    exit 1
fi

# --remove: strip the marker line and exit
if [ "${1:-}" = "--remove" ]; then
    REMAIN="$(crontab -l 2>/dev/null | grep -v "$MARKER" || true)"
    if [ -n "$REMAIN" ]; then
        printf '%s\n' "$REMAIN" | crontab -
    else
        crontab -r 2>/dev/null || true   # nothing left — clear the crontab
    fi
    echo "✅ Removed Hermes Evolution auto-update cron entry."
    exit 0
fi

HERMES_BIN="$(command -v hermes || true)"
if [ -z "$HERMES_BIN" ]; then
    echo "❌ 'hermes' not found on PATH. Install Hermes first (see AUTO_UPGRADE.md)." >&2
    exit 1
fi

mkdir -p "$(dirname "$LOG")"

# `hermes update --yes` = non-interactive (auto-answers stash/migration prompts),
# pulls origin/main (the fork), keeps the built-in pre-update backup + rollback.
ENTRY="$SCHEDULE $HERMES_BIN update --yes >> $LOG 2>&1"
# Also keep the optional Turbo-Quant Memory MCP current when uv is present. Use
# uv's ABSOLUTE path (cron's PATH usually omits ~/.local/bin) and a trailing
# `; true` so a memory-upgrade hiccup never fails the job or the hermes update
# that already ran. Skipped entirely if uv isn't installed.
UV_BIN="$(command -v uv || true)"
if [ -n "$UV_BIN" ]; then
    ENTRY="$ENTRY; $UV_BIN tool upgrade turbo-memory-mcp >> $LOG 2>&1; true"
fi
ENTRY="$ENTRY  # $MARKER"

# Idempotent install (if-form, no set -e/pipefail traps on empty crontab).
KEPT="$(crontab -l 2>/dev/null | grep -v "$MARKER" || true)"
if [ -n "$KEPT" ]; then
    printf '%s\n%s\n' "$KEPT" "$ENTRY" | crontab -
else
    printf '%s\n' "$ENTRY" | crontab -
fi

echo "✅ Scheduled daily 'hermes update --yes':"
crontab -l | grep "$MARKER" | sed 's/^/   /'
echo ""
echo "📂 Log:       $LOG"
echo "🧪 Test now:  hermes update --check    (then: hermes update --yes)"
echo "🗑  Remove:    $0 --remove"
echo ""
echo "ℹ️  Ensure the install's git origin points at the fork, or 'hermes update'"
echo "    will pull the wrong repo. See AUTO_UPGRADE.md."
