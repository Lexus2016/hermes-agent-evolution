"""Lean Phase 1 — learn from user corrections.

Tests the deterministic correction detector, the transient->durable
generalization guard (recurrence tracker), provenance, and the symmetric
unlearn path.

Design under test (``agent/correction_learning.py``):

- ``detect_correction(...)`` — deterministic. Inspects a *completed* turn
  (its ``messages`` list + interrupt state) and returns a small structured
  ``CorrectionRecord`` if the turn ended in a structured correction
  (INTERRUPT / DENY / STEER), else ``None``. No fuzzy text regex.

- ``CorrectionLearner`` — owns a fail-open JSON store under a per-profile
  directory. ``record(...)`` applies the generalization guard:
  a correction is TRANSIENT by default and becomes DURABLE on cross-session
  EVIDENCE — the same signature recurs across >=2 distinct sessions. The
  ``record(remember=True)`` fast-path also promotes durably and is exercised
  here at the unit level, but NOTE it is not wired to any production caller in
  Phase 1 (explicit "remember this" is deferred); recurrence is the sole
  production durable trigger. Durable items are
  written through a memory-store sink (the real re-injection path) and a
  provenance ledger entry is recorded. ``unlearn(provenance_id)`` removes
  a durable item (symmetric, reversible).

The store directory is injected for test isolation; in production it
resolves under ``get_hermes_home()/corrections``.
"""

from __future__ import annotations

import json

import pytest

from agent.correction_learning import (
    CorrectionLearner,
    CorrectionRecord,
    detect_correction,
)
from agent.prompt_builder import STEER_MARKER_OPEN, format_steer_marker


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeMemorySink:
    """Stand-in for the durable re-injection path (MEMORY.md).

    Mirrors ``MemoryStore.add`` / ``remove`` just enough that a durable write
    lands somewhere the injection path would read, and an unlearn removes it.
    ``entries`` is what a fresh session's ``load_from_disk`` would surface.
    """

    def __init__(self):
        self.entries: list[str] = []

    def add(self, target, content, **kwargs):
        content = content.strip()
        if content in self.entries:
            return {"success": True, "message": "Entry already exists"}
        self.entries.append(content)
        return {"success": True, "message": "Entry added"}

    def remove(self, target, content_substr, **kwargs):
        before = len(self.entries)
        self.entries = [e for e in self.entries if content_substr not in e]
        return {"success": len(self.entries) < before}

    # What a future session would inject.
    def injected_text(self) -> str:
        return "\n".join(self.entries)


def _learner(tmp_path, sink=None):
    return CorrectionLearner(
        store_dir=tmp_path / "corrections",
        memory_sink=sink if sink is not None else FakeMemorySink(),
    )


# ---------------------------------------------------------------------------
# 1. DETECTION (deterministic)
# ---------------------------------------------------------------------------


def test_detect_interrupt():
    messages = [
        {"role": "user", "content": "refactor module X"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "write_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    rec = detect_correction(
        messages,
        interrupted=True,
        interrupt_message="stop, do it in TypeScript instead",
        turn_exit_reason="interrupted_by_user",
        session_id="s1",
    )
    assert rec is not None
    assert rec.kind == "INTERRUPT"
    assert "TypeScript" in rec.context
    assert rec.session_id == "s1"
    assert rec.signature  # stable, non-empty
    assert rec.ts  # timestamp recorded


def test_detect_deny():
    # A GENUINE user denial carries ``user_denied: True`` (stamped by the
    # approval flow). That marker — not the bare ``status: "blocked"`` — is what
    # the detector keys on.
    messages = [
        {"role": "user", "content": "clean up"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps(
            {"output": "", "exit_code": -1,
             "error": "Command denied: rm -rf /tmp/x", "status": "blocked",
             "user_denied": True})},
    ]
    rec = detect_correction(
        messages, interrupted=False, interrupt_message=None,
        turn_exit_reason="text_response(stop)", session_id="s1",
    )
    assert rec is not None
    assert rec.kind == "DENY"


def test_automatic_dangerous_block_not_detected_as_deny():
    # X2: an AUTOMATIC dangerous-command block (no user involved) sets
    # ``status: "blocked"`` but NO ``user_denied`` marker. It must NOT mint a
    # false "user correction".
    messages = [
        {"role": "user", "content": "clean up"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps(
            {"output": "", "exit_code": -1,
             "error": "Command denied: recursive delete", "status": "blocked"})},
    ]
    rec = detect_correction(
        messages, interrupted=False, interrupt_message=None,
        turn_exit_reason="text_response(stop)", session_id="s1",
    )
    assert rec is None


def test_automatic_workdir_validation_block_not_detected_as_deny():
    # X2: the workdir shell-injection validator also emits ``status: "blocked"``
    # with no user involvement -> not a correction.
    messages = [
        {"role": "user", "content": "build it"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps(
            {"output": "", "exit_code": -1,
             "error": "Blocked: workdir contains disallowed character ';'.",
             "status": "blocked"})},
    ]
    rec = detect_correction(
        messages, interrupted=False, interrupt_message=None,
        turn_exit_reason="text_response(stop)", session_id="s1",
    )
    assert rec is None


def test_detect_steer():
    steer = format_steer_marker("actually use pytest not unittest")
    assert STEER_MARKER_OPEN in steer  # sanity: marker present
    messages = [
        {"role": "user", "content": "write tests"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "file body" + steer},
    ]
    rec = detect_correction(
        messages, interrupted=False, interrupt_message=None,
        turn_exit_reason="text_response(stop)", session_id="s1",
    )
    assert rec is not None
    assert rec.kind == "STEER"
    assert "pytest" in rec.context


def test_no_correction_on_normal_turn():
    messages = [
        {"role": "user", "content": "summarize this"},
        {"role": "assistant", "content": "Here is the summary."},
    ]
    rec = detect_correction(
        messages, interrupted=False, interrupt_message=None,
        turn_exit_reason="text_response(stop)", session_id="s1",
    )
    assert rec is None


def test_no_correction_on_plain_interrupt_without_message():
    # User hit stop but gave no redirect text. Not a structured correction
    # we can learn from (nothing to capture); existing interrupt behavior is
    # preserved by the caller. Detector returns None.
    messages = [{"role": "user", "content": "do a thing"}]
    rec = detect_correction(
        messages, interrupted=True, interrupt_message=None,
        turn_exit_reason="interrupted_by_user", session_id="s1",
    )
    assert rec is None


def test_same_correction_same_signature_across_sessions():
    # Stable signature is what the recurrence tracker keys on.
    def mk(sess):
        return detect_correction(
            [{"role": "user", "content": "x"},
             {"role": "assistant", "content": "", "tool_calls": [
                 {"id": "c1", "function": {"name": "terminal", "arguments": "{}"}}]},
             {"role": "tool", "tool_call_id": "c1", "content": json.dumps(
                 {"error": "Command denied: rm -rf build", "status": "blocked",
                  "user_denied": True})}],
            interrupted=False, interrupt_message=None,
            turn_exit_reason="t", session_id=sess,
        )
    a = mk("s1")
    b = mk("s2")
    assert a.signature == b.signature


# ---------------------------------------------------------------------------
# 2. GENERALIZATION GUARD (transient -> durable)
# ---------------------------------------------------------------------------


def test_first_sighting_stays_transient(tmp_path):
    sink = FakeMemorySink()
    learner = _learner(tmp_path, sink)
    rec = CorrectionRecord(
        kind="DENY", signature="sig-A", context="do not rm build",
        session_id="s1", ts="2026-01-01T00:00:00Z",
    )
    outcome = learner.record(rec)
    assert outcome["tier"] == "transient"
    assert outcome["durable"] is False
    # NEGATIVE CONTROL: nothing written to the durable injection path.
    assert sink.entries == []
    assert sink.injected_text() == ""
    # And no durable ledger entry.
    assert learner.list_durable() == []


def test_second_sighting_new_session_promotes_durable(tmp_path):
    sink = FakeMemorySink()
    learner = _learner(tmp_path, sink)
    rec1 = CorrectionRecord(
        kind="DENY", signature="sig-A", context="do not rm build",
        session_id="s1", ts="2026-01-01T00:00:00Z",
    )
    rec2 = CorrectionRecord(
        kind="DENY", signature="sig-A", context="do not rm build",
        session_id="s2", ts="2026-01-02T00:00:00Z",
    )
    first = learner.record(rec1)
    assert first["durable"] is False
    assert sink.entries == []

    second = learner.record(rec2)
    assert second["tier"] == "durable"
    assert second["durable"] is True
    # The durable store now contains it, where a NEW session would inject it.
    assert sink.entries, "durable write must land on the injection path"
    assert "build" in sink.injected_text()
    # Provenance ledger records it with origin signal + reason.
    durable = learner.list_durable()
    assert len(durable) == 1
    assert durable[0]["provenance_id"] == second["provenance_id"]
    assert durable[0]["origin_kind"] == "DENY"
    assert durable[0]["reason"] == "recurrence"
    assert durable[0]["signature"] == "sig-A"


def test_same_session_twice_does_not_promote(tmp_path):
    # Recurrence requires DISTINCT sessions. A loop within one session must
    # not look like cross-session evidence (over-promotion guard).
    sink = FakeMemorySink()
    learner = _learner(tmp_path, sink)
    rec = CorrectionRecord(
        kind="DENY", signature="sig-A", context="x",
        session_id="s1", ts="2026-01-01T00:00:00Z",
    )
    learner.record(rec)
    second = learner.record(rec)  # same session_id
    assert second["durable"] is False
    assert sink.entries == []


def test_explicit_remember_promotes_on_first_sighting(tmp_path):
    sink = FakeMemorySink()
    learner = _learner(tmp_path, sink)
    rec = CorrectionRecord(
        kind="STEER", signature="sig-pref", context="use pytest not unittest",
        session_id="s1", ts="2026-01-01T00:00:00Z",
    )
    outcome = learner.record(rec, remember=True)
    assert outcome["tier"] == "durable"
    assert outcome["durable"] is True
    assert "pytest" in sink.injected_text()
    durable = learner.list_durable()
    assert durable[0]["reason"] == "explicit_remember"


def test_one_off_correction_never_injects(tmp_path):
    # NEGATIVE CONTROL (explicit): seen once, no remember -> stays ephemeral,
    # no durable write, no injection. This is the safety core of Phase 1.
    sink = FakeMemorySink()
    learner = _learner(tmp_path, sink)
    rec = CorrectionRecord(
        kind="INTERRUPT", signature="sig-oneoff", context="this one time, skip linting",
        session_id="s1", ts="2026-01-01T00:00:00Z",
    )
    outcome = learner.record(rec)
    assert outcome["durable"] is False
    assert sink.entries == []
    assert sink.injected_text() == ""


# ---------------------------------------------------------------------------
# 3. PROVENANCE + UNLEARN (reversibility)
# ---------------------------------------------------------------------------


def test_provenance_recorded_on_durable(tmp_path):
    sink = FakeMemorySink()
    learner = _learner(tmp_path, sink)
    rec = CorrectionRecord(
        kind="STEER", signature="sig-pref", context="use pytest not unittest",
        session_id="s1", ts="2026-01-01T00:00:00Z",
    )
    outcome = learner.record(rec, remember=True)
    entry = learner.get_durable(outcome["provenance_id"])
    assert entry is not None
    assert entry["origin_kind"] == "STEER"
    assert entry["signature"] == "sig-pref"
    assert entry["session_id"] == "s1"
    assert entry["tier"] == "durable"
    assert entry["ts"]
    assert entry["promoted_ts"]


def test_unlearn_removes_durable_and_stops_injection(tmp_path):
    sink = FakeMemorySink()
    learner = _learner(tmp_path, sink)
    rec = CorrectionRecord(
        kind="STEER", signature="sig-pref", context="use pytest not unittest",
        session_id="s1", ts="2026-01-01T00:00:00Z",
    )
    outcome = learner.record(rec, remember=True)
    pid = outcome["provenance_id"]
    assert "pytest" in sink.injected_text()

    ok = learner.unlearn(pid)
    assert ok is True
    # No longer durable, no longer injected.
    assert learner.get_durable(pid) is None
    assert learner.list_durable() == []
    assert "pytest" not in sink.injected_text()


def test_unlearn_unknown_id_is_safe(tmp_path):
    learner = _learner(tmp_path)
    assert learner.unlearn("does-not-exist") is False


def test_promotion_is_idempotent_no_duplicate_ledger_or_memory(tmp_path):
    # Once durable, further sightings of the SAME signature must NOT create
    # duplicate ledger entries or re-write memory (ledger-bloat guard found in
    # independent review). The item stays a single durable rule.
    sink = FakeMemorySink()
    learner = _learner(tmp_path, sink)

    def sight(sess):
        rec = CorrectionRecord(
            kind="DENY", signature="sig-A", context="do not rm build",
            session_id=sess, ts="2026-01-01T00:00:00Z",
        )
        return learner.record(rec)

    sight("s1")  # transient
    out2 = sight("s2")  # promote
    assert out2["durable"] is True
    out3 = sight("s3")  # already durable
    assert out3["durable"] is True
    # Exactly ONE durable ledger entry, ONE memory entry.
    assert len(learner.list_durable()) == 1
    assert len(sink.entries) == 1
    # The provenance id is stable across repeat sightings.
    assert out3["provenance_id"] == out2["provenance_id"]
    assert out3["reason"] == "already_durable"


def test_unlearn_resets_recurrence_so_no_instant_repromote(tmp_path):
    # After unlearn, the same correction must require fresh evidence again —
    # not snap straight back to durable on the next sighting (independent
    # review caught that recurrence history outlived the durable entry).
    sink = FakeMemorySink()
    learner = _learner(tmp_path, sink)

    def sight(sess):
        return learner.record(CorrectionRecord(
            kind="DENY", signature="sig-A", context="x",
            session_id=sess, ts="t",
        ))

    sight("s1")
    out2 = sight("s2")
    assert out2["durable"] is True
    assert learner.unlearn(out2["provenance_id"]) is True
    assert "x" not in sink.injected_text()

    # Next single sighting must be transient again (evidence was reset).
    out3 = sight("s3")
    assert out3["durable"] is False
    assert sink.entries == []


# ---------------------------------------------------------------------------
# 4. FAIL-OPEN PERSISTENCE
# ---------------------------------------------------------------------------


def test_state_persists_across_learner_instances(tmp_path):
    # Recurrence must survive process restarts (cross-session evidence is the
    # whole point). A second learner over the same dir sees the first sighting.
    sink = FakeMemorySink()
    rec1 = CorrectionRecord(
        kind="DENY", signature="sig-A", context="x",
        session_id="s1", ts="2026-01-01T00:00:00Z",
    )
    rec2 = CorrectionRecord(
        kind="DENY", signature="sig-A", context="x",
        session_id="s2", ts="2026-01-02T00:00:00Z",
    )
    _learner(tmp_path, sink).record(rec1)
    # Fresh learner instance, same store dir, NEW session.
    second = _learner(tmp_path, sink).record(rec2)
    assert second["durable"] is True


def test_record_failopen_on_unwritable_store(tmp_path, monkeypatch):
    # A broken store must never crash the turn. record() returns a result and
    # does not raise even if persistence fails.
    learner = _learner(tmp_path)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(learner, "_write_json", boom)
    rec = CorrectionRecord(
        kind="DENY", signature="sig-A", context="x",
        session_id="s1", ts="2026-01-01T00:00:00Z",
    )
    # Must not raise.
    outcome = learner.record(rec)
    assert outcome["durable"] is False
