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

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "ReExecutionVerdict",
    "verify_skill_edit",
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
