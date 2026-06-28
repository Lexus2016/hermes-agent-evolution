#!/bin/bash
# Evolution wake-gate: only wake the LLM agent if the authenticated GitHub
# account has WRITE (push) access to the evolution repo.
#
# WHY: the whole evolution cycle (research -> issues -> analysis ->
# implementation -> integration) only has value if the agent can PUSH to GitHub
# (open issues / PRs, merge). Reachability alone is NOT enough: a read-only
# account passes `gh api user`, so it would wake the agent, let it spend tokens
# on research/issues/analysis, and only fail at the implementation push — with
# nothing durable to show. (This actually happened: a read-only account woke
# the agent, ran every stage, then could not push a single branch or PR, leaving
# a trail of "push:false" blocked-implementation comments.) Without WRITE the
# user simply runs plain Hermes from our repo and receives our updates; the
# agent should NOT burn LLM tokens / web-search quota trying to self-evolve with
# no outlet.
#
# HOW: Hermes cron treats the LAST stdout line as a wake gate. Printing
# `{"wakeAgent": false}` skips the agent entirely — no LLM run, no delivery.
# The repo-permission check is one cheap REST call, so it costs ~nothing to gate
# the expensive agent run behind it. The repo is GITHUB_EVOLUTION_REPO (same
# default as scripts/setup_evolution_bot.sh); write is push|maintain|admin.
set +e

ENVF="${HERMES_HOME:-$HOME/.hermes}/.env"
[ -f "$ENVF" ] && { set -a; . "$ENVF" 2>/dev/null; set +a; }

REPO="${GITHUB_EVOLUTION_REPO:-Lexus2016/hermes-agent-evolution}"

# A repo's `permissions` object is only populated for the AUTHENTICATED viewer,
# so `"push":true` there means the current token can push to REPO. push,
# maintain and admin all imply push access.
_has_write() {
    case "$1" in
        *'"push":true'*|*'"maintain":true'*|*'"admin":true'*) return 0 ;;
        *'"push": true'*|*'"maintain": true'*|*'"admin": true'*) return 0 ;;
    esac
    return 1
}

ok=0
# 1) persistent gh auth (~/.config/gh) — the canonical path on a configured server
if command -v gh >/dev/null 2>&1 && gh api user --jq .login >/dev/null 2>&1; then
    perms="$(gh api "repos/$REPO" --jq '.permissions // {}' 2>/dev/null || echo '{}')"
    _has_write "$perms" && ok=1
fi
# 2) fall back to a raw token from the env file
if [ "$ok" = "0" ] && [ -n "${GITHUB_PRIVATE_TOKEN:-}${GITHUB_TOKEN:-}" ]; then
    _tok="${GITHUB_PRIVATE_TOKEN:-$GITHUB_TOKEN}"
    if command -v curl >/dev/null 2>&1; then
        perms="$(curl -fsS -H "Authorization: Bearer ${_tok}" \
            -H "Accept: application/vnd.github+json" \
            "https://api.github.com/repos/$REPO" 2>/dev/null)"
        _has_write "$perms" && ok=1
    fi
    unset _tok
fi

if [ "$ok" = "1" ]; then
    echo "evolution access-gate: write access to $REPO confirmed — waking agent."
    echo '{"wakeAgent": true}'
else
    echo "evolution access-gate: no WRITE access to $REPO — skipping agent to avoid burning LLM tokens / web-search quota on work that cannot be pushed (a reachable read-only account still cannot open branches/PRs). Grant the authenticated account push access on $REPO (or run as a writer), then 'gh auth login'."
    echo '{"wakeAgent": false}'
fi
