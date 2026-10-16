#!/usr/bin/env bash
# Wake-gate for evolution-issues: GitHub write AND a fresh research batch.
#
# Council 2026-08-31: issues must consume a research batch, not fire on
# empty days. LAST stdout line is the scheduler wake JSON. Always exit 0.
set +e

ENVF="${HERMES_HOME:-$HOME/.hermes}/.env"
[ -f "$ENVF" ] && { set -a; . "$ENVF" 2>/dev/null; set +a; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
RESEARCH_DIR="$HOME_DIR/evolution/research"
MAX_AGE_SECONDS="${EVOLUTION_ISSUES_BATCH_MAX_AGE:-691200}"  # 8 days

_find_python() {
    if [ -n "${HERMES_PYTHON:-}" ] && [ -x "$HERMES_PYTHON" ]; then
        echo "$HERMES_PYTHON"
        return
    fi
    for c in python3 python; do
        if command -v "$c" >/dev/null 2>&1; then
            command -v "$c"
            return
        fi
    done
    echo ""
}

PY="$(_find_python)"
ACCESS="$SCRIPT_DIR/evolution_github_access.py"
if [ -z "$PY" ] || [ ! -f "$ACCESS" ]; then
    echo "evolution issues-gate: cannot classify GitHub write — not waking." >&2
    echo '{"wakeAgent": false}'
    exit 0
fi

STATE="$("$PY" "$ACCESS" 2>/dev/null | tail -1 | tr -d '[:space:]')"
if [ "$STATE" != "write" ]; then
    echo "evolution issues-gate: GitHub write=$STATE — skipping agent." >&2
    echo '{"wakeAgent": false}'
    exit 0
fi

# Newest YYYY-MM-DD.md that is not a BLOCKED web-gate stub (walk newest-first
# so a later BLOCKED stub does not hide an earlier valid batch).
BATCH=""
if [ -d "$RESEARCH_DIR" ]; then
    while IFS= read -r cand; do
        [ -f "$cand" ] || continue
        if grep -q "Status: BLOCKED" "$cand" 2>/dev/null; then
            continue
        fi
        BATCH="$cand"
        break
    done <<EOF
$(ls -1 "$RESEARCH_DIR"/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].md 2>/dev/null | sort -r)
EOF
fi
if [ -z "$BATCH" ] || [ ! -f "$BATCH" ]; then
    echo "evolution issues-gate: no unblocked research batch — skipping agent." >&2
    echo '{"wakeAgent": false}'
    exit 0
fi

NOW="$(date +%s)"
# GNU coreutils first (`stat -c %Y`). macOS `stat -f %m` is second: GNU
# `stat -f` means --file-system and can exit 0 with a mount-point string,
# which then breaks AGE=$((NOW - MTIME)).
MTIME="$(stat -c %Y "$BATCH" 2>/dev/null || stat -f %m "$BATCH" 2>/dev/null || echo 0)"
case "$MTIME" in
    ''|*[!0-9]*) MTIME=0 ;;
esac
AGE=$((NOW - MTIME))
if [ "$AGE" -gt "$MAX_AGE_SECONDS" ]; then
    echo "evolution issues-gate: research batch older than ${MAX_AGE_SECONDS}s — skipping agent." >&2
    echo '{"wakeAgent": false}'
    exit 0
fi

echo "evolution issues-gate: write access + fresh batch $BATCH — waking agent." >&2
echo '{"wakeAgent": true}'
exit 0
