#!/usr/bin/env python3
"""Evolution meta-editor — bounded, deterministic procedure-adjustment proposals.

Issue #906 (AEvo): the evolution pipeline already computes signals about its
own health (``metrics.jsonl`` via ``evolution_funnel``), but that history is
archival — nothing in the pipeline's own PROCEDURE (e.g. the ``min_priority_score``
quality bar each stage's YAML hardcodes) ever reacts to it. This module gives
the pipeline ONE narrow, machine-checked lever: a deterministic aggregator that
reads that existing evidence and proposes a bounded adjustment to an explicit,
tiny registry of tunable parameters.

Deliberately narrow scope — this is NOT the "meta-agent rewrites its own
pipeline" framework the research issue sketches. That would require an LLM
judging procedure changes against held-out historical performance, which is
unsound to ship as auto-merged, auto-applied code (no sound way to validate an
LLM's own procedure edits before they run for real). Instead:

  * NO LLM. Every function here is a pure, deterministic transform of evidence
    the pipeline already writes (``metrics.jsonl``, read via
    ``evolution_funnel.summarize``).
  * NO auto-apply. This script only aggregates state and WRITES a proposal
    record to ``<evolution_dir>/meta/{date}.json`` — it never edits
    ``cron/evolution/*.yaml`` itself. Applying a proposal is a separate,
    human-reviewed action (mirrors ``evolution_postmortem_miner`` writing
    advisory rules, and ``evolution_ci_diagnosis`` opening child issues for
    complex cases instead of auto-fixing them).
  * Registry-bounded. ``propose_edits()`` can only ever suggest a field
    explicitly declared in ``TUNABLE_PARAMS``, each with a hard min/max/step
    that ``validate_proposal()`` enforces INDEPENDENTLY of the proposer's own
    arithmetic — a bug in ``propose_edits`` cannot produce a proposal that
    escapes the bound the registry declares.

Out of scope for this first increment (left for follow-up issues once this
primitive has real-world signal to justify it):
  * Consuming rubric-scorecard.jsonl / postmortem-rules.json as additional
    evidence (this increment reads only the funnel, the pipeline's most
    load-bearing existing signal).
  * A cooldown/hysteresis window to damp oscillating proposals — harmless
    today because nothing auto-applies a proposal, but would matter before
    any future auto-apply path.
  * An ``--apply`` mode that edits the YAML. Deliberately NOT built: applying
    a bounded proposal is exactly the step that turns "safe advisory data"
    into "pipeline behavior changed autonomously", which is the auto-merge
    risk this increment is scoped to avoid.

Pure functions + explicit IO boundary so it is import-safe and unit-testable,
matching the rest of the ``evolution_*`` script family.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from evolution_funnel import (  # noqa: E402
    is_evolution_halted,
    load_records,
    summarize,
)


def _resolve_repo_dir() -> Optional[Path]:
    """Locate the git repo to read the live cron/evolution/*.yaml values from.

    Duplicated locally rather than imported (matches evolution_watchdog's own
    copy, not evolution_funnel's): this script runs as a COPY under
    HERMES_HOME/scripts, outside the repo, so it cannot rely on importing
    another script's private helper staying in sync. Env override, then the
    in-tree location (when run from the repo), then the common server
    install / agent-clone paths. Returns None when none is a git repo — the
    caller then falls back to each TunableParam's registry default.
    """
    candidates = [
        os.environ.get("EVOLUTION_REPO_DIR"),
        str(Path(__file__).resolve().parent.parent),  # scripts/ -> repo root (in-tree)
        "/usr/local/lib/hermes-agent",
        str(Path.home() / "hermes-agent-evolution"),
    ]
    for cand in candidates:
        if cand and (Path(cand) / ".git").exists():
            return Path(cand)
    return None


# ---------------------------------------------------------------------------
# The registry — the ONLY edits this module may ever propose.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TunableParam:
    """One (file, section, key) triple this module is allowed to touch.

    ``min_value``/``max_value``/``step`` are the hard bound: a proposal that
    would move the value outside them, or by more than one ``step``, is
    invalid (see ``validate_proposal``) regardless of what proposed it.
    ``default`` is the fallback used when the live YAML value cannot be read
    (missing file, unparsable, key absent) — the registry's own last resort,
    never a network call or an LLM guess.
    """

    stage: str
    yaml_file: str
    section: str
    key: str
    min_value: float
    max_value: float
    step: float
    default: float


# All four stages share the same ``min_priority_score`` quality-bar concept
# (see cron/evolution/{research,issues,introspection}.yaml `limits.` and
# analysis.yaml `safety.` — same field name, different top-level section).
# Bounds [0.5, 0.9] keep the bar from ever falling to "accept anything" or
# rising to "accept almost nothing"; step 0.05 caps how much any single
# cycle's proposal can move it.
TUNABLE_PARAMS: Dict[str, TunableParam] = {
    "research.min_priority_score": TunableParam(
        "research", "research.yaml", "limits", "min_priority_score", 0.5, 0.9, 0.05, 0.7
    ),
    "issues.min_priority_score": TunableParam(
        "issues", "issues.yaml", "limits", "min_priority_score", 0.5, 0.9, 0.05, 0.7
    ),
    "introspection.min_priority_score": TunableParam(
        "introspection",
        "introspection.yaml",
        "limits",
        "min_priority_score",
        0.5,
        0.9,
        0.05,
        0.7,
    ),
    "analysis.min_priority_score": TunableParam(
        "analysis", "analysis.yaml", "safety", "min_priority_score", 0.5, 0.9, 0.05, 0.7
    ),
}

# Same reject_rate threshold evolution_funnel.summarize() already uses for its
# HIGH_REJECT_RATE flag — one number, one meaning, across the pipeline.
_HIGH_REJECT_RATE = 0.70
# New for this module: a low-reject-rate signal that the bar may be too
# strict. Deliberately conservative (well below the high threshold) so the
# two triggers can never both fire for the same state.
_LOW_REJECT_RATE = 0.20
# Require several cycles of evidence before proposing anything — a single
# noisy cycle must never justify moving a pipeline-wide threshold. Slightly
# higher than evolution_metrics.compute_health's >=3 (which only surfaces a
# flag for a human to read); this module computes a concrete number, so it
# asks for one more cycle of confidence.
_MIN_CYCLES_FOR_PROPOSAL = 5


def aggregate_state(evolution_dir: Path, last: int = 14) -> Dict[str, Any]:
    """Process-level state snapshot: the evidence this module reasons over.

    Deliberately minimal for this increment — reads only the funnel, the
    pipeline's most load-bearing existing signal. ``cycles`` reflects how much
    evidence backs the snapshot; callers must not propose edits when it is
    below ``_MIN_CYCLES_FOR_PROPOSAL``.
    """
    records = load_records(evolution_dir / "metrics.jsonl")
    summary = summarize(records, last)
    return {
        "cycles": summary["cycles"],
        "reject_rate": summary["reject_rate"],
        "merged_zero_streak": summary["merged_zero_streak"],
        "flags": summary["flags"],
        "halted": bool(is_evolution_halted(evolution_dir)),
    }


# ---------------------------------------------------------------------------
# Reading the CURRENT value of a tunable parameter out of the live YAML.
# ---------------------------------------------------------------------------


def read_current_value(
    repo_root: Optional[Path], param: TunableParam
) -> Optional[float]:
    """Read ``param``'s current value from its cron YAML. None on any failure
    (missing repo, missing file, unparsable YAML, missing key) — callers fall
    back to ``param.default`` so a read failure never crashes the job."""
    if repo_root is None:
        return None
    path = repo_root / "cron" / "evolution" / param.yaml_file
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return None
    except Exception:
        return None
    section = data.get(param.section)
    if not isinstance(section, dict):
        return None
    value = section.get(param.key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# Proposing edits — pure function of (state, current values, registry).
# ---------------------------------------------------------------------------


def propose_edits(
    state: Dict[str, Any],
    current_values: Dict[str, Optional[float]],
    params: Dict[str, TunableParam] = TUNABLE_PARAMS,
) -> List[Dict[str, Any]]:
    """Return zero or more bounded edit proposals for the given state.

    An empty list is the normal, expected outcome most cycles ("procedure
    looks fine, no changes proposed") — this is advisory data, not a job that
    must always produce output.
    """
    proposals: List[Dict[str, Any]] = []

    if state.get("halted"):
        return proposals  # a halted pipeline has a bigger problem than tuning
    if int(state.get("cycles", 0) or 0) < _MIN_CYCLES_FOR_PROPOSAL:
        return proposals

    reject_rate = state.get("reject_rate")
    if reject_rate is None:
        return proposals

    if reject_rate > _HIGH_REJECT_RATE:
        direction = "increase"
        reason = (
            f"reject_rate {reject_rate:.0%} > {_HIGH_REJECT_RATE:.0%} over "
            f"{state['cycles']} cycles — raise the quality bar"
        )
    elif (
        reject_rate < _LOW_REJECT_RATE
        and int(state.get("merged_zero_streak", 0) or 0) == 0
    ):
        direction = "decrease"
        reason = (
            f"reject_rate {reject_rate:.0%} < {_LOW_REJECT_RATE:.0%} over "
            f"{state['cycles']} cycles and merges are healthy — the bar may "
            "be filtering out viable proposals; ease it"
        )
    else:
        return proposals

    for name in sorted(params):
        param = params[name]
        current = current_values.get(name)
        if current is None:
            current = param.default
        if direction == "increase":
            candidate = _clamp(current + param.step, param.min_value, param.max_value)
        else:
            candidate = _clamp(current - param.step, param.min_value, param.max_value)
        candidate = round(candidate, 4)
        if candidate == current:
            continue  # already at the bound, or no-op — nothing to propose

        proposal = {
            "name": name,
            "stage": param.stage,
            "yaml_file": param.yaml_file,
            "section": param.section,
            "key": param.key,
            "current": current,
            "proposed": candidate,
            "delta": round(candidate - current, 4),
            "reason": reason,
            "evidence": {
                "cycles": state["cycles"],
                "reject_rate": reject_rate,
                "merged_zero_streak": state.get("merged_zero_streak"),
            },
        }
        ok, err = validate_proposal(proposal, param)
        if ok:
            proposals.append(proposal)
        # An invalid proposal here would be a bug in this very function (the
        # candidate is already clamped) — silently dropping it is defense in
        # depth, not the expected path. Tests cover both sides directly.

    return proposals


# ---------------------------------------------------------------------------
# Validation gate — independent of propose_edits' own arithmetic.
# ---------------------------------------------------------------------------


def validate_proposal(
    proposal: Dict[str, Any], param: TunableParam
) -> Tuple[bool, Optional[str]]:
    """Check ``proposal`` against ``param``'s declared bounds. Returns
    ``(True, None)`` when valid, ``(False, reason)`` otherwise. This is the
    module's validation gate: it re-checks bounds from scratch rather than
    trusting whatever computed the proposal."""
    if (
        proposal.get("stage") != param.stage
        or proposal.get("yaml_file") != param.yaml_file
        or proposal.get("section") != param.section
        or proposal.get("key") != param.key
    ):
        return False, "proposal identity does not match the declared TunableParam"

    proposed = proposal.get("proposed")
    if not isinstance(proposed, (int, float)) or isinstance(proposed, bool):
        return False, "proposed value is not numeric"
    if proposed < param.min_value or proposed > param.max_value:
        return False, (
            f"proposed {proposed} outside bounds [{param.min_value}, {param.max_value}]"
        )

    current = proposal.get("current")
    if not isinstance(current, (int, float)) or isinstance(current, bool):
        return False, "current value is not numeric"
    if abs(proposed - current) > param.step + 1e-9:
        return False, (
            f"proposed change {abs(proposed - current):.4f} exceeds the bounded "
            f"step {param.step}"
        )
    if proposed == current:
        return False, "proposal is a no-op"

    return True, None


def validate_proposal_against_registry(
    proposal: Dict[str, Any], params: Dict[str, TunableParam] = TUNABLE_PARAMS
) -> Tuple[bool, Optional[str]]:
    """Top-level validation gate: is ``proposal["name"]`` even a registered,
    legal edit target? Then defers to ``validate_proposal`` for the bounds
    check. Any name outside the registry is rejected outright — the registry
    is the only source of truth for what may ever be proposed."""
    name = proposal.get("name")
    param = params.get(name) if isinstance(name, str) else None
    if param is None:
        return False, f"'{name}' is not a registered tunable parameter"
    return validate_proposal(proposal, param)


# ---------------------------------------------------------------------------
# Rendering + persistence.
# ---------------------------------------------------------------------------


def format_proposals(
    date: str, state: Dict[str, Any], proposals: List[Dict[str, Any]]
) -> str:
    """One-line, log/agent-friendly rendering, matching the funnel/health
    sidecar convention (readable by stages without a terminal toolset)."""
    if not proposals:
        return (
            f"[evolution-meta] {date}: {state['cycles']} cycles, "
            f"reject_rate={_pct(state.get('reject_rate'))} — no procedure "
            "changes proposed"
        )
    parts = [f"{p['name']}: {p['current']}->{p['proposed']}" for p in proposals]
    return (
        f"[evolution-meta] {date}: {state['cycles']} cycles, "
        f"reject_rate={_pct(state.get('reject_rate'))} — proposing " + ", ".join(parts)
    )


def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def write_meta_record(evolution_dir: Path, date: str, record: Dict[str, Any]) -> Path:
    """Write the proposal record for ``date``, overwriting any prior run for
    the same date (idempotent re-runs), matching the date-keyed stage report
    convention (analysis/{date}.json etc.)."""
    out_dir = evolution_dir / "meta"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{date}.json"
    out_path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    return out_path


def main(argv: List[str]) -> int:
    evolution_dir = Path(
        os.environ.get(
            "EVOLUTION_PROFILE_DIR",
            str(Path.home() / ".hermes" / "evolution"),
        )
    )

    date = argv[1] if len(argv) > 1 and not argv[1].startswith("-") else ""
    date = date or os.environ.get("EVOLUTION_META_DATE", "")
    if not date:
        date = datetime.now(timezone.utc).date().isoformat()

    state = aggregate_state(evolution_dir)

    repo_root = _resolve_repo_dir()
    current_values = {
        name: read_current_value(repo_root, param)
        for name, param in TUNABLE_PARAMS.items()
    }

    proposals = propose_edits(state, current_values)
    # Independent re-validation before anything touches disk — defense in
    # depth so a future refactor of propose_edits cannot silently smuggle an
    # out-of-bounds proposal into the written record.
    proposals = [p for p in proposals if validate_proposal_against_registry(p)[0]]

    record = {"date": date, "state": state, "proposals": proposals}
    out_path = write_meta_record(evolution_dir, date, record)

    summary_line = format_proposals(date, state, proposals)
    try:
        (evolution_dir / "meta-proposals.txt").write_text(
            summary_line + "\n", encoding="utf-8"
        )
    except OSError:
        pass

    print(f"{summary_line} (wrote {out_path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
