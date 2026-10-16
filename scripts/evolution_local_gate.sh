#!/usr/bin/env bash
# Local-signal wake-gate for class-B evolution jobs (introspection).
#
# Council 2026-08-31: local introspection must NOT require GitHub write.
# Wake the LLM only when introspection_extract.py produces a non-empty
# digest that differs from the last one we already fed the agent.
# Empty / identical digest -> {"wakeAgent": false} (no tokens).
#
# LAST stdout line is the scheduler wake JSON. Always exit 0.
set +e

ENVF="${HERMES_HOME:-$HOME/.hermes}/.env"
[ -f "$ENVF" ] && { set -a; . "$ENVF" 2>/dev/null; set +a; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
STATE_DIR="$HOME_DIR/evolution/introspection"
LAST="$STATE_DIR/.last-digest.json"
EXTRACT="$SCRIPT_DIR/introspection_extract.py"

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
if [ -z "$PY" ] || [ ! -f "$EXTRACT" ]; then
    echo "evolution local-gate: extractor missing — not waking (no new local signal)." >&2
    echo '{"wakeAgent": false}'
    exit 0
fi

mkdir -p "$STATE_DIR"
if command -v timeout >/dev/null 2>&1; then
    DIGEST="$(timeout 30 "$PY" "$EXTRACT" 2>/dev/null)"
else
    DIGEST="$("$PY" "$EXTRACT" 2>/dev/null)"
fi

# Empty, whitespace-only, or an empty JSON object is "no signal".
_stripped="$(printf '%s' "$DIGEST" | tr -d '[:space:]')"
if [ -z "$_stripped" ] || [ "$_stripped" = "{}" ] || [ "$_stripped" = "null" ]; then
    echo "evolution local-gate: empty digest — skipping agent." >&2
    echo '{"wakeAgent": false}'
    exit 0
fi

if [ -f "$LAST" ]; then
    if cmp -s <(printf '%s' "$DIGEST") "$LAST" 2>/dev/null; then
        echo "evolution local-gate: digest unchanged since last run — skipping agent." >&2
        echo '{"wakeAgent": false}'
        exit 0
    fi
fi

printf '%s' "$DIGEST" > "$LAST"
printf '\n## Introspection Digest\n%s\n' "$DIGEST"
echo '{"wakeAgent": true}'
exit 0
