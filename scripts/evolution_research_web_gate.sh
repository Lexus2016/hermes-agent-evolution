#!/usr/bin/env bash
# Evolution research web-health preflight gate (issue #108).
#
# WHY: the research stage's web path can be dead (firecrawl HTTP 402 / MCP
# circuit-break, DNS outage, quota exhaustion) with no health gate at stage
# start. A dead web surface left the stage free to emit a plausible-looking
# report — or, at best, rely on manual discipline to admit it was blocked.
#
# HOW: probe a few cheap, independent endpoints (the research feeds' hosts).
#   - All probes fail  -> write a BLOCKED report + a degraded flag, then
#                        print {"wakeAgent": false} so no LLM tokens are spent
#                        and no plausible-looking findings can be fabricated.
#   - Any probe works  -> clear the degraded flag and fall through to the
#                        standard write-access gate (evolution_access_gate.sh),
#                        which owns the final wake decision.
#
# The scheduler's wake-gate contract: the LAST stdout line must be the JSON.
# We always exit 0 so the printed JSON — not the exit code — is the single
# source of truth (a nonzero exit would wake the agent unconditionally).
set +e

ENVF="${HERMES_HOME:-$HOME/.hermes}/.env"
[ -f "$ENVF" ] && { set -a; . "$ENVF" 2>/dev/null; set +a; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESEARCH_DIR="${HERMES_HOME:-$HOME/.hermes}/evolution/research"
DATE="$(date +%F)"
STAMP="$(date '+%Y-%m-%d %H:%M:%S %Z')"
FLAG="$RESEARCH_DIR/.web-degraded"

# Probe list: independent hosts behind the research stage's web surface.
PROBES=(
    "https://arxiv.org"
    "https://huggingface.co"
    "https://duckduckgo.com"
)

_results=""
_ok=0
for url in "${PROBES[@]}"; do
    if curl -fsS -m 8 -o /dev/null "$url" 2>/dev/null; then
        _ok=1
        _results="$_results $url=ok"
    else
        _results="$_results $url=FAIL"
    fi
done

if [ "$_ok" = "0" ]; then
    # Web surface is down — fail LOUDLY: durable BLOCKED report + degraded
    # flag, and skip the agent entirely.
    mkdir -p "$RESEARCH_DIR"
    cat > "$RESEARCH_DIR/$DATE.md" <<EOF
# Evolution Research Report — $DATE

**Status: BLOCKED — web surface unreachable at research-stage start (issue #108).**

The web-health preflight failed for every probed endpoint at $STAMP:
- https://arxiv.org — FAIL
- https://huggingface.co — FAIL
- https://duckduckgo.com — FAIL

No research was attempted and no findings were produced. The degraded flag
(\`$FLAG\`) records this cycle as web-degraded so sibling stages know there is
no fresh web-derived signal. Retry at the next scheduled slot; do NOT consume
stale or fabricated findings from this cycle.
EOF
    date -u +"%Y-%m-%dT%H:%M:%SZ" > "$FLAG"
    echo "evolution research web-gate: ALL web probes FAILED ($_results) — wrote BLOCKED report $RESEARCH_DIR/$DATE.md, skipping agent." >&2
    echo '{"wakeAgent": false}'
    exit 0
fi

# Web is fine — clear any previous degraded flag so the signal is stateful.
rm -f "$FLAG" 2>/dev/null

# Fall through to the standard write-access gate, which prints its own status
# line(s) and the final {"wakeAgent": true|false} JSON.
if [ -f "$SCRIPT_DIR/evolution_access_gate.sh" ]; then
    # shellcheck disable=SC1090
    ( source "$SCRIPT_DIR/evolution_access_gate.sh" )
    exit 0
fi

echo "evolution research web-gate: web OK ($_results) but evolution_access_gate.sh not found — cannot confirm write access, not waking" >&2
echo '{"wakeAgent": false}'
