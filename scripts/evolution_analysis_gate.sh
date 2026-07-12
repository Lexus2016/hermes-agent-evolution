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
# decision logic (and its tests) live in exactly one place. `source` inside
# a `( ... )` subshell (not `exec bash ...`, not a bare top-level `source`):
# a subshell forks THIS already-running interpreter (no execve, no PATH
# lookup needed — unlike spawning a nested `bash` binary, which would need
# to resolve `bash` via PATH, possibly empty/restricted under a locked-down
# cron PATH or this script's own test harness), while also containing the
# access gate's `.env` exports (it sources `$HERMES_HOME/.env` with `set -a`)
# to the subshell instead of leaking them into this script's own remaining
# environment. The subshell's stdout still flows straight through to ours,
# so its last line is still the `{"wakeAgent": ...}` JSON this script's
# contract requires as ITS last line too.
#
# `exit 0` here (NOT `exit $?`) is deliberate, not exit-code masking: the
# scheduler's wake-gate contract (cron/scheduler.py `_run_job_script` +
# `_parse_wake_gate`) treats a NONZERO/failed script run as "gate could not
# run" and wakes the agent UNCONDITIONALLY, ignoring any JSON it printed —
# the opposite of fail-closed. Propagating a hypothetical future nonzero
# exit from evolution_access_gate.sh (today it always ends in a successful
# `echo`, so this is currently moot) would flip a real failure from "don't
# wake" into "wake anyway", which is the one outcome this whole gate exists
# to prevent. Exiting 0 unconditionally keeps the printed JSON — not the
# process exit code — as the single source of truth for the wake decision.
if [ -f "$SCRIPT_DIR/evolution_access_gate.sh" ]; then
    # shellcheck disable=SC1090
    ( source "$SCRIPT_DIR/evolution_access_gate.sh" )
    exit 0
fi

# Fallback: the generic write-access gate isn't installed alongside us — a
# degraded install (register_evolution_cron.py's _install_access_gate runs
# unconditionally on every registration, so this should always be present).
# We cannot confirm write access without it, so fail CLOSED: do not wake the
# LLM agent. Phase 0's local-triage output above already stands as this
# cycle's result, and no tokens are spent on work that might not even be
# pushable.
echo "evolution-analysis-gate: evolution_access_gate.sh not found — cannot confirm write access, not waking" >&2
echo '{"wakeAgent": false}'
