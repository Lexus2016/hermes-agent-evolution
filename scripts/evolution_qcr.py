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
``evolution_trajectory_store``), plus the summary-reranking selector that
picks the reusable memory for a new target. Pure functions, import-safe,
deterministic, no LLM, no network. Increments of #2694: schema + builder
(increment 1, PR #2703); summary-reranking selector (increment 2).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

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


def _note_from(obj: Union[QcrNote, Dict[str, Any]]) -> QcrNote:
    """Coerce a :class:`QcrNote` or its dict projection to a :class:`QcrNote`."""
    if isinstance(obj, QcrNote):
        return obj
    return QcrNote.from_dict(obj)


def score_note_for_target(
    note: Union[QcrNote, Dict[str, Any]],
    target_task_type: str = "",
    target_tools: Optional[Sequence[str]] = None,
) -> float:
    """Deterministic reuse score (clamped 0.0–1.0) for one note vs a new target.

    Summary-reranking signal (#2694 increment 2): prefer the note whose
    workflow invariant still applies and whose bindings re-resolve cheaply
    with the tools the target actually has. No LLM — pure arithmetic:

    * ``+0.5`` task-type match; ``+0.3`` × source-tool overlap share;
      ``−0.2`` per binding tool missing from the target (cap −0.6);
      ``−0.05`` per binding to re-obtain; ``+0.1`` any-environment note.
    """
    n = _note_from(note)
    target_tool_set = {t.lower() for t in (target_tools or [])}
    src_tool_set = {t.lower() for t in n.source_tools}

    score = 0.0
    if target_task_type and n.source_task_type:
        if target_task_type.lower() == n.source_task_type.lower():
            score += 0.5

    if target_tool_set and src_tool_set:
        score += 0.3 * (len(src_tool_set & target_tool_set) / len(src_tool_set))
        missing = src_tool_set - target_tool_set
        score -= 0.2 * min(len(missing), 3)

    score -= 0.05 * len(n.bindings_to_obtain)

    conditions = " ".join(n.applicability_conditions).lower()
    if "any environment" in conditions:
        score += 0.1

    return max(0.0, min(1.0, score))


def rank_notes_for_target(
    notes: Sequence[Union[QcrNote, Dict[str, Any]]],
    target_task_type: str = "",
    target_tools: Optional[Sequence[str]] = None,
) -> List[Tuple[float, QcrNote]]:
    """Rank candidate notes for a new target, best first.

    Stable sort — ties keep input order, so the result is deterministic for
    the same input list. Returns ``[(score, note), ...]``.
    """
    scored = [
        (score_note_for_target(n, target_task_type, target_tools), _note_from(n))
        for n in notes
    ]
    return sorted(scored, key=lambda pair: pair[0], reverse=True)


def select_reusable_memory(
    notes: Sequence[Union[QcrNote, Dict[str, Any]]],
    target_task_type: str = "",
    target_tools: Optional[Sequence[str]] = None,
    min_score: float = 0.5,
) -> Optional[QcrNote]:
    """Pick the best reusable memory for a new target, or ``None`` below threshold.

    ``min_score`` (default 0.5) is the floor: when no candidate reaches it the
    caller should fall back to a fresh execution instead of forcing a
    mismatched reuse (the QCR "does not apply" branch).
    """
    ranked = rank_notes_for_target(notes, target_task_type, target_tools)
    if ranked and ranked[0][0] >= min_score:
        return ranked[0][1]
    return None


# ── Replay path: note store + guardrail + composed reuse (#2694) ────────────

#: Env tool -> the capability a target toolset must have to replay with it.
_ENV_TOOL_ALIASES: Dict[str, str] = {
    "terminal": "terminal",
    "browser_navigate": "browser_navigate",
    "docker": "terminal",
    "ssh": "terminal",
}


def load_notes(store_path: Any) -> List[QcrNote]:
    """Read the QCR note store (JSONL; missing/unreadable/malformed → skip)."""
    path = Path(store_path)
    if not path.is_file():
        return []
    notes: List[QcrNote] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            notes.append(QcrNote.from_dict(json.loads(line)))
        except (ValueError, TypeError):
            continue
    return notes


def write_notes(store_path: Any, notes: Sequence[QcrNote]) -> int:
    """Persist notes to the store as JSONL (one object per line)."""
    path = Path(store_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(n.to_dict()) for n in notes]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return len(lines)


def check_reuse_guardrail(
    note: Union[QcrNote, Dict[str, Any]],
    *,
    target_task_type: str = "",
    target_tools: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Replay gate (#2694): verify applicability + bindings BEFORE reuse.

    ``ok=False`` → fall back to fresh execution; missing binding tools are
    informational (``unresolved_bindings``), task-type/env mismatches block.
    """
    env_aliases = _ENV_TOOL_ALIASES
    n = _note_from(note)
    target = {t.lower() for t in (target_tools or [])}
    reasons: List[str] = []

    if target_task_type and n.source_task_type:
        if target_task_type.lower() != n.source_task_type.lower():
            reasons.append(
                f"task-type mismatch: note is {n.source_task_type!r}, "
                f"target is {target_task_type!r}"
            )
    if any("same environment" in c.lower() for c in n.applicability_conditions):
        env_missing = sorted({
            alias
            for tool, alias in env_aliases.items()
            if tool in {t.lower() for t in n.source_tools} and alias not in target
        })
        if env_missing:
            reasons.append(f"environment tools unavailable: {env_missing}")

    return {
        "ok": not reasons,
        "reasons": reasons,
        "bindings_to_resolve": sorted(set(n.bindings_to_obtain)),
        "unresolved_bindings": [
            b
            for b in n.bindings_to_obtain
            if b.lower() in _BINDING_TOOLS and b.lower() not in target
        ],
        "guardrail": n.verification_guardrail,
        "must_re_resolve_before_replay": True,
    }


def reuse_for_target(
    store_path: Any,
    *,
    target_task_type: str = "",
    target_tools: Optional[Sequence[str]] = None,
    min_score: float = 0.5,
) -> Dict[str, Any]:
    """Replay-path entry: select (incr 2) then guardrail-check (incr 3)."""
    ranked = rank_notes_for_target(
        load_notes(store_path), target_task_type, target_tools
    )
    if not ranked or ranked[0][0] < min_score:
        return {
            "reusable": False,
            "reason": "no candidate note reached the reuse threshold",
            "best_score": ranked[0][0] if ranked else None,
        }
    score, note = ranked[0]
    verdict = check_reuse_guardrail(
        note, target_task_type=target_task_type, target_tools=target_tools
    )
    verdict["reusable"] = bool(verdict["ok"])
    verdict["score"] = score
    verdict["note"] = note.to_dict()
    if not verdict["ok"]:
        verdict["reason"] = "; ".join(verdict["reasons"])
    return verdict


def notes_from_capture_dir(capture_dir: Any) -> List[QcrNote]:
    """Producer (#2694): distill notes from captured successful trajectories."""
    try:
        from evolution_trajectory_store import TrajectoryStore
    except Exception:
        return []
    store = TrajectoryStore.from_capture_dir(capture_dir)
    notes: List[QcrNote] = []
    for task_type in store.task_types():
        for rec in store.by_type(task_type):
            tools = sorted(rec["tool_set"])
            if tools:
                notes.append(build_qcr_note({"tools": tools}, task_type))
    return notes


# ── Skill-distillation consumer (QCR → new skills) (#2694 next increment) ──

_DISTILL_TRACE_GOAL = (
    "Reusable workflow distilled from captured successful trajectories"
)


def distill_skill(
    notes: Sequence[Union[QcrNote, Dict[str, Any]]],
    *,
    target_task_type: str = "funnel",
    target_tools: Optional[Sequence[str]] = None,
    min_score: float = 0.5,
) -> Dict[str, Any]:
    """Reuse distilled QCR notes when building a NEW skill (#2694).

    The QCR replay path already feeds ``reuse_for_target`` (reuse-or-fallback
    in the funnel). This is the *distillation* consumer: it routes the selected
    note into the skill-crystallizer (#2359) so a freshly built skill carries
    the note's four target-bound fields (workflow invariant, bindings,
    applicability, verification guardrail) as an explicit ``Reuse Notes (QCR)``
    section instead of being minted from the raw trace alone.

    Deterministic, import-safe, no LLM, no network:

    1. Select the best reusable note via :func:`select_reusable_memory`.
    2. If none reaches ``min_score`` → ``{"skill_created": False, ...}``
       (fresh-execution fallback, same branch as the replay path).
    3. Otherwise synthesize a minimal trace from the note's source tools and
       crystallize it (``SkillCrystallizer.reflect_on_trace``), then append
       the note's four fields to the skill markdown.

    Returns a summary dict (never raises — the crystallizer is best-effort).
    """
    note = select_reusable_memory(
        notes,
        target_task_type=target_task_type,
        target_tools=target_tools,
        min_score=min_score,
    )
    if note is None:
        ranked = rank_notes_for_target(notes, target_task_type, target_tools)
        return {
            "skill_created": False,
            "reason": "no candidate note reached the reuse threshold",
            "best_score": ranked[0][0] if ranked else None,
        }
    try:
        from evolution.lib.skill_crystallizer import SkillCrystallizer
    except Exception:
        return {
            "skill_created": False,
            "reason": "skill_crystallizer unavailable",
            "note_task_type": note.source_task_type,
        }

    trace = {
        "status": "success",
        "session_id": f"qcr-{note.source_task_type or 'note'}",
        "goal": _DISTILL_TRACE_GOAL,
        "tool_calls": [
            {"name": t, "arguments": "{}"} for t in (note.source_tools or ["terminal"])
        ],
    }
    candidate = SkillCrystallizer.reflect_on_trace(trace)
    if candidate is None:
        return {
            "skill_created": False,
            "reason": "crystallizer rejected the trace",
            "note_task_type": note.source_task_type,
        }

    section = build_qcr_skill_section(note)
    candidate.skill_markdown = candidate.skill_markdown.rstrip() + "\n\n" + section
    if "qcr-reuse" not in candidate.tags:
        candidate.tags.append("qcr-reuse")
    return {
        "skill_created": True,
        "skill_name": candidate.name,
        "note_task_type": note.source_task_type,
        "source_tools": sorted(note.source_tools),
        "qcr_fields": list(QCR_FIELDS),
    }


def build_qcr_skill_section(note: Union[QcrNote, Dict[str, Any]]) -> str:
    """Render a note's four fields as the markdown section embedded in a skill."""
    n = _note_from(note)
    return (
        "## Reuse Notes (QCR)\n"
        "- Workflow invariant: {invariant}\n"
        "- Bindings to obtain: {bindings}\n"
        "- Applicability: {conditions}\n"
        "- Verification guardrail: {guardrail}\n"
    ).format(
        invariant=n.workflow_invariant,
        bindings="; ".join(n.bindings_to_obtain) or "none",
        conditions="; ".join(n.applicability_conditions) or "none",
        guardrail=n.verification_guardrail,
    )
