"""Lookahead re-planning — divergence detection after each step (issue #291).

Child #1 (#290) shipped the explicit plan data model (:mod:`agent.plan_schema`):
an ordered, immutable :class:`~agent.plan_schema.Plan` of declarative
:class:`~agent.plan_schema.Step` s, each carrying an ``expected_observation``
— the prediction of what the agent should see if the step goes as planned.

This module is child #2's *minimal* slice: the seam that, after a step's tool
call completes, compares the **observed** result against that step's
``expected_observation`` and decides whether reality *diverged* from the plan.
When it diverges, a replan is *triggered* — but triggering is all this slice
owns. The actual "ask an LLM for a fresh plan" call is sibling #292's job and
stays behind a default-off seam here (a stub / overridable hook), so nothing in
this module reaches a model or mutates agent behavior on its own.

What's here
-----------
* :class:`DivergenceResult` — a frozen verdict: ``diverged`` (bool), a plain
  ``reason``, and the ``signal`` that fired (an enum-ish string). Pure value.
* :func:`evaluate_divergence` — the pure heuristic comparator. Given an observed
  result string and a :class:`~agent.plan_schema.Step`, returns a
  :class:`DivergenceResult`. No I/O, no LLM, deterministic. This is the
  load-bearing piece other slices and the eval harness (#292) build on.
* :class:`PlanProgress` — a tiny cursor over a plan's steps so the agent loop
  can ask "which step did this tool call belong to?" and advance. Default-off
  by construction: with no active plan there is no progress object.
* :func:`should_replan` / :func:`trigger_replan` — the replan-trigger seam.
  ``should_replan`` is pure policy over a :class:`DivergenceResult`;
  ``trigger_replan`` invokes an optional, caller-supplied ``replanner`` callable
  and is a no-op (returns ``None``) when none is wired — exactly mirroring how
  ``emit_plan`` stays inert when there is no active plan.

Why a heuristic, not a model
----------------------------
``expected_observation`` is free text written by whatever produced the plan, and
the observed tool result is free text too. A cheap, transparent, deterministic
comparator (error-signal detection + keyword overlap) is enough to *flag* a
divergence for a replanner to act on, and it costs zero tokens and zero latency
— which matters because this runs after *every* step. Sharper, model-based
adjudication is a strict upgrade #292 can layer on top of the same seam.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, List, Optional

from agent.plan_schema import Plan, Step

# A replanner: given the diverged step, its observed result, and the divergence
# verdict, optionally produce a fresh :class:`Plan`. Returning ``None`` means
# "keep the current plan". This module never supplies one — the agent (or #292's
# harness) wires it in. Kept as a type alias so the seam's contract is explicit.
Replanner = Callable[["Plan", "Step", str, "DivergenceResult"], Optional[Plan]]

# Substrings that mark a tool result as a failure/error observation. Matched
# case-insensitively against the observed result. Intentionally small and
# conservative — these are the high-signal markers the runtime itself emits
# (see ``agent.tool_diagnostics`` / guardrail synthetic results) plus the
# obvious ones. A replanner can be far smarter; this only needs to catch the
# "the step clearly failed" case the expected_observation didn't anticipate.
_ERROR_MARKERS: tuple[str, ...] = (
    "error executing tool",
    "traceback (most recent call last)",
    "exception:",
    "failed:",
    "[tool execution cancelled",
    "permission denied",
    "no such file or directory",
    "command not found",
    "fatal:",
)

# Words too common to count as meaningful overlap between an expectation and an
# observation. Keeps keyword matching from passing on filler alone.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "for",
        "with", "is", "are", "be", "it", "its", "this", "that", "as", "by",
        "from", "should", "will", "would", "we", "i", "you", "they", "see",
        "show", "shows", "showing", "result", "results", "output", "back",
    }
)

# Signal labels carried on a DivergenceResult, so consumers (and #292's harness)
# can branch on *why* without re-parsing the reason text.
SIGNAL_OK = "ok"  # observed matched / no yardstick to judge against
SIGNAL_NO_EXPECTATION = "no_expectation"  # step predicted nothing — never diverges
SIGNAL_EMPTY_OBSERVATION = "empty_observation"  # expected something, saw nothing
SIGNAL_ERROR = "error"  # observed carries a failure marker the step didn't predict
SIGNAL_MISMATCH = "mismatch"  # expected keywords absent from observed


@dataclass(frozen=True)
class DivergenceResult:
    """A frozen verdict on whether an observation diverged from a step.

    ``diverged`` is the boolean the replan-trigger seam keys off.
    ``reason`` is a one-line, human-readable explanation (for status output /
    transcripts). ``signal`` is a stable label from the ``SIGNAL_*`` constants so
    programmatic consumers branch without parsing prose. ``overlap`` is the
    fraction (0..1) of the expectation's meaningful keywords found in the
    observation — recorded for the same reason ``Plan.metadata`` is: downstream
    policy (#292) may want a knob, and a verdict that hides its evidence is
    harder to tune.
    """

    diverged: bool
    reason: str
    signal: str
    overlap: float = 0.0


def _keywords(text: str) -> List[str]:
    """Lowercased, de-stopworded word tokens of ``text`` (length >= 3)."""
    tokens = re.findall(r"[a-z0-9_]+", text.lower())
    return [t for t in tokens if len(t) >= 3 and t not in _STOPWORDS]


def _has_error_marker(observed_lower: str) -> bool:
    return any(marker in observed_lower for marker in _ERROR_MARKERS)


def evaluate_divergence(
    observed: Optional[str],
    step: Step,
    *,
    overlap_threshold: float = 0.34,
) -> DivergenceResult:
    """Compare an observed tool result against ``step.expected_observation``.

    Pure and deterministic — no I/O, no model call. The policy, in order:

    1. **No yardstick.** If the step predicted nothing
       (``expected_observation`` blank), there is nothing to diverge from →
       ``diverged=False`` (``SIGNAL_NO_EXPECTATION``). This is what keeps a plan
       whose steps omit predictions from ever tripping a replan.
    2. **Empty observation.** The step expected *something* but the tool gave
       back nothing → diverged (``SIGNAL_EMPTY_OBSERVATION``).
    3. **Unanticipated error.** The observation carries a failure marker that the
       expectation did not itself describe (so a step that *predicts* an error
       isn't flagged for getting one) → diverged (``SIGNAL_ERROR``).
    4. **Keyword mismatch.** Otherwise, measure how many of the expectation's
       meaningful keywords appear in the observation. Below ``overlap_threshold``
       → diverged (``SIGNAL_MISMATCH``); at or above → on-plan (``SIGNAL_OK``).

    ``overlap_threshold`` is the one tunable knob; the default (~a third of the
    expectation's keywords must surface) is deliberately lenient so only clear
    drift trips it. Tightening it is a #292 concern.
    """
    expectation = (step.expected_observation or "").strip()
    if not expectation:
        return DivergenceResult(
            diverged=False,
            reason="step predicted no observation; nothing to compare against",
            signal=SIGNAL_NO_EXPECTATION,
        )

    observed_text = observed or ""
    observed_lower = observed_text.lower()

    if not observed_text.strip():
        return DivergenceResult(
            diverged=True,
            reason="expected an observation but the step produced no output",
            signal=SIGNAL_EMPTY_OBSERVATION,
        )

    # An error counts as divergence only if the step did not itself anticipate
    # one — otherwise a step whose whole point is "expect a failure" would be
    # falsely flagged.
    if _has_error_marker(observed_lower) and not _has_error_marker(expectation.lower()):
        return DivergenceResult(
            diverged=True,
            reason="observed result reports an error the step did not anticipate",
            signal=SIGNAL_ERROR,
        )

    expected_keywords = _keywords(expectation)
    if not expected_keywords:
        # Expectation was all filler/stopwords — no testable content. Treat as
        # "no yardstick" rather than inventing a mismatch.
        return DivergenceResult(
            diverged=False,
            reason="expectation has no testable keywords; treated as on-plan",
            signal=SIGNAL_NO_EXPECTATION,
        )

    observed_keywords = set(_keywords(observed_text))
    hits = sum(1 for kw in expected_keywords if kw in observed_keywords)
    overlap = hits / len(expected_keywords)

    if overlap < overlap_threshold:
        return DivergenceResult(
            diverged=True,
            reason=(
                f"observation matched {hits}/{len(expected_keywords)} expected "
                f"keywords ({overlap:.0%}), below the {overlap_threshold:.0%} threshold"
            ),
            signal=SIGNAL_MISMATCH,
            overlap=overlap,
        )

    return DivergenceResult(
        diverged=False,
        reason=(
            f"observation matched {hits}/{len(expected_keywords)} expected "
            f"keywords ({overlap:.0%}); on plan"
        ),
        signal=SIGNAL_OK,
        overlap=overlap,
    )


@dataclass
class PlanProgress:
    """A mutable cursor over a plan's steps for the execute loop.

    The agent advances this as each step's tool call completes, so the loop can
    answer "which step does this observation belong to?" via :meth:`current` and
    move on via :meth:`advance`. Deliberately *not* frozen — it is per-turn
    mutable execution state, the counterpart to the immutable :class:`Plan`
    artifact it points into.

    Default-off by construction: a :class:`PlanProgress` only exists when an
    active plan is set, so the loop's "no progress object" branch is the
    unchanged-behavior path.
    """

    plan: Plan
    cursor: int = 0  # 0-based index of the *next* step to be observed

    def current(self) -> Optional[Step]:
        """The step the next observation is judged against, or ``None`` if done."""
        if 0 <= self.cursor < len(self.plan.steps):
            return self.plan.steps[self.cursor]
        return None

    def advance(self) -> None:
        """Move the cursor past the current step (clamped at the end)."""
        if self.cursor < len(self.plan.steps):
            self.cursor += 1

    def is_exhausted(self) -> bool:
        """True once every step has been observed."""
        return self.cursor >= len(self.plan.steps)

    def adopt(self, plan: Plan) -> None:
        """Replace the plan and reset the cursor — what a replan installs."""
        self.plan = plan
        self.cursor = 0


def should_replan(result: DivergenceResult) -> bool:
    """Pure policy: does this divergence verdict warrant a replan?

    Trivial today (any divergence → replan), but isolated as its own seam so
    #292 can make it selective (e.g. only ``SIGNAL_ERROR``, or only after N
    consecutive mismatches) without touching the comparator or the agent loop.
    """
    return result.diverged


def trigger_replan(
    plan: Plan,
    step: Step,
    observed: str,
    result: DivergenceResult,
    *,
    replanner: Optional[Replanner] = None,
) -> Optional[Plan]:
    """Fire the replan seam. Inert (returns ``None``) when no replanner is wired.

    This is the #291/#292 boundary made explicit. #291 owns *detecting*
    divergence and *deciding to* replan; actually producing a new plan is a
    model call #292 supplies as ``replanner``. With no replanner — the default —
    this returns ``None`` and the current plan stands, so the trigger is a no-op
    exactly like ``emit_plan(None, ...)`` is.

    A misbehaving replanner must never break a turn, so its exceptions are
    swallowed (mirroring ``emit_plan``'s broken-sink contract).
    """
    if replanner is None:
        return None
    try:
        new_plan = replanner(plan, step, observed, result)
    except Exception:
        return None
    if isinstance(new_plan, Plan):
        return new_plan
    return None
