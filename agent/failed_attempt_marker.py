"""Failure-detection predicates for context denoising (#1580 — Slice A).

When an agent retries a failed approach, the errored tool results and the
assistant turns that produced them stay in the context window.  On the next
compaction they are summarized like any other content, polluting the summary
with failed-strategy detail that the model then treats as background (#1580).

This module provides **pure, standalone predicates** that identify which
message indices constitute a *failed attempt span* — a contiguous
``assistant(tool_calls) → tool_result`` block where the tool result signals
an error.  The compressor (Slice B, future) can prioritise these spans for
removal or summarization, and the refinement loop (Slice C, future) can
filter failed drafts.

Detection reuses existing failure classifiers rather than introducing new
heuristics:

* ``tool_diagnostics.classify`` — regex taxonomy of error strings.
* ``tool_diagnostics.payload_anomaly`` — structural payload checks.
* ``replay_cleanup.is_interrupted_tool_result`` — interrupted-call signals.

Design constraints
------------------
* **No side effects** — every function is a pure query over a message list.
* **No imports from the compressor** — keeps the predicate testable in
  isolation and avoids circular imports (the compressor will import *this*).
* **Stable output** — the same message list always yields the same span set.
* **Conservative** — only flag spans where the tool result is *clearly* an
  error.  Ambiguous content is left alone; false positives would silently
  discard useful context.
"""

from __future__ import annotations

import json
from typing import Any, Dict, FrozenSet, List, Sequence

from agent import replay_cleanup, tool_diagnostics

__all__ = [
    "FailedAttemptSpan",
    "identify_failed_attempt_spans",
    "failed_attempt_indices",
]


class FailedAttemptSpan:
    """A contiguous assistant → tool_result block that ended in failure.

    Attributes
    ----------
    assistant_index:
        Index of the ``assistant`` message carrying ``tool_calls``.
    result_indices:
        Indices of the ``tool`` messages that answered those calls.
        At least one of these is a detected failure.
    failure_categories:
        Mapping ``result_index → (category, source)`` for every result that
        was flagged, where *source* is one of
        ``"classify"``, ``"payload_anomaly"``, ``"interrupted"``.
    """

    __slots__ = ("assistant_index", "result_indices", "failure_categories")

    def __init__(
        self,
        assistant_index: int,
        result_indices: Sequence[int],
        failure_categories: Dict[int, tuple[str, str]],
    ) -> None:
        self.assistant_index = assistant_index
        self.result_indices: tuple[int, ...] = tuple(result_indices)
        self.failure_categories = dict(failure_categories)

    @property
    def all_indices(self) -> tuple[int, ...]:
        """Every message index covered by this span (assistant + results)."""
        return (self.assistant_index, *self.result_indices)

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return (
            f"FailedAttemptSpan(assistant={self.assistant_index}, "
            f"results={self.result_indices}, "
            f"failures={self.failure_categories})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FailedAttemptSpan):
            return NotImplemented
        return (
            self.assistant_index == other.assistant_index
            and self.result_indices == other.result_indices
            and self.failure_categories == other.failure_categories
        )

    def __hash__(self) -> int:
        return hash((self.assistant_index, self.result_indices))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _content_to_text(content: Any) -> str:
    """Best-effort flattening of a message ``content`` field to plain text.

    Handles ``str``, ``list[dict]`` (multimodal / structured), and ``dict``
    envelopes.  Anything else is stringified.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
                elif "content" in part:
                    parts.append(_content_to_text(part["content"]))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    if isinstance(content, dict):
        # Common envelope shapes: {"content": ...}, {"text": ...}
        inner = content.get("content")
        if inner is not None:
            return _content_to_text(inner)
        text = content.get("text")
        return text if isinstance(text, str) else str(content)
    return str(content)


def _classify_tool_result(content: Any) -> tuple[str, str] | None:
    """Return ``(category, source)`` if *content* is a failed tool result.

    Tries the structural anomaly check first (most reliable — it does not
    depend on error-string wording), then the regex classifier, then the
    interrupted-result check.
    """
    anomaly = tool_diagnostics.payload_anomaly(content)
    if anomaly is not None:
        category, _hint = anomaly
        return category, "payload_anomaly"

    classified = tool_diagnostics.classify(_content_to_text(content))
    if classified is not None:
        category, _hint = classified
        return category, "classify"

    if replay_cleanup.is_interrupted_tool_result(content):
        return "interrupted", "interrupted"

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def identify_failed_attempt_spans(
    messages: List[Dict[str, Any]],
) -> List[FailedAttemptSpan]:
    """Identify all failed-attempt spans in *messages*.

    A *failed-attempt span* is a contiguous sequence starting at an
    ``assistant`` message that carries ``tool_calls``, followed immediately
    by one or more ``tool`` result messages, where **at least one** result
    is classified as a failure by :func:`_classify_tool_result`.

    Spans where *all* results are successful are not returned.  A span with
    a mix of successes and failures is returned as-is (the whole attempt is
    treated as failed because the agent pursued a path that produced at least
    one error).

    Parameters
    ----------
    messages:
        The conversation message list (same shape as the OpenAI chat
        completions ``messages`` array).

    Returns
    -------
    list of :class:`FailedAttemptSpan`, ordered by ``assistant_index``.
    """
    spans: list[FailedAttemptSpan] = []
    if not messages:
        return spans

    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            i += 1
            continue

        # Collect consecutive tool results after this assistant message.
        result_indices: list[int] = []
        j = i + 1
        while j < n and messages[j].get("role") == "tool":
            result_indices.append(j)
            j += 1

        if not result_indices:
            # Dangling tool_calls with no results — not a failed *attempt*,
            # the replay-cleanup path handles dangling tails separately.
            i += 1
            continue

        failure_categories: dict[int, tuple[str, str]] = {}
        for ridx in result_indices:
            result_msg = messages[ridx]
            content = result_msg.get("content")
            hit = _classify_tool_result(content)
            if hit is not None:
                failure_categories[ridx] = hit

        if failure_categories:
            spans.append(
                FailedAttemptSpan(
                    assistant_index=i,
                    result_indices=result_indices,
                    failure_categories=failure_categories,
                )
            )

        # Advance past the entire assistant→results block.
        i = j

    return spans


def failed_attempt_indices(
    messages: List[Dict[str, Any]],
    *,
    include_assistant: bool = True,
) -> FrozenSet[int]:
    """Convenience: flat set of all message indices in failed-attempt spans.

    Parameters
    ----------
    messages:
        The conversation message list.
    include_assistant:
        If ``True`` (default), include the ``assistant`` index of each span.
        Set to ``False`` to get only the tool-result indices — useful when
        the assistant turn carries reasoning worth preserving even though
        the tool call failed.

    Returns
    -------
    A frozenset of integer indices into *messages*.
    """
    indices: set[int] = set()
    for span in identify_failed_attempt_spans(messages):
        if include_assistant:
            indices.add(span.assistant_index)
        indices.update(span.result_indices)
    return frozenset(indices)
