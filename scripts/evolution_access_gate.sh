#!/bin/bash
# Evolution wake-gate: only wake the LLM agent if GitHub is actually reachable.
#
# WHY: the whole evolution cycle (research -> issues -> analysis ->
# implementation -> integration) only has value if the agent can POST to GitHub
# (open issues / PRs, merge). With no GitHub access the agent can produce nothing
# durable — so running research/issues/etc would just BURN LLM tokens and
# web-search quota for nothing. In that case the user simply runs plain Hermes
# from our repo and receives our updates; the agent should NOT spend money trying
# to self-evolve without an outlet.
#
# HOW: Hermes cron treats the LAST stdout line as a wake gate. Printing
# `{"wakeAgent": false}` skips the agent entirely — no LLM run, no delivery.
# This check is cheap (one gh/REST call), so it costs ~nothing to gate the
# expensive agent run behind it.
set +e

ENVF="${HERMES_HOME:-$HOME/.hermes}/.env"
[ -f "$ENVF" ] && { set -a; . "$ENVF" 2>/dev/null; set +a; }

ok=0
# 1) persistent gh auth (~/.config/gh) — the canonical path on a configured server
if command -v gh >/dev/null 2>&1 && gh api user --jq .login >/dev/null 2>&1; then
    ok=1
fi
# 2) fall back to a raw token from the env file
if [ "$ok" = "0" ] && [ -n "${GITHUB_PRIVATE_TOKEN:-}${GITHUB_TOKEN:-}" ]; then
    _tok="${GITHUB_PRIVATE_TOKEN:-$GITHUB_TOKEN}"
    if command -v curl >/dev/null 2>&1; then
        curl -fsS -H "Authorization: Bearer ${_tok}" \
            -H "Accept: application/vnd.github+json" \
            https://api.github.com/user >/dev/null 2>&1 && ok=1
    fi
    unset _tok
fi

if [ "$ok" = "1" ]; then
    echo "evolution access-gate: GitHub reachable — waking agent."
    echo '{"wakeAgent": true}'
else
    echo "evolution access-gate: no GitHub access — skipping agent to avoid burning LLM tokens / web-search quota. Add a GitHub token to ${ENVF} (and run 'gh auth login') to enable self-evolution."
    echo '{"wakeAgent": false}'
fi
