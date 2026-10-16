"""Direction-aware context composition for subagent delegation and model fallback.

Implements #3280: Direction-aware context composition to eliminate the 'handoff tax'
when transferring between weaker and stronger models in agent pipelines.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, List, Optional


class HandoffDirection(str, Enum):
    ESCALATE = "escalate"    # weaker -> stronger (compact summary, strip low-quality trace)
    DOWNSHIFT = "downshift"  # stronger -> weaker (preserve high-quality plan, explicit steps)
    PEER = "peer"            # equal tier (balanced graph context)


_WEAK_MODEL_KEYWORDS = frozenset({
    "mini", "flash", "haiku", "small", "lite", "nano", "instant", "8b", "7b", "3b"
})
_STRONG_MODEL_KEYWORDS = frozenset({
    "pro", "opus", "sonnet", "large", "deepseek-r1", "o1", "o3", "r1", "reasoning", "70b", "405b"
})


def _estimate_model_tier(model_name: Optional[str]) -> int:
    """Return model tier: 1 (lightweight/fast), 2 (standard), 3 (frontier/reasoning)."""
    if not model_name:
        return 2
    name = model_name.lower()
    for kw in _WEAK_MODEL_KEYWORDS:
        if kw in name:
            return 1
    for kw in _STRONG_MODEL_KEYWORDS:
        if kw in name:
            return 3
    return 2


def detect_handoff_direction(
    source_model: Optional[str], target_model: Optional[str]
) -> HandoffDirection:
    """Detect directionality of model transition."""
    src_tier = _estimate_model_tier(source_model)
    tgt_tier = _estimate_model_tier(target_model)

    if src_tier < tgt_tier:
        return HandoffDirection.ESCALATE
    elif src_tier > tgt_tier:
        return HandoffDirection.DOWNSHIFT
    return HandoffDirection.PEER


def compose_directional_context(
    turns: list[dict[str, Any]],
    goal: str,
    direction: HandoffDirection,
    existing_context: Optional[str] = None,
) -> str:
    """Compose optimal context tailored to the handoff direction."""
    if not turns:
        return existing_context or ""

    if direction == HandoffDirection.ESCALATE:
        # Weaker -> Stronger: Distill key findings & clean hypotheses, discard noisy tokens
        summary_lines = ["[DIRECTIONAL HANDOFF: ESCALATION TO FRONTIER MODEL]"]
        summary_lines.append("• Prior Context Summary:")

        # Extract only key user intents and failure points
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            role = turn.get("role")
            content = turn.get("content") or ""
            if role == "user" and isinstance(content, str):
                summary_lines.append(f"  - Request: {content[:180].strip()}")
            elif role == "tool" and isinstance(content, str) and ("error" in content.lower() or "fail" in content.lower()):
                summary_lines.append(f"  - Prior Error: {content[:150].strip()}")

        summary_lines.append("• Objective: Reason deeply from first principles and resolve the task.")
        block = "\n".join(summary_lines)

    elif direction == HandoffDirection.DOWNSHIFT:
        # Stronger -> Weaker: Provide concrete execution instructions and retain plan
        lines = ["[DIRECTIONAL HANDOFF: DOWNSHIFT TO FAST WORKER]"]
        lines.append(f"• Concrete Goal: {goal}")
        lines.append("• Instruction: Execute explicitly without diverging from specifications.")
        block = "\n".join(lines)

    else:  # PEER
        from agent.handoff_router import extract_dependency_graph
        graph = extract_dependency_graph(turns)
        block = graph.render_markdown()

    if existing_context and str(existing_context).strip():
        return f"{block}\n\n{str(existing_context).strip()}"
    return block
