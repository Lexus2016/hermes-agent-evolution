# -*- coding: utf-8 -*-
"""SkillProx Slice 1 — re-execution verify primitive for proposed skill edits (#2777).

Child of #2744 (SkillProx validation gate).  A proposed skill edit is
trustworthy only if re-running it on the *same* batch reproduces the same
outcome — a proximal check that catches edits which pass once by luck but
fail on re-execution (non-determinism, hidden state, order dependence).

Components:

1. **Re-execution verifier** — a pure function that applies a proposed edit
   to a skill body, runs the edited skill against the same batch of inputs,
   and returns a pass/fail verdict plus the per-input outcomes.
2. **Verdict** — ``True`` when the edited skill passes every input in the
   batch; ``False`` otherwise.  The verdict is *verified* because it is
   derived from an actual re-run, not from the edit's author's claim.

New module, no changes to existing skill loading.  Diff ≤ 200 lines.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "ReExecutionVerdict",
    "verify_skill_edit",
    "verify_skill_edit_with_memory",
    "record_verdict",
    "is_rejected",
    "edit_key",
]

# A skill edit is a callable that transforms a skill body (str) into a new
# body (str).  The verifier re-runs the *edited* body against the batch.
SkillEdit = Callable[[str], str]
# A runner executes a skill body against one input and returns a bool outcome.
SkillRunner = Callable[[str, Any], bool]


@dataclass
class ReExecutionVerdict:
    """Outcome of re-running a proposed skill edit on the same batch."""

    passed: bool
    per_input: Dict[str, bool] = field(default_factory=dict)
    edited_body: str = ""
    error: Optional[str] = None


def verify_skill_edit(
    original_body: str,
    edit: SkillEdit,
    batch: Sequence[Any],
    runner: SkillRunner,
    *,
    input_keys: Optional[Sequence[str]] = None,
) -> ReExecutionVerdict:
    """Re-run a proposed skill edit on the same batch and return a verdict.

    ``edit`` transforms ``original_body`` into the proposed new body.  The
    edited body is then executed against every input in ``batch`` via
    ``runner``; the verdict passes only when *all* inputs succeed.

    ``input_keys`` optionally names each input (for the per-input map);
    otherwise inputs are keyed by position.  A runner exception is treated
    as a per-input failure (never a crash of the verifier itself).
    """
    try:
        edited_body = edit(original_body)
    except Exception as exc:  # noqa: BLE001 - an edit that throws is a failure
        return ReExecutionVerdict(
            passed=False,
            edited_body=original_body,
            error=f"edit raised: {exc}",
        )

    per_input: Dict[str, bool] = {}
    for pos, inp in enumerate(batch):
        key = input_keys[pos] if input_keys and pos < len(input_keys) else str(pos)
        try:
            per_input[key] = bool(runner(edited_body, inp))
        except Exception as exc:  # noqa: BLE001 - a runner that throws is a failure
            per_input[key] = False
            logger.debug("skill_edit re-execution failed on %r: %s", key, exc)

    return ReExecutionVerdict(
        passed=all(per_input.values()) if per_input else False,
        per_input=per_input,
        edited_body=edited_body,
    )


# ──────────────────────────────────────────────────────────────────────
# Slice 2 — accept/reject memory (#2778): persist verdicts so a rejected
# edit is never re-proposed. The edit's identity is the content hash of
# (skill name, edited body): a byte-identical re-proposal is recognized
# across runs without any proposer cooperation.
# ──────────────────────────────────────────────────────────────────────

_DEFAULT_STORE = ("skill-prox", "verdicts.jsonl")


def _default_store_path() -> Path:
    """Canonical store: $EVOLUTION_PROFILE_DIR or ~/.hermes/evolution."""
    import os
    from pathlib import Path as _P

    base = os.environ.get("EVOLUTION_PROFILE_DIR") or str(_P.home() / ".hermes" / "evolution")
    return _P(base).joinpath(*_DEFAULT_STORE)


def edit_key(skill_name: str, edited_body: str) -> str:
    """Stable identity of a proposed edit (sha256 of skill + body)."""
    import hashlib

    return hashlib.sha256(
        f"{(skill_name or '').strip()}\n{edited_body or ''}".encode("utf-8")
    ).hexdigest()


def _load_verdicts(store_path: Path) -> Dict[str, bool]:
    verdicts: Dict[str, bool] = {}
    try:
        for line in store_path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and isinstance(rec.get("key"), str):
                verdicts[rec["key"]] = bool(rec.get("passed"))
    except OSError:
        pass
    return verdicts


def record_verdict(
    skill_name: str,
    edited_body: str,
    passed: bool,
    *,
    store_path: Optional[Path] = None,
) -> None:
    """Append one verdict record (best-effort; never raises)."""
    import time as _time

    path = store_path or _default_store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "key": edit_key(skill_name, edited_body),
            "skill": (skill_name or "")[:200],
            "passed": bool(passed),
            "recorded_at": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
    except OSError as exc:
        logger.debug("skill-prox verdict record failed: %s", exc)


def is_rejected(skill_name: str, edited_body: str, *, store_path: Optional[Path] = None) -> bool:
    """True when this EXACT edit already failed verification once.

    Latest verdict wins (a later accept supersedes an earlier reject for the
    same key — e.g. a fixed environment re-verification), and a rejected edit
    must not be re-proposed while its reject stands.
    """
    verdicts = _load_verdicts(store_path or _default_store_path())
    return verdicts.get(edit_key(skill_name, edited_body)) is False


def verify_skill_edit_with_memory(
    skill_name: str,
    original_body: str,
    edit: SkillEdit,
    batch: Sequence[Any],
    runner: SkillRunner,
    *,
    input_keys: Optional[Sequence[str]] = None,
    store_path: Optional[Path] = None,
) -> ReExecutionVerdict:
    """Verify an edit ONCE: a previously-rejected identical edit is skipped.

    The gate the skill-evolution loop calls (#2744): a rejected edit returns
    its recorded verdict without re-running the batch; a fresh edit is
    verified and its verdict persisted, so the decision survives restarts.
    """
    # Cheap pre-check on the edited body (runs the edit; an edit that throws
    # is a failed verdict, identical to verify_skill_edit).
    try:
        candidate_body = edit(original_body)
    except Exception as exc:  # noqa: BLE001
        verdict = ReExecutionVerdict(
            passed=False, edited_body=original_body, error=f"edit raised: {exc}"
        )
        record_verdict(skill_name, original_body, False, store_path=store_path)
        return verdict
    if is_rejected(skill_name, candidate_body, store_path=store_path):
        return ReExecutionVerdict(
            passed=False,
            edited_body=candidate_body,
            error="previously rejected edit (skill-prox memory) — not re-verified",
        )
    verdict = verify_skill_edit(
        original_body, edit, batch, runner, input_keys=input_keys
    )
    record_verdict(skill_name, verdict.edited_body, verdict.passed, store_path=store_path)
    return verdict
