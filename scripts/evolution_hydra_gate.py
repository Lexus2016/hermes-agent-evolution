#!/usr/bin/env python3
"""Hydra gate — pre-check that saves tokens by suppressing the LLM orchestrator
when the evolution knowledge pool has no fresh material or the pipeline is
in a halted state.

Contract (Hermes cron gate):
  Last stdout line = wake signal.
  ``{"wakeAgent": false}`` → skips the LLM agent (no tokens spent).
  ``{"wakeAgent": true}``  → LLM agent fires to dispatch subagents.

The gate checks upstream→downstream staleness for the 7 evolution stages and
returns false (sleep) when every consumer is ahead of or equal to its producer.
It also sleeps when ``halt-state.txt`` is present, preventing expensive LLM work
on a broken pipeline (#770).

A per-edge dispatch ledger (#1305) suppresses false-positive wake-ups: once the
gate has dispatched for an edge on a given day, subsequent ticks on the same day
do NOT re-fire unless the upstream stage produced NEW output (a strictly newer
mtime) since the last dispatch. Without this ledger the raw-mtime check re-fires
on every tick — observed at 20+ wasted orchestrator wake-ups/day on the
integration→upstream-sync edge alone (#1305).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple

# Ledger entries older than this are pruned to keep the file bounded. Entries are
# keyed by day, so a week of history is ample for debugging a "did the gate fire
# today?" question without growing unbounded over months.
_LEDGER_PRUNE_DAYS = 7


def _hot_path() -> Path:
    """Canonical evolution output directory."""
    env = os.environ.get("EVOLUTION_PROFILE_DIR", "")
    if env:
        return Path(env)
    return Path.home() / ".hermes" / "evolution"


def _mtime(path: Path) -> float:
    """Modified time, or 0 if missing."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _today() -> str:
    return datetime.now().date().isoformat()


def _today_paths(evo_dir: Path, stage: str, ext: str = ".json") -> Tuple[Path, Path]:
    """Return (stage_today, stage_alt) — paths for today's output in json and
    possible markdown format."""
    return (
        evo_dir / stage / f"{_today()}{ext}",
        evo_dir / stage / f"{_today()}.md",
    )


def _latest_output(evo_dir: Path, stage: str) -> float:
    """Return the latest mtime of any output file for this stage (today)."""
    json_path, md_path = _today_paths(evo_dir, stage)
    return max(_mtime(json_path), _mtime(md_path))


# ---------------------------------------------------------------------------
# Dispatch ledger (#1305) — stop false-positive re-wake-ups on the same day
# ---------------------------------------------------------------------------


def _ledger_path(evo_dir: Path) -> Path:
    """Path to the per-edge dispatch ledger (a small JSON file)."""
    return evo_dir / ".hydra-dispatch-ledger.json"


def _read_ledger(evo_dir: Path) -> Dict[str, dict]:
    """Load the dispatch ledger, or ``{}`` if absent / corrupt.

    Schema: ``{"<edge>": {"date": "YYYY-MM-DD", "upstream_mtime": <float>,
    "verdict": <str|None>}}``. Corrupt files are treated as empty rather than
    raising — a corrupt ledger must never block a genuine wake-up (fail-open).
    """
    path = _ledger_path(evo_dir)
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_ledger(evo_dir: Path, ledger: Dict[str, dict]) -> None:
    """Persist the ledger atomically. Best-effort: a write failure is logged to
    stderr but never raised — the ledger is an optimization, not a correctness
    requirement, so a failed write degrades to "may re-wake once" rather than
    crashing the gate."""
    path = _ledger_path(evo_dir)
    tmp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(ledger), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        # Don't leave a .tmp littering the dir on a failed rename.
        try:
            tmp.unlink()
        except OSError:
            pass
        print(f"[hydra-gate] ledger write failed ({exc}) — continuing", file=sys.stderr)


def _already_dispatched_today(evo_dir: Path, edge: str, upstream_mtime: float) -> bool:
    """Return True if this edge was already dispatched today AND the upstream
    stage has NOT produced newer output since that dispatch.

    A NEW upstream mtime (strictly greater than the one recorded at dispatch
    time) means genuinely fresh material arrived → re-fire. The same or older
    mtime means the edge is a repeat of an already-adjudicated state → suppress.
    """
    entry = _read_ledger(evo_dir).get(edge)
    if not isinstance(entry, dict):
        return False
    if entry.get("date") != _today():
        # Stale entry from a prior day — that day's verdict doesn't carry over.
        return False
    recorded_mtime = entry.get("upstream_mtime")
    if not isinstance(recorded_mtime, (int, float)):
        return False
    # Suppress only when upstream has not advanced since the last dispatch.
    # A strict-newer mtime is genuine new material and must re-fire.
    return upstream_mtime <= float(recorded_mtime)


def _record_dispatch(evo_dir: Path, edge: str, upstream_mtime: float) -> None:
    """Record that the gate dispatched for ``edge`` at ``upstream_mtime`` today.

    Also opportunistically prunes entries older than ``_LEDGER_PRUNE_DAYS`` so
    the file stays small over time.
    """
    ledger = _read_ledger(evo_dir)
    ledger[edge] = {
        "date": _today(),
        "upstream_mtime": upstream_mtime,
        "verdict": None,
    }
    # Prune stale entries (older than the prune window). Keep today's + the
    # last week for debugging "did the gate already see this edge?".
    cutoff = (datetime.now() - timedelta(days=_LEDGER_PRUNE_DAYS)).date().isoformat()
    pruned = {
        k: v
        for k, v in ledger.items()
        if isinstance(v, dict) and str(v.get("date", "")) >= cutoff
    }
    _write_ledger(evo_dir, pruned)


def _has_upstream_freshness(
    evo_dir: Path,
    upstream_stage: str,
    downstream_stage: str,
) -> bool:
    """Return True if the upstream stage has fresher output than the downstream
    stage's latest output — meaning the downstream stage has work it hasn't
    processed yet. Missing downstream = definitely fresh."""
    up_mtime = _latest_output(evo_dir, upstream_stage)
    down_mtime = _latest_output(evo_dir, downstream_stage)

    # No downstream output yet and upstream has output → fresh
    if down_mtime == 0 and up_mtime > 0:
        return True
    # Upstream output is more recent than downstream's → fresh
    return up_mtime > down_mtime


def _check_github_write_access() -> Tuple[bool, str]:
    """Check if the authenticated GitHub account has WORKING access to the
    evolution repo.  The `.permissions` API endpoint is unreliable for repo
    owners — it often returns ``push: false`` even when the token has full
    ``repo`` scope and can write.  Instead we verify operability directly:

      1.  ``gh auth status`` — confirms the CLI is authenticated.
      2.  ``gh issue list`` — confirms the CLI can READ the repo.
      3.  Token scopes include ``repo`` — indicates write capability.

    If gh CLI works and the repo is reachable, assume WRITE access (a
    ``ghp_`` token with ``repo`` scope inherently has push capability).
    """
    repo = os.environ.get("GITHUB_EVOLUTION_REPO", "Lexus2016/hermes-agent-evolution")

    # 1) gh CLI — auth + read check
    try:
        r = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode != 0:
            return False, "gh CLI not authenticated"
    except (OSError, subprocess.TimeoutExpired):
        return False, "gh CLI unreachable"

    # 2) Can we read the repo? (list issues = lightweight read check)
    try:
        r = subprocess.run(
            ["gh", "issue", "list", "--repo", repo, "--limit", "1", "--json", "number"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode != 0:
            return False, f"cannot read repo {repo}: {r.stderr.strip()}"
    except (OSError, subprocess.TimeoutExpired):
        return False, f"repo {repo} unreachable"

    # 3) Token scope check — a ghp_ or githu_ token with 'repo' scope can write.
    try:
        r = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        user = r.stdout.strip() if r.returncode == 0 else "?"
    except (OSError, subprocess.TimeoutExpired):
        user = "?"

    return True, f"gh CLI {user}: auth OK, repo {repo} readable, write assumed"


def _check_halt(evo_dir: Path) -> Tuple[bool, Path]:
    """Return (halted, halt_file_path) for the evolution pipeline.

    If ``halt-state.txt`` exists, the pipeline has produced zero automated
    deliverables for 5+ consecutive cycles and zero selections for 3+ cycles.
    Expensive LLM stages (research, analysis, implementation) must sleep
    until the halt file is manually cleared (#770).
    """
    halt_file = evo_dir / "halt-state.txt"
    try:
        return halt_file.exists(), halt_file
    except OSError:
        return False, halt_file


def _check_pool(evo_dir: Path) -> Dict[str, bool]:
    """Check all upstream→downstream pairs for staleness. Returns per-pair
    freshness map."""
    # Upstream → downstream pairs in the evolution pipeline
    pairs = [
        ("research", "issues"),  # new findings → need issues
        ("issues", "analysis"),  # new issues → need analysis
        ("introspection", "analysis"),  # new patterns → need analysis
        ("analysis", "implementation"),  # new selections → need impl
        ("implementation", "integration"),  # new PRs → need merge
        ("integration", "upstream-sync"),  # new merges → need sync
    ]

    results: Dict[str, bool] = {}
    for up, down in pairs:
        fresh = _has_upstream_freshness(evo_dir, up, down)
        results[f"{up}→{down}"] = fresh
    return results


def _has_work(evo_dir: Path) -> Tuple[bool, str]:
    """Core gate logic. Returns (has_work, reason).

    Freshness edges that were already dispatched today (with no upstream
    advancement since) are suppressed via the dispatch ledger (#1305).
    """
    now_ts = datetime.now().timestamp()

    freshness = _check_pool(evo_dir)
    # Map edge label → upstream stage name, so we can read the upstream mtime
    # the ledger compares against. Pairs are defined in _check_pool.
    edge_upstream = {
        "research→issues": "research",
        "issues→analysis": "issues",
        "introspection→analysis": "introspection",
        "analysis→implementation": "analysis",
        "implementation→integration": "implementation",
        "integration→upstream-sync": "integration",
    }
    fresh_pairs = []
    suppressed = []
    for pair, is_fresh in freshness.items():
        if not is_fresh:
            continue
        up_stage = edge_upstream.get(pair)
        if up_stage is None:
            # Unknown edge label — can't ledger it; fire to be safe.
            fresh_pairs.append(pair)
            continue
        up_mtime = _latest_output(evo_dir, up_stage)
        if _already_dispatched_today(evo_dir, pair, up_mtime):
            # Already dispatched today for this exact upstream state — suppress.
            suppressed.append(pair)
        else:
            fresh_pairs.append(pair)

    if fresh_pairs:
        # Record the dispatch for each firing edge so a same-day re-tick with
        # no upstream advancement is suppressed next time.
        for pair in fresh_pairs:
            up_stage = edge_upstream[pair]
            _record_dispatch(evo_dir, pair, _latest_output(evo_dir, up_stage))
        return True, f"fresh material: {', '.join(fresh_pairs)}"

    # Time-based triggers: root stages that should run periodically even when
    # the pool is settled — they generate the material that downstream stages
    # consume. With the stage crons paused, these are the Hydra's heartbeat.
    time_triggers = {
        "research": 24,  # daily scan of AI agent landscape
        "introspection": 24,  # daily session analysis
        "upstream-sync": 28,  # daily fork sync (slightly wider window)
    }
    for stage, max_interval_h in time_triggers.items():
        last_mtime = _latest_output(evo_dir, stage)
        if last_mtime > 0:
            age_hours = (now_ts - last_mtime) / 3600
            if age_hours >= max_interval_h:
                return (
                    True,
                    f"time trigger: {stage} overdue ({age_hours:.0f}h, max {max_interval_h}h)",
                )
        else:
            # Stage has NEVER produced output — definitely needs to run.
            return True, f"time trigger: {stage} not yet run today"

    # Safety net: if NO stage has produced output in the last 12 hours,
    # fire the Hydra anyway — something might be stuck.
    stages = [
        "research",
        "issues",
        "introspection",
        "analysis",
        "implementation",
        "integration",
        "upstream-sync",
    ]
    latest_any = max(_latest_output(evo_dir, s) for s in stages)
    if latest_any > 0:
        age_hours = (now_ts - latest_any) / 3600
        if age_hours >= 12:
            return True, f"safety wake: {age_hours:.0f}h since last output"

    if suppressed:
        return False, (
            "pool settled — no fresh material "
            f"({len(suppressed)} edge(s) already dispatched today: "
            f"{', '.join(suppressed)})"
        )
    return False, "pool settled — no fresh material"


def main() -> int:
    evo_dir = _hot_path()

    # 1) Check GitHub write access first — without it, no evolution work
    #    can be pushed to GitHub (issues, PRs, merges all fail).
    gh_ok, gh_reason = _check_github_write_access()
    if not gh_ok:
        print(f"[hydra-gate] {gh_reason} — sleeping")
        print('{"wakeAgent": false}')
        return 0

    # 2) Check for pipeline halt-state BEFORE any LLM work
    halted, halt_file = _check_halt(evo_dir)
    if halted:
        print(f"[hydra-gate] pipeline HALTED ({halt_file}) — sleeping")
        print('{"wakeAgent": false}')
        return 0

    # 3) Check knowledge pool for fresh material
    has_work, reason = _has_work(evo_dir)

    if has_work:
        print(f"[hydra-gate] {reason} — waking orchestrator")
        print('{"wakeAgent": true}')
    else:
        print(f"[hydra-gate] {reason} — sleeping")
        print('{"wakeAgent": false}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
