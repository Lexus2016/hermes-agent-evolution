"""Tests for the optional handoff collapse-mode (GitHub issue #319).

Context isolation already shipped: children never receive parent history; they
only get the explicit ``context`` string (``_build_child_system_prompt`` proves
this). The genuinely-new piece is an OPTIONAL ``handoff_mode='collapsed_summary'``
that routes the parent's recent conversation through the EXISTING
``ContextCompressor`` and prepends the summary to each child's ``context``.

These tests pin three contracts:

  1. Default (no handoff_mode)        -> context passed through unchanged.
  2. collapsed_summary mode           -> parent history compressed into context
                                         (compressor mocked, never a real call).
  3. empty / short / no-snapshot      -> no-op, context unchanged.

The compressor's ``_generate_summary`` is always mocked, so no test makes a
network call.
"""

import types

import pytest

import tools.delegate_tool as dt
from tools.delegate_tool import (
    HANDOFF_MODE_COLLAPSED_SUMMARY,
    _HANDOFF_COLLAPSE_HEADER,
    _apply_handoff_collapse,
    _build_collapsed_handoff_context,
    _collapsible_parent_turns,
)


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


class _FakeCompressor:
    """Stand-in for ContextCompressor exposing only ``_generate_summary``."""

    def __init__(self, summary="SUMMARY-OF-PARENT-HISTORY"):
        self._summary = summary
        self.calls = []

    def _generate_summary(self, turns, focus_topic=None):
        self.calls.append(list(turns))
        return self._summary


_DEFAULT_COMPRESSOR = object()  # sentinel: "give me a fresh _FakeCompressor"


def _make_parent(messages=None, compressor=_DEFAULT_COMPRESSOR):
    """Build a minimal parent_agent stub.

    ``messages`` becomes the ``_delegate_handoff_messages`` snapshot; pass None
    to omit the attribute entirely (simulating a transport that never stashes
    it). ``compressor`` defaults to a fresh _FakeCompressor; pass None to
    simulate an agent without a compressor.
    """
    parent = types.SimpleNamespace()
    if messages is not None:
        parent._delegate_handoff_messages = messages
    if compressor is _DEFAULT_COMPRESSOR:
        compressor = _FakeCompressor()
    parent.context_compressor = compressor
    return parent


def _sample_history():
    """A realistic multi-turn parent transcript (system + user + assistant)."""
    return [
        {"role": "system", "content": "You are Hermes."},
        {"role": "user", "content": "Refactor the auth module to use JWT."},
        {"role": "assistant", "content": "Sure, reading auth.py now."},
        {"role": "user", "content": "Also add a test for token expiry."},
        # In-flight assistant turn carrying the delegate_task tool call that
        # triggered this handoff — must be dropped from the collapse input.
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "delegate_task", "arguments": "{}"}}],
        },
    ]


# --------------------------------------------------------------------------- #
# 1. Default behavior unchanged (no handoff_mode)
# --------------------------------------------------------------------------- #


def test_no_mode_leaves_context_identical():
    """handoff_mode=None must not touch any task's context (byte-identical)."""
    parent = _make_parent(messages=_sample_history())
    task_list = [
        {"goal": "do thing", "context": "original-context"},
        {"goal": "other", "context": None},
    ]

    _apply_handoff_collapse(task_list, None, parent)

    assert task_list[0]["context"] == "original-context"
    assert task_list[1]["context"] is None
    # Compressor must never be consulted when no mode is requested.
    assert parent.context_compressor.calls == []


def test_unknown_mode_is_ignored_no_op():
    """An unrecognized handoff_mode degrades to today's behavior, not an error."""
    parent = _make_parent(messages=_sample_history())
    task_list = [{"goal": "g", "context": "ctx"}]

    _apply_handoff_collapse(task_list, "manager_decentralized_galaxy_brain", parent)

    assert task_list[0]["context"] == "ctx"
    assert parent.context_compressor.calls == []


# --------------------------------------------------------------------------- #
# 2. collapsed_summary mode compresses parent history into context
# --------------------------------------------------------------------------- #


def test_collapsed_summary_prepends_summary_to_existing_context():
    parent = _make_parent(messages=_sample_history())
    task_list = [{"goal": "g", "context": "caller-context"}]

    _apply_handoff_collapse(task_list, HANDOFF_MODE_COLLAPSED_SUMMARY, parent)

    ctx = task_list[0]["context"]
    # Header + summary present, caller context preserved AFTER it (background
    # first, foreground last).
    assert _HANDOFF_COLLAPSE_HEADER in ctx
    assert "SUMMARY-OF-PARENT-HISTORY" in ctx
    assert ctx.endswith("caller-context")
    assert ctx.index("SUMMARY-OF-PARENT-HISTORY") < ctx.index("caller-context")
    # Compressor consulted exactly once.
    assert len(parent.context_compressor.calls) == 1


def test_collapsed_summary_sets_context_when_none():
    parent = _make_parent(messages=_sample_history())
    task_list = [{"goal": "g", "context": None}]

    _apply_handoff_collapse(task_list, HANDOFF_MODE_COLLAPSED_SUMMARY, parent)

    ctx = task_list[0]["context"]
    assert ctx is not None
    assert ctx.startswith(_HANDOFF_COLLAPSE_HEADER)
    assert "SUMMARY-OF-PARENT-HISTORY" in ctx


def test_collapsed_summary_strips_system_and_inflight_delegate_turn():
    """The collapse input must exclude the system prompt and the delegate call."""
    parent = _make_parent(messages=_sample_history())

    turns = _collapsible_parent_turns(parent)

    roles = [t["role"] for t in turns]
    assert "system" not in roles  # system prompt dropped
    # The trailing assistant turn (delegate_task tool call) is dropped.
    assert not (turns[-1]["role"] == "assistant" and turns[-1].get("tool_calls"))
    # The substantive user/assistant turns survive.
    assert any(t["role"] == "user" for t in turns)


def test_collapsed_summary_is_generated_once_for_a_batch():
    """Batch tasks share one summary — the compressor runs once, not per task."""
    parent = _make_parent(messages=_sample_history())
    task_list = [
        {"goal": "a", "context": "ctx-a"},
        {"goal": "b", "context": "ctx-b"},
        {"goal": "c", "context": None},
    ]

    _apply_handoff_collapse(task_list, HANDOFF_MODE_COLLAPSED_SUMMARY, parent)

    assert len(parent.context_compressor.calls) == 1
    for task in task_list:
        assert "SUMMARY-OF-PARENT-HISTORY" in task["context"]
    assert task_list[0]["context"].endswith("ctx-a")
    assert task_list[1]["context"].endswith("ctx-b")
    assert task_list[2]["context"].startswith(_HANDOFF_COLLAPSE_HEADER)


# --------------------------------------------------------------------------- #
# 3. Empty / short / missing history -> no-op
# --------------------------------------------------------------------------- #


def test_empty_history_is_noop():
    parent = _make_parent(messages=[])
    task_list = [{"goal": "g", "context": "ctx"}]

    _apply_handoff_collapse(task_list, HANDOFF_MODE_COLLAPSED_SUMMARY, parent)

    assert task_list[0]["context"] == "ctx"
    assert parent.context_compressor.calls == []


def test_short_history_below_min_turns_is_noop():
    """A single substantive turn is too little to be worth summarizing."""
    # Only the system prompt + one user turn -> after stripping system, 1 turn.
    parent = _make_parent(
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]
    )
    task_list = [{"goal": "g", "context": "ctx"}]

    _apply_handoff_collapse(task_list, HANDOFF_MODE_COLLAPSED_SUMMARY, parent)

    assert task_list[0]["context"] == "ctx"
    assert parent.context_compressor.calls == []


def test_missing_snapshot_is_noop():
    """No _delegate_handoff_messages attribute -> safe no-op."""
    parent = _make_parent(messages=None)  # attribute omitted entirely
    task_list = [{"goal": "g", "context": "ctx"}]

    _apply_handoff_collapse(task_list, HANDOFF_MODE_COLLAPSED_SUMMARY, parent)

    assert task_list[0]["context"] == "ctx"


def test_missing_compressor_is_noop():
    parent = _make_parent(messages=_sample_history(), compressor=None)
    task_list = [{"goal": "g", "context": "ctx"}]

    _apply_handoff_collapse(task_list, HANDOFF_MODE_COLLAPSED_SUMMARY, parent)

    assert task_list[0]["context"] == "ctx"


def test_summary_returning_none_is_noop():
    """When the compressor fails/returns None, context is left unchanged."""
    parent = _make_parent(
        messages=_sample_history(), compressor=_FakeCompressor(summary=None)
    )
    task_list = [{"goal": "g", "context": "ctx"}]

    _apply_handoff_collapse(task_list, HANDOFF_MODE_COLLAPSED_SUMMARY, parent)

    assert task_list[0]["context"] == "ctx"


def test_summary_raising_is_swallowed_as_noop():
    """A compressor exception must never break delegation."""

    class _BoomCompressor:
        def _generate_summary(self, turns, focus_topic=None):
            raise RuntimeError("aux provider down")

    parent = _make_parent(messages=_sample_history(), compressor=_BoomCompressor())
    task_list = [{"goal": "g", "context": "ctx"}]

    _apply_handoff_collapse(task_list, HANDOFF_MODE_COLLAPSED_SUMMARY, parent)

    assert task_list[0]["context"] == "ctx"


# --------------------------------------------------------------------------- #
# Helper-level direct contract
# --------------------------------------------------------------------------- #


def test_build_collapsed_context_returns_existing_on_noop():
    parent = _make_parent(messages=[])
    assert _build_collapsed_handoff_context(parent, "keep-me") == "keep-me"
    assert _build_collapsed_handoff_context(parent, None) is None


# --------------------------------------------------------------------------- #
# 4. Param threading: delegate_task accepts handoff_mode and the schema/registry
#    handler forward it (default-safe — single-task flow exercised with a stub).
# --------------------------------------------------------------------------- #


def test_delegate_task_signature_accepts_handoff_mode():
    import inspect

    sig = inspect.signature(dt.delegate_task)
    assert "handoff_mode" in sig.parameters
    assert sig.parameters["handoff_mode"].default is None


def test_schema_exposes_handoff_mode_enum():
    props = dt.DELEGATE_TASK_SCHEMA["parameters"]["properties"]
    assert "handoff_mode" in props
    assert props["handoff_mode"]["enum"] == ["collapsed_summary"]


def test_apply_handoff_collapse_called_in_delegate_task(monkeypatch):
    """delegate_task must invoke the collapse hook (proving wiring), and the
    default None mode keeps it a no-op."""
    seen = {}

    def _spy(task_list, handoff_mode, parent_agent):
        seen["handoff_mode"] = handoff_mode
        seen["task_list"] = task_list

    monkeypatch.setattr(dt, "_apply_handoff_collapse", _spy)
    # Short-circuit the heavy child build/run so we only exercise the wiring up
    # to the collapse hook. Raising a sentinel after the hook lets us assert it
    # ran without standing up a full child agent.
    monkeypatch.setattr(dt, "is_spawn_paused", lambda: False)

    class _Sentinel(Exception):
        pass

    def _boom(*a, **k):
        raise _Sentinel

    # _build_child_agent runs AFTER _apply_handoff_collapse; blow up there.
    monkeypatch.setattr(dt, "_build_child_agent", _boom)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda cfg, pa: {
        "model": "m", "provider": "", "base_url": "", "api_key": "", "api_mode": "",
        "command": None, "args": None,
    })

    parent = _make_parent(messages=_sample_history())
    # Mirror the attributes delegate_task reads before the child build.
    parent._delegate_depth = 0

    with pytest.raises(_Sentinel):
        dt.delegate_task(
            goal="do the thing",
            context="ctx",
            handoff_mode=None,
            parent_agent=parent,
        )

    assert seen.get("handoff_mode") is None
    assert seen["task_list"][0]["goal"] == "do the thing"
