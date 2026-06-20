#!/usr/bin/env python3
"""Self-critique: audit a completed task against the original request.

Compares the final response (and an optional tool trace) to the user's
initial ask and returns a structured verdict — does the deliverable actually
satisfy the request, or does it omit a constraint, misread scope, or stop one
step short? This is most useful after long multi-tool loops, where the final
message can drift from what was originally asked.

Design goals
------------
* **No history mutation, no auto re-loop.** This module only *reports*. It
  never edits the conversation and never re-enters the agent loop. Acting on
  the verdict is the caller's (and ultimately the user's) decision.
* **Deterministically testable.** The LLM call is injected via
  ``critique_fn`` so the audit logic can be unit-tested with synthetic
  request/response pairs and no network.
* **Degrades safely.** When no client is injected it lazily uses Hermes'
  shared auxiliary client (``agent.auxiliary_client.call_llm``). If that is
  unavailable or errors, it returns ``verdict="unknown"`` with a reason
  rather than guessing a pass/fail.
* **Cron/CLI friendly.** Run standalone: feed a JSON payload on stdin (or
  ``--input FILE``) and read a JSON verdict on stdout.

Output shape
------------
    {
      "verdict": "satisfied" | "partial" | "missing" | "unknown",
      "missing_items": [str, ...],
      "suggested_follow_up": str
    }
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# The three substantive verdicts the auditor may return. ``unknown`` is a
# fourth, reserved state used only when the audit could not be performed.
VERDICTS = ("satisfied", "partial", "missing")

# Keep the audit prompt cheap: bound the trace we feed the model.
_MAX_TRACE_CHARS = 6000
_MAX_FIELD_CHARS = 8000

_SYSTEM_PROMPT = (
    "You are a strict quality auditor for an AI agent. Given a user's ORIGINAL "
    "REQUEST and the agent's FINAL RESPONSE (plus an optional tool trace), "
    "judge ONLY whether the response actually satisfies the original request. "
    "Look for: omitted constraints, misread scope, requirements addressed "
    "partially, and claims of completion that the trace does not support. "
    "Do NOT reward fluent or well-formatted answers that miss the ask.\n\n"
    "Respond with a SINGLE JSON object and nothing else:\n"
    '{"verdict": "satisfied" | "partial" | "missing", '
    '"missing_items": ["concise unmet requirement", ...], '
    '"suggested_follow_up": "one short actionable sentence, or empty string"}\n\n'
    "Rules: verdict=satisfied only when every explicit requirement is met "
    "(missing_items MUST be empty). verdict=partial when some are met. "
    "verdict=missing when the core ask is unmet. Keep missing_items terse."
)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.6)]
    tail = text[-int(limit * 0.3):]
    return f"{head}\n...[truncated {len(text)} chars]...\n{tail}"


def _normalize_trace(tool_trace_json: Any) -> str:
    """Render the tool trace to a compact, bounded string for the prompt."""
    if tool_trace_json in (None, "", [], {}):
        return ""
    if isinstance(tool_trace_json, str):
        text = tool_trace_json
    else:
        try:
            text = json.dumps(tool_trace_json, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(tool_trace_json)
    return _truncate(text, _MAX_TRACE_CHARS)


def build_messages(
    original_request: str,
    final_response: str,
    tool_trace_json: Any = None,
) -> List[Dict[str, str]]:
    """Build the chat messages for the audit call."""
    trace = _normalize_trace(tool_trace_json)
    user_parts = [
        "=== ORIGINAL REQUEST ===",
        _truncate(original_request or "", _MAX_FIELD_CHARS),
        "",
        "=== FINAL RESPONSE ===",
        _truncate(final_response or "", _MAX_FIELD_CHARS),
    ]
    if trace:
        user_parts += ["", "=== TOOL TRACE (truncated) ===", trace]
    user_parts += ["", "Now return the JSON verdict."]
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(user_parts)},
    ]


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Pull the first JSON object out of a model response (tolerates fences)."""
    if not text:
        return None
    stripped = text.strip()
    # Strip ```json ... ``` / ``` ... ``` fences if present.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()
    try:
        obj = json.loads(stripped)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        pass
    # Fall back to the first balanced-looking {...} span.
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            obj = json.loads(stripped[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except (ValueError, TypeError):
            return None
    return None


def _coerce_verdict_word(raw: Any) -> Optional[str]:
    """Map a free-form verdict string onto the canonical vocabulary."""
    if not isinstance(raw, str):
        return None
    low = raw.strip().lower()
    if low.startswith("satisf"):
        return "satisfied"
    if low.startswith("part"):
        return "partial"
    if low.startswith("miss") or low.startswith("unmet") or low.startswith("fail"):
        return "missing"
    return None


def coerce_result(obj: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalize a parsed audit object into the strict output shape.

    Applies a consistency guard: a ``satisfied`` verdict with non-empty
    ``missing_items`` is downgraded to ``partial`` (the model contradicted
    itself; the safer reading is that something is unmet).
    """
    if not isinstance(obj, dict):
        return _unknown("auditor returned no parseable JSON object")

    verdict = _coerce_verdict_word(obj.get("verdict"))
    if verdict is None:
        return _unknown("auditor returned an unrecognized verdict")

    raw_items = obj.get("missing_items", [])
    missing_items: List[str] = []
    if isinstance(raw_items, list):
        missing_items = [str(it).strip() for it in raw_items if str(it).strip()]
    elif isinstance(raw_items, str) and raw_items.strip():
        missing_items = [raw_items.strip()]

    follow_up = obj.get("suggested_follow_up", "")
    follow_up = follow_up.strip() if isinstance(follow_up, str) else ""

    # Consistency guard.
    if verdict == "satisfied" and missing_items:
        verdict = "partial"

    return {
        "verdict": verdict,
        "missing_items": missing_items,
        "suggested_follow_up": follow_up,
    }


def _unknown(reason: str) -> Dict[str, Any]:
    return {
        "verdict": "unknown",
        "missing_items": [],
        "suggested_follow_up": reason,
    }


def _default_critique_fn(
    messages: List[Dict[str, str]],
    *,
    timeout: float = 30.0,
    max_tokens: int = 500,
    main_runtime: Optional[Dict[str, Any]] = None,
) -> str:
    """Call Hermes' shared auxiliary client and return the raw text content.

    Imported lazily so this module stays importable (and unit-testable with an
    injected ``critique_fn``) in environments without the agent runtime.
    """
    from agent.auxiliary_client import call_llm

    response = call_llm(
        task="self_critique",
        messages=messages,
        max_tokens=max_tokens,
        temperature=0.0,
        timeout=timeout,
        main_runtime=main_runtime,
    )
    return response.choices[0].message.content or ""


def critique(
    original_request: str,
    final_response: str,
    tool_trace_json: Any = None,
    *,
    critique_fn: Optional[Callable[..., str]] = None,
    timeout: float = 30.0,
    main_runtime: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Audit a completed task against the original request.

    Parameters
    ----------
    original_request : the user's initial ask.
    final_response   : the agent's final deliverable.
    tool_trace_json  : optional tool trace (JSON string or object).
    critique_fn      : callable ``(messages, **kw) -> str`` returning the raw
                       auditor text. Defaults to the shared auxiliary client.
                       Injected in tests for determinism.

    Returns the strict output shape (see module docstring). Never raises for
    auditor failures — returns ``verdict="unknown"`` with a reason instead.
    """
    if not (original_request or "").strip():
        return _unknown("no original request provided")
    if not (final_response or "").strip():
        return _unknown("no final response provided")

    messages = build_messages(original_request, final_response, tool_trace_json)
    fn = critique_fn or _default_critique_fn

    try:
        raw = fn(messages, timeout=timeout, main_runtime=main_runtime)
    except TypeError:
        # An injected critique_fn may not accept the runtime kwargs.
        try:
            raw = fn(messages)
        except Exception as e:  # noqa: BLE001
            logger.warning("self_critique: auditor call failed: %s", e)
            return _unknown(f"auditor unavailable: {e}")
    except Exception as e:  # noqa: BLE001
        logger.warning("self_critique: auditor call failed: %s", e)
        return _unknown(f"auditor unavailable: {e}")

    return coerce_result(_extract_json_object(raw or ""))


def _read_payload(args: argparse.Namespace) -> Dict[str, Any]:
    if args.input:
        with open(args.input, "r", encoding="utf-8") as fh:
            return json.load(fh)
    data = sys.stdin.read()
    return json.loads(data) if data.strip() else {}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit a completed task against the original request."
    )
    parser.add_argument(
        "--input",
        help="Path to a JSON file with keys: original_request, final_response, "
        "tool_trace (optional). Reads stdin when omitted.",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    try:
        payload = _read_payload(args)
    except (OSError, ValueError) as e:
        print(json.dumps(_unknown(f"could not read input: {e}")))
        return 2

    result = critique(
        payload.get("original_request", ""),
        payload.get("final_response", ""),
        payload.get("tool_trace"),
        timeout=args.timeout,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # Exit 0 always — the verdict is on stdout; a non-zero code is reserved
    # for input/operational errors so cron wrappers can distinguish them.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
