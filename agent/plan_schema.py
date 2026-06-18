"""Explicit plan data model — the artifact behind plan-and-execute (issue #290).

Hermes' loop is reactive ReAct today: observe, think, act, one tool call at a
time. The plan-and-execute upgrade (parent #283) replaces that greedy posture
with an *explicit, ordered plan* the agent commits to before it starts calling
tools. This module ships only the **data model** for that plan plus a safe,
inert emission seam — no lookahead (sibling #291), no replanning (sibling #292).

What's here
-----------
* :class:`Step` — one ordered unit of intended work. Three fields, matching the
  issue spec: ``tool_call_intent`` (what tool/action the agent means to run),
  ``rationale`` (why this step, in plain language), and
  ``expected_observation`` (what the agent expects to see back). All three are
  declarative *intent* — a Step is never the tool call itself, so building a
  plan has no side effects and dispatches nothing.
* :class:`Plan` — an ordered, immutable sequence of ``Step`` s with an optional
  one-line ``goal``. Steps are 1-indexed for humans via :attr:`Step.index`.
* :func:`emit_plan` — render a plan to a human-readable block and hand it to an
  ``emit`` sink (a ``print``-like callable). Pure I/O on a sink the caller
  supplies; it computes nothing about tool dispatch.

Why frozen dataclasses
----------------------
The codebase models declarative "this is the resolved shape of X" objects as
``@dataclass(frozen=True)`` (see ``agent.coding_context.ContextProfile`` /
``RuntimeMode``). A plan is exactly that kind of object: built once, read by
many, never mutated in place. Freezing makes accidental mid-flight edits a hard
error and keeps the artifact safe to stash on session state and share.

Default-off by construction
---------------------------
Nothing in this module reaches into the agent loop, and nothing in the loop
constructs a :class:`Plan` on its own. The emission hook on the agent
(``_emit_plan_before_tool_calls``) is a no-op until something sets an active
plan, so the agent behaves identically when the feature is unused. Wiring an
opt-in flag and an eval harness on top of this is #292's job, not this slice's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

# A plan-emission sink: any ``print``-like callable taking a single string.
EmitFn = Callable[[str], None]


@dataclass(frozen=True)
class Step:
    """One ordered, declarative step of an execution plan.

    A ``Step`` describes *intended* work — it is never the tool call itself, so
    constructing one runs nothing and has no side effects. The three content
    fields are the issue-#290 contract:

    ``tool_call_intent``     — the tool or action the agent means to take next,
                               e.g. ``"read_file(agent/plan_schema.py)"`` or
                               ``"search the web for FLARE benchmark numbers"``.
                               Free text by design: this is a stated intent, not
                               a validated/dispatchable tool invocation.
    ``rationale``            — why this step exists, in plain language. The
                               reasoning that justifies the intent.
    ``expected_observation`` — what the agent expects to observe back if the
                               step goes as planned. The yardstick a later
                               replanning pass (#292) will compare reality
                               against; unused here beyond being recorded.

    ``index`` is an optional 1-based position used only for human-readable
    rendering. :meth:`Plan.__post_init__` assigns it when steps are collected
    into a plan, so callers normally leave it ``None``.
    """

    tool_call_intent: str
    rationale: str
    expected_observation: str
    index: Optional[int] = None

    def __post_init__(self) -> None:
        # Intent and rationale are the load-bearing fields; an empty intent is a
        # programming error (a step that intends nothing). Validate at the
        # source rather than letting a blank step slip into the artifact.
        if not self.tool_call_intent or not self.tool_call_intent.strip():
            raise ValueError("Step.tool_call_intent must be a non-empty string")
        if not self.rationale or not self.rationale.strip():
            raise ValueError("Step.rationale must be a non-empty string")

    def with_index(self, index: int) -> "Step":
        """Return a copy of this step pinned to a 1-based ``index``.

        Frozen dataclasses can't be mutated, so plan assembly rebuilds each step
        with its position rather than writing the field in place.
        """
        return Step(
            tool_call_intent=self.tool_call_intent,
            rationale=self.rationale,
            expected_observation=self.expected_observation,
            index=index,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict (session-state / transcript friendly)."""
        return {
            "tool_call_intent": self.tool_call_intent,
            "rationale": self.rationale,
            "expected_observation": self.expected_observation,
            "index": self.index,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Step":
        """Rebuild a :class:`Step` from :meth:`to_dict` output.

        Tolerant of a missing ``expected_observation`` / ``index`` (older or
        hand-authored payloads), strict on the two required fields via
        :meth:`__post_init__`.
        """
        return cls(
            tool_call_intent=str(data.get("tool_call_intent", "")),
            rationale=str(data.get("rationale", "")),
            expected_observation=str(data.get("expected_observation", "")),
            index=data.get("index"),
        )

    def render(self) -> str:
        """One human-readable block for this step (used by :func:`emit_plan`)."""
        head = f"{self.index}. " if self.index is not None else "- "
        lines = [f"{head}{self.tool_call_intent}"]
        lines.append(f"   why: {self.rationale}")
        if self.expected_observation and self.expected_observation.strip():
            lines.append(f"   expect: {self.expected_observation}")
        return "\n".join(lines)


@dataclass(frozen=True)
class Plan:
    """An ordered, immutable plan: a ``goal`` plus 1-indexed :class:`Step` s.

    Built once and read by many. The constructor assigns each step its 1-based
    :attr:`Step.index` so callers pass steps without worrying about numbering.
    A plan with zero steps is rejected — an empty plan is not an artifact, and
    the inert emission hook treats "no plan" and "empty plan" the same anyway
    (it emits nothing), so forbidding it keeps the type honest.
    """

    steps: Sequence[Step]
    goal: str = ""
    # Free-form provenance/labels for downstream consumers (#292's harness).
    # Never read by this module; carried verbatim.
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.steps:
            raise ValueError("Plan must contain at least one Step")
        # Re-pin indices 1..N so the artifact's numbering is always canonical,
        # regardless of any index the caller set on individual steps. Frozen, so
        # assign through object.__setattr__ (the dataclass-documented escape
        # hatch for normalizing fields in __post_init__).
        indexed = tuple(
            step.with_index(position)
            for position, step in enumerate(self.steps, start=1)
        )
        object.__setattr__(self, "steps", indexed)

    def __len__(self) -> int:
        return len(self.steps)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict for session state or transcripts."""
        return {
            "goal": self.goal,
            "steps": [step.to_dict() for step in self.steps],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Plan":
        """Rebuild a :class:`Plan` from :meth:`to_dict` output."""
        raw_steps = data.get("steps") or []
        steps = [Step.from_dict(item) for item in raw_steps]
        metadata = data.get("metadata")
        return cls(
            steps=steps,
            goal=str(data.get("goal", "")),
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
        )

    def render(self) -> str:
        """Render the whole plan as a human-readable text block.

        Stable, terminal-friendly output suitable for printing before tool
        calls or stashing in a transcript. No ANSI, no side effects.
        """
        lines: List[str] = []
        header = "Plan"
        if self.goal and self.goal.strip():
            header = f"Plan — {self.goal.strip()}"
        lines.append(header)
        for step in self.steps:
            lines.append(step.render())
        return "\n".join(lines)


def emit_plan(plan: Optional[Plan], *, emit: EmitFn) -> bool:
    """Render ``plan`` and push it to the ``emit`` sink. Inert when no plan.

    The single shared emission primitive. Returns ``True`` if a plan was
    emitted, ``False`` when ``plan`` is ``None`` (the default-off case) — so the
    agent's pre-tool-call hook can call this unconditionally and stay a no-op
    until an active plan is set.

    ``emit`` is any ``print``-like callable; the agent passes its own status
    sink. Never raises on a misbehaving sink — emission must not break a turn.
    """
    if plan is None:
        return False
    try:
        emit(plan.render())
    except Exception:
        # A broken sink must never interrupt tool dispatch.
        return False
    return True
