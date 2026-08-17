#!/usr/bin/env python3
"""Query-conditioned trajectory reuse (QCR) — target-bound note schema (#2694).

arXiv:2608.12847 isolates the post-retrieval reuse step as the bottleneck for
long-horizon trajectory memory. Instead of injecting a raw trace, QCR delivers
a deliberately simple **target-bound note** with four fields:

1. ``workflow_invariant`` — what must stay true for the approach to apply,
2. ``bindings_to_obtain`` — values that must be re-resolved against the target,
3. ``applicability_conditions`` — when the memory applies / when it does not,
4. ``verification_guardrail`` — what must be checked before trusting the outcome.

This module adds the note schema and a deterministic builder that derives a
note from a successful trajectory (the ``TrajectoryStore`` projection in
``evolution_trajectory_store``). Pure functions, import-safe, deterministic,
no LLM, no network. First coherent slice of #2694: schema + builder.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List

#: The four QCR note fields, in canonical order.
QCR_FIELDS: tuple = (
    "workflow_invariant",
    "bindings_to_obtain",
    "applicability_conditions",
    "verification_guardrail",
)

#: Tools that read/search external state whose targets must be re-resolved
#: against a new target (bindings).
_BINDING_TOOLS = frozenset({
    "read_file",
    "search_files",
    "web_search",
    "web_extract",
    "browser_navigate",
})

#: Tools that mutate/execute and therefore require a verification step.
_VERIFY_TOOLS = frozenset({
    "terminal",
    "execute_code",
    "patch",
    "write_file",
    "browser_navigate",
})

#: Tools that imply the workflow is environment-specific.
_ENV_TOOLS = frozenset({"terminal", "browser_navigate", "docker", "ssh"})


@dataclass
class QcrNote:
    """A target-bound note distilled from a successful trajectory."""

    workflow_invariant: str = ""
    bindings_to_obtain: List[str] = field(default_factory=list)
    applicability_conditions: List[str] = field(default_factory=list)
    verification_guardrail: str = ""
    source_task_type: str = ""
    source_tools: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "QcrNote":
        return cls(
            workflow_invariant=str(d.get("workflow_invariant", "")),
            bindings_to_obtain=list(d.get("bindings_to_obtain", []) or []),
            applicability_conditions=list(d.get("applicability_conditions", []) or []),
            verification_guardrail=str(d.get("verification_guardrail", "")),
            source_task_type=str(d.get("source_task_type", "")),
            source_tools=list(d.get("source_tools", []) or []),
        )


def _tool_names(record: Dict[str, Any]) -> List[str]:
    tools = record.get("tools") or []
    if isinstance(tools, (list, tuple, set, frozenset)):
        return [str(t) for t in tools]
    return []


def build_qcr_note(
    record: Dict[str, Any],
    task_type: str = "",
) -> QcrNote:
    """Derive a target-bound note from a successful trajectory record.

    ``record`` is a ``TrajectoryStore`` projection (``{"tools": [...], ...}``).
    Deterministic: the same record always yields the same note. The four
    fields are derived from the tool set: the task type becomes the workflow
    invariant; read/search tools become bindings to re-resolve; env-dependent
    tools make applicability conditional; mutating/executing tools require a
    verification guardrail.
    """
    tools = _tool_names(record)
    tool_set = {t.lower() for t in tools}

    invariant = (
        f"Workflow is known to succeed for {task_type or 'general'} tasks; "
        "reuse only when the new target is of the same task type."
    )

    bindings = sorted(t for t in tools if t.lower() in _BINDING_TOOLS)
    if not bindings:
        bindings = ["re-resolve any file/URL/entity targets against the current task"]

    conditions: List[str] = []
    if tool_set & _ENV_TOOLS:
        conditions.append(
            "Requires the same environment (terminal/browser) as the source run."
        )
    if not conditions:
        conditions.append("Applies to any environment with the standard toolset.")

    guardrail = (
        "Before trusting the outcome, verify the final artifact exists and "
        "matches the task's success criteria."
    )
    if tool_set & _VERIFY_TOOLS:
        guardrail += (
            " Re-run the verification step (tests/exit code) since the workflow "
            "mutates or executes."
        )

    return QcrNote(
        workflow_invariant=invariant,
        bindings_to_obtain=bindings,
        applicability_conditions=conditions,
        verification_guardrail=guardrail,
        source_task_type=task_type,
        source_tools=tools,
    )
