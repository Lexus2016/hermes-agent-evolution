#!/bin/bash
# Evolution-analysis wake-gate (#910).
#
# WHY: evolution-analysis has two independent halves — a LOCAL, file-based
# triage pass (reads issues/introspection/research sidecars, writes
# analysis/YYYY-MM-DD.json) that needs no GitHub token at all, and a
# GitHub-dependent full pass (Phase 1: `gh issue list`, triage/reject/close)
# that needs write access to the repo. Before this fix, evolution-analysis
# was registered WITHOUT its own gate script, so it fell back to the generic
# `evolution_access_gate.sh` default: a job-wide wake-gate that skips the
# LLM agent entirely whenever GitHub write access is unavailable. That meant
# the local-only triage pass never even got a chance to run — the pipeline
# accumulated backlog but produced no analysis/*.json output on days the
# private token was missing/scoped-down (the exact MERGED_ZERO symptom this
# issue reports).
#
# HOW: this gate ALWAYS runs the local triage script first (unconditionally,
# best-effort — a failure here must never block the wake-gate decision below),
# so `analysis/YYYY-MM-DD.json` exists for today regardless of GitHub access.
# It then delegates the wake decision to the same write-access check every
# other evolution stage uses: wake the LLM agent (for the richer, GitHub-
# dependent Phase 1 pass) only when write access is confirmed; otherwise the
# local-only pass already written stands as this cycle's output.
set +e

# Resolve our own directory using bash parameter expansion only — NOT
# `dirname`/`cd`/`pwd` (external commands that need PATH resolution). This
# script must locate its sibling scripts even when PATH is empty/restricted
# (the same isolated-PATH convention `tests/scripts/test_evolution_access_gate.py`
# uses), so no external binary can be on the critical path here.
_src="${BASH_SOURCE[0]}"
case "$_src" in
    */*) SCRIPT_DIR="${_src%/*}" ;;
    *) SCRIPT_DIR="." ;;
esac

# Phase 0 — local (file-based) triage. No GitHub API calls, no token
# required. Runs unconditionally, before the write-access check, so it can
# never be skipped by a missing/scoped-down token.
if command -v python3 >/dev/null 2>&1 && [ -f "$SCRIPT_DIR/evolution_local_triage.py" ]; then
    python3 "$SCRIPT_DIR/evolution_local_triage.py" 2>&1 || true
else
    echo "evolution-analysis-gate: python3 or evolution_local_triage.py unavailable — skipping local triage" >&2
fi

# Phase 1 gate — reuse the generic write-access wake-gate verbatim so the
# decision logic (and its tests) live in exactly one place. `source` (not
# `exec bash ...`) on purpose: spawning a nested `bash` would need to resolve
# the `bash` binary via PATH, which can be empty/restricted (e.g. under a
# locked-down cron PATH, or this script's own test harness) — sourcing runs
# it in THIS already-running interpreter, no new process, no PATH lookup.
# Its own last stdout line is the `{"wakeAgent": ...}` JSON this script's
# contract requires as ITS last line too.
if [ -f "$SCRIPT_DIR/evolution_access_gate.sh" ]; then
    # shellcheck disable=SC1090
    source "$SCRIPT_DIR/evolution_access_gate.sh"
    exit 0
fi

# Fallback: no access gate installed alongside us — default to waking the
# agent (matches the scheduler's "gate absent -> wake" contract).
echo "evolution-analysis-gate: evolution_access_gate.sh not found — defaulting to wake" >&2
echo '{"wakeAgent": true}'
