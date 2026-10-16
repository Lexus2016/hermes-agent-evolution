"""Tests for template-marker expansion at delegate_task dispatch (issue #95).

The cron orchestrator (and other LLM-driven dispatchers) can copy prompt
placeholders (<NOW-ISO>, <session_id>, <generated-timestamp>) verbatim into a
delegate_task goal. delegate_task's batch quality gate then rejects the call
and the delegated stage silently no-ops. These tests prove the fix:

1. Known markers are substituted with real values at dispatch time.
2. Unresolvable markers are reported as residual (still rejected loudly).
3. A batch carrying a known marker no longer trips the rejection gate.
"""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch

from tools.delegate_tool import (
    _marker_token,
    delegate_task,
    expand_template_markers,
)


def _make_mock_parent(depth=0):
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "test-key"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = depth
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    return parent


def _call(tasks):
    return json.loads(delegate_task(tasks=tasks, parent_agent=_make_mock_parent()))


GOOD_A = "Refactor the login handler to use the new session helper"
GOOD_B = "Write regression tests for the session expiry watcher"


# --- pure helpers ----------------------------------------------------------

def test_marker_token_normalizes_separators_and_case():
    assert _marker_token("<NOW-ISO>") == "now-iso"
    assert _marker_token("<now_iso>") == "now-iso"
    assert _marker_token("<now iso>") == "now-iso"
    assert _marker_token("{generated-timestamp}") == "generated-timestamp"
    assert _marker_token("<session_id>") == "session-id"
    assert _marker_token("<real citation>") == "real-citation"


def test_expand_substitutes_known_timestamp_markers():
    expanded, substituted, residual = expand_template_markers(
        "Write report dated <NOW-ISO> and <generated-timestamp> to disk",
        now_iso="2026-08-21T22:00:00Z",
    )
    assert "2026-08-21T22:00:00Z" in expanded
    assert "<NOW-ISO>" not in expanded
    assert "<generated-timestamp>" not in expanded
    assert set(substituted) == {"<NOW-ISO>", "<generated-timestamp>"}
    assert residual == []


def test_expand_substitutes_session_id_when_available():
    expanded, substituted, residual = expand_template_markers(
        "tag the record with <session_id>",
        now_iso="2026-08-21T22:00:00Z",
        session_id="cron_abc123",
    )
    assert "cron_abc123" in expanded
    assert "<session_id>" not in expanded
    assert substituted == ["<session_id>"]
    assert residual == []


def test_expand_leaves_unknown_marker_as_residual():
    expanded, substituted, residual = expand_template_markers(
        "Summarize the paper and add <real citation> for each claim",
        now_iso="2026-08-21T22:00:00Z",
    )
    assert "<real citation>" in expanded
    assert substituted == []
    assert residual == ["<real citation>"]


def test_expand_session_id_without_value_stays_residual():
    # No session id available -> <session_id> is NOT substituted; it is
    # reported residual so the caller can strip/reject rather than silently
    # emit an empty value.
    expanded, substituted, residual = expand_template_markers(
        "store under <session_id>/out.json",
        now_iso="2026-08-21T22:00:00Z",
        session_id=None,
    )
    assert "<session_id>" in expanded
    assert substituted == []
    assert residual == ["<session_id>"]


def test_expand_brace_form_too():
    expanded, substituted, _ = expand_template_markers(
        "generated at {NOW-ISO}", now_iso="2026-08-21T22:00:00Z"
    )
    assert "{NOW-ISO}" not in expanded
    assert "2026-08-21T22:00:00Z" in expanded
    assert substituted == ["{NOW-ISO}"]


def test_expand_leaves_code_brackets_untouched():
    # The narrow regex must never fire on generics / HTML / f-strings.
    text = "Return Vec<T> inside a <div> and interpolate {i} and {a,b}"
    expanded, substituted, residual = expand_template_markers(
        text, now_iso="2026-08-21T22:00:00Z"
    )
    assert expanded == text
    assert substituted == []
    assert residual == []


# --- integration: dispatch no longer rejects known markers -----------------

def _completed(idx):
    return {
        "task_index": idx,
        "status": "completed",
        "summary": "ok",
        "api_calls": 1,
        "duration_seconds": 1.0,
        "_child_role": None,
    }


def test_batch_with_known_marker_proceeds_after_substitution():
    # A goal carrying <NOW-ISO> must be substituted and dispatched, not
    # rejected — the exact silent no-op reported in issue #95.
    goal = "Record the pipeline run timestamp <NOW-ISO> in the output file"
    with patch("tools.delegate_tool._run_single_child") as mock_run:
        mock_run.side_effect = [_completed(0), _completed(1)]
        result = _call([{"goal": GOOD_A}, {"goal": goal}])
    assert "error" not in result
    assert len(result["results"]) == 2


def test_batch_with_unknown_marker_still_rejected():
    # Residual markers (unresolvable) still reject — the protection from
    # #81141 is preserved, only now it is loud.
    result = _call([{"goal": GOOD_A}, {"goal": "Implement <feature_name> end to end"}])
    assert "error" in result
    assert "template" in result["error"].lower()


# --- issue #139: single-word date markers + double-brace forms -------------


def test_expand_substitutes_single_word_date_markers():
    # The cron evolution jobs use {date}/{today} in goal templates; these
    # single-word markers must be substituted, not left as residual (issue
    # #139 — regression of the #95 fix).
    expanded, substituted, residual = expand_template_markers(
        "write EVOLUTION/research/{date}.md and tag {{today}}",
        now_iso="2026-08-30T03:15:46Z",
    )
    assert "{date}" not in expanded
    assert "{{today}}" not in expanded
    assert expanded.count("2026-08-30") == 2
    assert set(substituted) == {"{date}", "{{today}}"}
    assert residual == []


def test_expand_substitutes_double_brace_now():
    expanded, _substituted, residual = expand_template_markers(
        "dispatch at {{now}} and <current_date>",
        now_iso="2026-08-30T03:15:46Z",
    )
    assert "{{now}}" not in expanded
    assert "<current_date>" not in expanded
    assert "2026-08-30T03:15:46Z" in expanded
    assert "2026-08-30" in expanded
    assert residual == []


def test_expand_single_word_code_braces_still_untouched():
    # f-string safety preserved: {i}, {a,b}, generics and HTML tags are not
    # template markers and must pass through byte-identical (issue #139).
    text = "iterate {i} over Vec<T> in a <div> and pick {a,b}"
    expanded, substituted, residual = expand_template_markers(
        text, now_iso="2026-08-30T03:15:46Z"
    )
    assert expanded == text
    assert substituted == []
    assert residual == []


def test_expand_unresolvable_double_brace_reported_residual():
    # {{key}} is matched as a template shape but unresolvable — it stays in
    # the expanded text and is reported residual for the caller to reject,
    # exactly like <real citation>.
    expanded, substituted, residual = expand_template_markers(
        "emit {{key}} in the payload", now_iso="2026-08-30T03:15:46Z"
    )
    assert expanded == "emit {{key}} in the payload"
    assert substituted == []
    assert residual == ["{{key}}"]
    # And the batch gate still rejects it loudly.
    result = _call([{"goal": GOOD_A}, {"goal": "emit {{key}} in the payload"}])
    assert "error" in result
    assert "template" in result["error"].lower()


def test_marker_token_strips_double_braces():
    assert _marker_token("{{date}}") == "date"
    assert _marker_token("{{current_date}}") == "current-date"
    assert _marker_token("{date}") == "date"
    assert _marker_token("{{now}}") == "now"


def test_batch_with_date_marker_proceeds_after_substitution():
    # The exact production shape that regressed: a goal carrying the cron
    # {date} marker must be substituted and dispatched, not rejected.
    goal = "write EVOLUTION/research/{date}.md from the gathered notes"
    with patch("tools.delegate_tool._run_single_child") as mock_run:
        mock_run.side_effect = [_completed(0), _completed(1)]
        result = _call([{"goal": GOOD_A}, {"goal": goal}])
    assert "error" not in result
    assert len(result["results"]) == 2


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
