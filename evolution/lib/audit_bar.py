"""Non-stationary audit bar — the Red-Queen rising audit standard (issue #63).

A frozen audit standard stalls a self-improving system: once an audit has
"accepted" the current state, an unchanged checklist keeps re-confirming it
while new drift slips past (the Red Queen Gödel Machine, arXiv 2606.26294:
the bar must rise as the agent improves).  This module makes the audit bar
non-stationary with four mechanisms:

* **Calibration traps** — the previous audit's accepted observations are
  carried forward into the next audit's prompt as KNOWN/ACCEPTED entries.
  The auditor must (a) recognise each trap as known — never re-report it as
  new drift — and (b) still identify NEW drift that no trap covers.
* **Miss counting** — each audit where drift occurred but the auditor
  reported nothing increments a consecutive-miss counter.
* **Rubric rotation** — when the miss counter crosses a configurable
  threshold, the audit rubric (the checklist/prompt variant the auditor is
  told to walk) rotates to the next variant and the counter resets: the bar
  rises, the standard is no longer frozen.
* **Persisted state** — accepted observations, miss count, and the active
  rubric variant live in a JSON state file under the evolution dir, the same
  convention as the watchdog's alert state and the rubric-forge sidecars
  (fail-open on read, best-effort atomic write).

Pure engine + explicit IO, mirroring the repo's other evolution lib modules
(``rubric_forge``, ``rubric_heldout_gate``): every rule is a pure function
unit-testable without touching the disk; ``load_state`` / ``save_state`` /
``state_file_path`` are the only IO.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Consecutive audits with drift-but-no-report before the rubric rotates.
# Sensible default for a daily audit: two silent misses in a row.
DEFAULT_MISS_THRESHOLD = 2

# Observation classification results.
TRAP = "trap"  # known id, unchanged content — already accepted
CHANGED = "changed"  # known id, drifted content — NEW drift on a known item
NEW = "new"  # unknown id — NEW drift

# Keys that define an observation's identity (kind + name); everything else
# is content that must match for the observation to count as unchanged.
_IDENTITY_KEYS = frozenset({"kind", "name"})

# ── Audit rubric variants (the checklist the auditor must walk) ────────────
# Rotation cycles through these in order; each variant re-frames the audit so
# a frozen checklist cannot keep rubber-stamping the accepted baseline.

AUDIT_RUBRIC_VARIANTS: tuple[str, ...] = (
    # v1 — baseline discipline (the shipped default).
    """1. CORPUS COVERAGE — every skill in the usage sidecar is audited this
   run; a skill that is silently skipped is drift.
2. VERDICT CORRECTNESS — each keep / consolidate / demote / remove verdict
   follows the leave-one-out utility rule (recency-weighted share, token
   overlap, activity floor); a verdict that contradicts the rule is drift.
3. REDUNDANCY — overlap is measured on token sets, never guessed from names.
4. CONSEQUENCE — a demote verdict is stamped into the curator sidecar with
   --apply so the audit changes state; a report-only run that forgets the
   stamp is a finding.
5. NEW DRIFT — anything that deviates from the accepted baseline in the
   calibration-trap list below must be reported as NEW DRIFT, never absorbed
   into the baseline silently.""",
    # v2 — drift-first (rotated after a miss streak).
    """1. BASELINE DEVIATION — compare against the accepted baseline FIRST: a
   skill whose verdict or share moved away from its accepted value is
   NEW DRIFT.
2. UTILITY COLLAPSE — a skill whose utility share collapsed relative to its
   accepted share (keep -> demote/remove in one cycle) is drift even when the
   final verdict is mechanically legal.
3. CORPUS GROWTH — a skill absent from the accepted baseline is new drift
   until the audit has examined it and its verdict is keep.
4. DISAPPEARANCE — a skill that was accepted and is no longer in the corpus
   is drift; say so explicitly instead of silently dropping it.
5. TRAP DISCIPLINE — calibration traps are known and accepted; re-reporting
   a trap as new drift is a false positive and is scored as one.""",
    # v3 — consequence & edge cases (rotated when drift keeps slipping past).
    """1. EDGE CASES — zero-activity skills, missing last_used_at, non-numeric
   counts, and near-zero share are checked explicitly, not assumed fine.
2. DEMOTION CORRECTNESS — only low-utility, non-redundant skills are
   demoted; a demotion stamp on a redundant or high-utility skill is drift.
3. STALENESS — a verdict that has not changed while the corpus moved (a
   frozen standard stops catching drift) is itself a finding.
4. TRAP ACKNOWLEDGEMENT — list every calibration trap by id as KNOWN before
   reporting anything new; missing acknowledgement is a quality flag.
5. NEW DRIFT — report id + what drifted for every deviation not covered by
   a trap; silence when drift exists is a miss and rotates this rubric.""",
)


# ── Observation identity / classification (pure) ────────────────────────────


def observation_id(observation: dict[str, Any]) -> str:
    """Stable identity for an observation: ``<kind>:<name>``.

    Two observations with the same id are the *same thing*; content drift is
    judged on the non-identity fields.
    """
    kind = observation.get("kind") if isinstance(observation, dict) else None
    name = observation.get("name") if isinstance(observation, dict) else None
    return f"{kind or 'observation'}:{name or '<unnamed>'}"


def _same_content(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Content equality ignoring the identity keys."""
    ca = {k: v for k, v in a.items() if k not in _IDENTITY_KEYS}
    cb = {k: v for k, v in b.items() if k not in _IDENTITY_KEYS}
    return ca == cb


def classify_observation(
    observation: dict[str, Any],
    accepted: Sequence[dict[str, Any]],
) -> str:
    """Classify one observation against the accepted (trap) pool.

    Returns ``TRAP`` when the id is known AND the content is unchanged
    (already accepted — must be flagged as known, never re-reported),
    ``CHANGED`` when the id is known but the content drifted (NEW drift on a
    known item), or ``NEW`` when the id has never been accepted (new drift).
    """
    oid = observation_id(observation)
    for prev in accepted:
        if observation_id(prev) != oid:
            continue
        return TRAP if _same_content(observation, prev) else CHANGED
    return NEW


def traps_in(
    observations: Sequence[dict[str, Any]],
    accepted: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The observations that are calibration traps (known + unchanged)."""
    return [o for o in observations if classify_observation(o, accepted) == TRAP]


def find_new_drift(
    observations: Sequence[dict[str, Any]],
    accepted: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """NEW drift among ``observations``: known items whose content changed
    plus items that were never accepted.  Traps are excluded."""
    return [
        o for o in observations if classify_observation(o, accepted) in (CHANGED, NEW)
    ]


def find_missing(
    observations: Sequence[dict[str, Any]],
    accepted: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Accepted observations that are absent from the current corpus.

    A previously-accepted entity vanishing is drift too (e.g. a skill dropped
    from the sidecar) — the auditor should say so, not silently forget it.
    """
    current = {observation_id(o) for o in observations}
    return [a for a in accepted if observation_id(a) not in current]


# ── Audit rubric / prompt (pure) ────────────────────────────────────────────


def active_rubric(
    variants: Sequence[str],
    variant_index: int,
) -> str:
    """The checklist text for ``variant_index``, clamped to the variant list.

    An out-of-range index (e.g. a hand-edited state file) falls back to the
    first variant rather than raising — a corrupt index must not take the
    audit down.
    """
    if not variants:
        return ""
    idx = variant_index if 0 <= variant_index < len(variants) else 0
    return variants[idx]


def _observation_line(observation: dict[str, Any]) -> str:
    """One-line summary of an observation for the prompt's trap list."""
    oid = observation_id(observation)
    content = ", ".join(
        f"{k}={v}" for k, v in observation.items() if k not in _IDENTITY_KEYS
    )
    return f"{oid} ({content})" if content else oid


def build_audit_prompt(
    variants: Sequence[str],
    variant_index: int,
    accepted: Sequence[dict[str, Any]],
    miss_threshold: int = DEFAULT_MISS_THRESHOLD,
) -> str:
    """Compose the audit prompt/checklist for this run.

    The prompt tells the auditor (1) that the previous audit's accepted
    observations are carried forward as CALIBRATION TRAPS — already known and
    accepted, to be flagged as known and never re-reported as new drift — and
    (2) that NEW drift beyond the traps must still be identified, or the miss
    counter grows and the rubric rotates (the bar rises).
    """
    variant = active_rubric(variants, variant_index)
    lines = [
        "NON-STATIONARY AUDIT BAR — Red-Queen audit standard (issue #63).",
        "",
        "This audit's bar is not frozen: it rises with the system. The previous",
        "audit's accepted observations are carried forward as CALIBRATION TRAPS",
        "below. They are ALREADY KNOWN AND ACCEPTED: acknowledge each one as",
        "known — never report it as new drift. Your job is (a) to flag every",
        "trap as known and (b) to identify NEW drift that no trap covers.",
        "",
        f"Miss rule: {miss_threshold} consecutive audits in which drift occurred",
        "but nothing was reported rotate the audit rubric (the bar rises) and",
        "reset the counter.",
        "",
        f"ACTIVE AUDIT RUBRIC — variant {variant_index + 1} of {len(variants)}",
        "--------------------------------------------------------------",
        variant,
        "",
        f"CALIBRATION TRAPS — {len(accepted)} already known/accepted (do NOT",
        "report these as new drift; acknowledge them as KNOWN):",
    ]
    for obs in accepted:
        lines.append(f"  - KNOWN/ACCEPTED: {_observation_line(obs)}")
    lines.extend([
        "",
        "Report format: list each trap id as KNOWN, then list NEW DRIFT",
        "findings as id + what drifted. Silence when drift exists is a miss.",
    ])
    return "\n".join(lines)


# ── State (explicit IO, fail-open) ──────────────────────────────────────────


@dataclass
class AuditBarState:
    """Persisted non-stationary bar state.

    ``accepted_observations`` is the calibration-trap pool carried forward
    run to run; ``miss_count`` is the consecutive missed-drift streak;
    ``rubric_variant`` is the active checklist variant index;
    ``miss_threshold`` is the rotation threshold (persisted so the owner can
    tune it in the state file); ``rotations`` is the rotation history.
    """

    accepted_observations: list[dict[str, Any]] = field(default_factory=list)
    miss_count: int = 0
    rubric_variant: int = 0
    miss_threshold: int = DEFAULT_MISS_THRESHOLD
    last_run_at: Optional[str] = None
    rotations: list[dict[str, Any]] = field(default_factory=list)


def default_state(miss_threshold: int = DEFAULT_MISS_THRESHOLD) -> AuditBarState:
    """A fresh state: empty trap pool, no misses, baseline rubric."""
    return AuditBarState(miss_threshold=max(1, int(miss_threshold)))


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def state_to_dict(state: AuditBarState) -> dict[str, Any]:
    """JSON-serialisable dict for the state file."""
    return {
        "accepted_observations": list(state.accepted_observations),
        "miss_count": state.miss_count,
        "rubric_variant": state.rubric_variant,
        "miss_threshold": state.miss_threshold,
        "last_run_at": state.last_run_at,
        "rotations": list(state.rotations),
    }


def state_from_dict(data: Any) -> AuditBarState:
    """Tolerant decode: wrong types / missing keys fall back to defaults, so
    a hand-edited or partially-corrupt state file degrades instead of
    crashing the audit."""
    if not isinstance(data, dict):
        return default_state()
    accepted = data.get("accepted_observations")
    if not isinstance(accepted, list):
        accepted = []
    rotations = data.get("rotations")
    if not isinstance(rotations, list):
        rotations = []
    return AuditBarState(
        accepted_observations=[o for o in accepted if isinstance(o, dict)],
        miss_count=max(0, _safe_int(data.get("miss_count"))),
        rubric_variant=max(0, _safe_int(data.get("rubric_variant"))),
        miss_threshold=max(
            1, _safe_int(data.get("miss_threshold"), DEFAULT_MISS_THRESHOLD)
        ),
        last_run_at=(str(data["last_run_at"]) if data.get("last_run_at") else None),
        rotations=[r for r in rotations if isinstance(r, dict)],
    )


def state_file_path(evolution_dir: Path) -> Path:
    """Canonical state file: ``<evolution-dir>/audit-bar-state.json``."""
    return Path(evolution_dir) / "audit-bar-state.json"


def load_state(path: Path) -> AuditBarState:
    """Read the persisted state.  FAIL-OPEN: a missing, unreadable, or
    structurally invalid file returns a fresh default state — the bar must
    never take the audit down over state it cannot read."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default_state()
    return state_from_dict(data)


def save_state(path: Path, state: AuditBarState) -> bool:
    """Persist state atomically (tmp + rename).  Best-effort: a write failure
    returns False and is swallowed — it must never crash the audit."""
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(state_to_dict(state), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, path)
        return True
    except OSError:
        return False


# ── The state machine (pure) ────────────────────────────────────────────────


def rotate_rubric(
    state: AuditBarState,
    variants: Sequence[str] = AUDIT_RUBRIC_VARIANTS,
    now: Optional[datetime] = None,
) -> AuditBarState:
    """Advance the active rubric variant (wraps around) and record the
    rotation.  Returns a NEW state; the input is never mutated."""
    now = now or datetime.now(UTC)
    count = max(1, len(variants))
    old = state.rubric_variant
    new_variant = (old + 1) % count
    new_state = AuditBarState(
        accepted_observations=list(state.accepted_observations),
        miss_count=state.miss_count,
        rubric_variant=new_variant,
        miss_threshold=state.miss_threshold,
        last_run_at=state.last_run_at,
        rotations=list(state.rotations),
    )
    new_state.rotations.append({
        "at": now.isoformat(),
        "from": old,
        "to": new_variant,
        "reason": "miss threshold reached",
    })
    return new_state


def _merge_accepted(
    previous: Sequence[dict[str, Any]],
    observations: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Carry the trap pool forward, upserting by id with latest content.

    Previously-accepted observations persist (the spec: carry the previous
    audit's accepted observations forward); newly-observed items join the
    pool; an item whose content changed takes its newest content so the next
    run judges drift from the newly-accepted state (the bar rises).
    """
    merged: dict[str, dict[str, Any]] = {}
    for obs in list(previous) + list(observations):
        merged[observation_id(obs)] = dict(obs)
    return list(merged.values())


def update_bar_state(
    state: AuditBarState,
    *,
    drift_occurred: bool,
    drift_reported: bool,
    observations: Sequence[dict[str, Any]],
    miss_threshold: Optional[int] = None,
    now: Optional[datetime] = None,
) -> tuple[AuditBarState, list[str]]:
    """Apply one audit run to the bar state.

    Rules:
    * Drift occurred, nothing reported  → miss_count += 1; at threshold the
      rubric rotates (variant advances) and the counter resets.
    * Drift occurred and reported       → the auditor earned its keep:
      miss_count resets to 0.
    * No drift                          → no evidence either way; miss_count
      is left unchanged (a clean corpus neither proves nor disproves the
      auditor's vigilance).
    * ``observations`` are always merged into the accepted (trap) pool, so
      the next audit's prompt carries them forward as calibration traps.
    * ``miss_threshold`` overrides (and persists) the rotation threshold when
      given; otherwise the state's stored threshold is kept.

    Returns ``(new_state, events)``; ``events`` is human-readable lines for
    the audit's report.
    """
    now = now or datetime.now(UTC)
    new_state = AuditBarState(
        accepted_observations=list(state.accepted_observations),
        miss_count=state.miss_count,
        rubric_variant=state.rubric_variant,
        miss_threshold=(
            max(1, int(miss_threshold))
            if miss_threshold is not None
            else state.miss_threshold
        ),
        last_run_at=state.last_run_at,
        rotations=list(state.rotations),
    )
    events: list[str] = []

    if drift_occurred and not drift_reported:
        new_state.miss_count += 1
        if new_state.miss_count >= new_state.miss_threshold:
            new_state = rotate_rubric(new_state, now=now)
            new_state.miss_count = 0
            events.append(
                f"miss count reached {new_state.miss_threshold}/{new_state.miss_threshold} "
                f"— audit rubric rotated to variant {new_state.rubric_variant + 1}; "
                "counter reset (the bar rises)"
            )
        else:
            events.append(
                f"MISS: drift occurred but nothing was reported "
                f"(miss {new_state.miss_count}/{new_state.miss_threshold})"
            )
    elif drift_occurred and drift_reported:
        if new_state.miss_count:
            events.append(
                f"drift reported — miss counter reset (was {new_state.miss_count})"
            )
        new_state.miss_count = 0

    new_state.accepted_observations = _merge_accepted(
        state.accepted_observations, observations
    )
    new_state.last_run_at = now.isoformat()
    return new_state, events
