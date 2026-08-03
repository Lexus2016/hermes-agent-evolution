#!/usr/bin/env python3
"""
Compact Context Tool — agent-driven intra-trajectory compression (#1568).

Implements the SelfCompact pattern (Li et al., arXiv:2606.23525): a tool the
model invokes itself to summarise accumulated conversation/tool history into
a concise prefix, replacing verbose prior turns. This complements the existing
automatic threshold-triggered ``_compress_context`` — here the *agent* decides
*when* to compact, reacting to context rot the system-level trigger cannot see
(e.g. right after a sub-task resolves).

The fire/suppress rubric lives in the system prompt
(agent.prompt_builder.SELFCOMPACT_RUBRIC_GUIDANCE) — the paper shows the tool
alone is used erratically by open-weight models; only tool + rubric together
elicit adaptive compaction.

Design:
- Thin wrapper over ``agent._compress_context(force=True, focus_topic=...)``
  — reuses the proven summariser, splitter, session-rotation, and memory
  handoff already shipped in agent.conversation_compression.
- ``force=True`` bypasses the summary-failure cooldown so the model can compact
  even right after an auto-compress abort (mirrors the manual ``/compress``
  slash command).
- The tool result is purely informational — the real state change happened
  inside ``_compress_context`` (the messages list was rewritten in place and
  the session rotated). The model continues the turn on the compacted list.
"""

import json
import logging

logger = logging.getLogger(__name__)


def compact_context_tool(
    *,
    focus_topic: str = "",
    agent=None,
    messages: list = None,
) -> str:
    """Invoke agent-driven context compaction.

    Args:
        focus_topic: Optional focus string. The summariser will prioritise
            preserving information related to this topic (mirrors the
            ``/compact <focus>`` slash command). Omit for a general summary.
        agent: The owning AIAgent (passed by the dispatcher).
        messages: The current message list (the same object the conversation
            loop is iterating). ``_compress_context`` rewrites it in place.

    Returns:
        JSON string with the compaction outcome.
    """
    if agent is None:
        return json.dumps(
            {
                "success": False,
                "error": "compact_context is not available in this execution context.",
            },
            ensure_ascii=False,
        )
    if messages is None:
        # Fall back to the agent's session messages if the dispatcher did not
        # inject the live list (defensive — the invoke_tool path always does).
        messages = getattr(agent, "_session_messages", None)
        if messages is None:
            return json.dumps(
                {"success": False, "error": "No active conversation to compact."},
                ensure_ascii=False,
            )

    pre_count = len(messages)
    system_message = getattr(agent, "_cached_system_prompt", None) or ""

    try:
        new_messages, new_prompt = agent._compress_context(
            messages,
            system_message,
            focus_topic=focus_topic or None,
            force=True,
        )
    except Exception as exc:
        logger.exception("compact_context tool failed")
        return json.dumps(
            {"success": False, "error": f"Compaction failed: {exc}"},
            ensure_ascii=False,
        )

    post_count = len(new_messages)
    # _compress_context returns the input unchanged when it aborts (e.g.
    # conversation too short to summarise, or the summary LLM failed).
    if post_count >= pre_count and new_messages is messages:
        return json.dumps(
            {
                "success": False,
                "aborted": True,
                "reason": (
                    "Compaction did not shrink the context — the conversation "
                    "may be too short, the summariser may be temporarily "
                    "unavailable, or no savings were achievable. Continue "
                    "working; the system will retry automatically at the "
                    "threshold."
                ),
                "message_count": pre_count,
            },
            ensure_ascii=False,
        )

    # Propagate the compacted list back so the conversation loop continues on it.
    # _compress_context may return a fresh list on session rotation; ensure the
    # caller's reference points at the compacted data.
    if new_messages is not messages:
        messages.clear()
        messages.extend(new_messages)
    if new_prompt:
        agent._cached_system_prompt = new_prompt
        agent._session_messages = messages

    return json.dumps(
        {
            "success": True,
            "message_count_before": pre_count,
            "message_count_after": post_count,
            "note": (
                "Context compacted. Continue from the summary — earlier "
                "verbatim turns have been replaced by a concise prefix. "
                "Resolved facts are preserved; re-read files or re-run "
                "searches only if you need exact prior output."
            ),
        },
        ensure_ascii=False,
    )


def check_compact_context_requirements(agent=None) -> bool:
    """Available when the agent has a context_compressor and compression is on."""
    if agent is None:
        # Schema-level check (no agent instance) — assume available; the
        # handler guards the real availability.
        return True
    return getattr(agent, "context_compressor", None) is not None


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

COMPACT_CONTEXT_SCHEMA = {
    "name": "compact_context",
    "description": (
        "Summarise the accumulated conversation and tool history into a concise "
        "prefix, replacing verbose prior turns. Use this when you notice the "
        "context has grown large with stale or resolved content and you want to "
        "keep working without losing track of resolved facts.\n\n"
        "FIRE this tool when:\n"
        "- A sub-task has just resolved and its verbose tool outputs are no "
        "longer needed verbatim.\n"
        "- The trajectory is converging (repeated similar tool calls, "
        "diminishing new signal).\n"
        "- You find yourself re-reading old turns to re-establish what was "
        "already decided.\n\n"
        "SUPPRESS (do NOT fire) when:\n"
        "- You are mid-derivation inside a multi-step calculation or reasoning "
        "chain (compaction there loses signal without helping).\n"
        "- You are stuck or iterating without progress — compaction will not "
        "unblock you; change strategy instead.\n"
        "- The conversation is short (few turns) — there is nothing to gain.\n\n"
        "Optional `focus_topic` tells the summariser what to prioritise "
        "preserving (e.g. 'the failing test names' or 'the API contract')."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "focus_topic": {
                "type": "string",
                "description": (
                    "Optional focus string. The summariser will prioritise "
                    "preserving information related to this topic. Omit for a "
                    "general summary."
                ),
            },
        },
        "required": [],
    },
}


# --- Registry ---
from tools.registry import registry  # noqa: E402

registry.register(
    name="compact_context",
    toolset="compact_context",
    schema=COMPACT_CONTEXT_SCHEMA,
    handler=lambda args, **kw: compact_context_tool(
        focus_topic=args.get("focus_topic", ""),
        agent=kw.get("agent"),
        messages=kw.get("messages"),
    ),
    check_fn=lambda *a, **kw: check_compact_context_requirements(agent=kw.get("agent")),
    emoji="🗜️",
)
