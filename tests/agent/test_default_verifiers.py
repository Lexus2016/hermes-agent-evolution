"""Tests for the Gather-Act-Verify default verifiers + retry-on-mismatch (#294).

Builds on the #293 registry/factories. Covers:

* the default verifiers for the built-in mutating tools (write_file /
  write_to_file → file-exists, patch → file-exists over resolved targets,
  terminal → exit-code + optional grep), and that ``register_default_verifiers``
  is idempotent and non-clobbering;
* the pure retry policy (``decide_retry`` / ``RetryDecision``) capped at exactly
  one re-run, then abort;
* the stable per-call signature used to enforce the cap across a turn;
* the byte-identical-when-OFF guarantee on the lazily-built agent state.

Run just this file (full collection breaks on a missing pypy dep)::

    python -m pytest tests/agent/test_default_verifiers.py -q
"""

from __future__ import annotations

import json

import pytest

from agent.verify_policy import (
    MAX_VERIFY_RETRIES,
    MUTATING_TOOL_NAMES,
    RetryDecision,
    VerifyCall,
    VerifyOutcome,
    VerifyPolicy,
    _AgentVerifyState,
    _patch_path_resolver,
    call_signature,
    decide_retry,
    make_terminal_verifier,
    register_default_verifiers,
)


# ── register_default_verifiers: coverage + idempotency ───────────────────────


def test_register_default_verifiers_covers_every_mutating_tool():
    policy = VerifyPolicy()
    assert policy.missing_verifier_tools() == MUTATING_TOOL_NAMES
    returned = register_default_verifiers(policy)
    # Returns the same policy for chaining.
    assert returned is policy
    # Every canonical mutating tool now has a verifier.
    assert policy.missing_verifier_tools() == frozenset()
    for tool in MUTATING_TOOL_NAMES:
        assert policy.has_verifier(tool) is True


def test_register_default_verifiers_is_idempotent():
    policy = VerifyPolicy()
    register_default_verifiers(policy)
    counts_before = {t: len(policy.lookup(t)) for t in MUTATING_TOOL_NAMES}
    # A second bootstrap must not stack duplicate defaults.
    register_default_verifiers(policy)
    counts_after = {t: len(policy.lookup(t)) for t in MUTATING_TOOL_NAMES}
    assert counts_before == counts_after
    assert all(c == 1 for c in counts_after.values())


def test_register_default_verifiers_preserves_custom_verifier():
    policy = VerifyPolicy()
    policy.register("write_file", lambda call: True, name="skill-custom")
    register_default_verifiers(policy)
    names = [rv.name for rv in policy.lookup("write_file")]
    # The pre-registered custom verifier wins; no default appended for it.
    assert names == ["skill-custom"]
    # Other tools still get their default.
    assert policy.has_verifier("patch") is True


# ── default write_file / write_to_file verifier (file-exists) ────────────────


def test_default_write_file_verifier_ok_when_file_exists(tmp_path):
    f = tmp_path / "out.txt"
    f.write_text("data")
    policy = register_default_verifiers(VerifyPolicy())
    outcome = policy.consult("write_file", {"path": str(f)}, result="{}")
    assert outcome.status == "ok"


def test_default_write_file_verifier_mismatch_when_missing(tmp_path):
    missing = tmp_path / "absent.txt"
    policy = register_default_verifiers(VerifyPolicy())
    outcome = policy.consult("write_file", {"path": str(missing)})
    assert outcome.status == "mismatch"


def test_default_write_to_file_verifier_uses_file_arg(tmp_path):
    f = tmp_path / "wtf.txt"
    f.write_text("x")
    policy = register_default_verifiers(VerifyPolicy())
    assert policy.consult("write_to_file", {"file": str(f)}).status == "ok"
    assert (
        policy.consult("write_to_file", {"file": str(tmp_path / "no")}).status
        == "mismatch"
    )


# ── default patch verifier (file-exists over resolved targets) ───────────────


def test_default_patch_verifier_replace_mode_uses_path(tmp_path):
    f = tmp_path / "edited.py"
    f.write_text("print(1)")
    policy = register_default_verifiers(VerifyPolicy())
    ok = policy.consult("patch", {"mode": "replace", "path": str(f)})
    assert ok.status == "ok"
    gone = policy.consult("patch", {"mode": "replace", "path": str(tmp_path / "x")})
    assert gone.status == "mismatch"


def test_patch_path_resolver_extracts_v4a_update_and_add_headers(tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    patch_body = (
        f"*** Update File: {a}\n"
        "@@\n-old\n+new\n"
        f"*** Add File: {b}\n"
        "+created\n"
    )
    paths = _patch_path_resolver(VerifyCall("patch", {"mode": "patch", "patch": patch_body}))
    assert paths == [str(a), str(b)]


def test_default_patch_verifier_v4a_mismatch_when_target_missing(tmp_path):
    target = tmp_path / "ghost.txt"  # never created
    patch_body = f"*** Update File: {target}\n@@\n-a\n+b\n"
    policy = register_default_verifiers(VerifyPolicy())
    outcome = policy.consult("patch", {"mode": "patch", "patch": patch_body})
    assert outcome.status == "mismatch"


def test_patch_path_resolver_skips_delete_headers(tmp_path):
    # A Delete File header means the path SHOULD be absent — never check it.
    patch_body = f"*** Delete File: {tmp_path / 'removed.txt'}\n"
    paths = _patch_path_resolver(VerifyCall("patch", {"mode": "patch", "patch": patch_body}))
    assert paths == []


def test_default_patch_verifier_unresolvable_shape_is_confirmed():
    # No path, no patch body → nothing to check → confirmed, not a false mismatch.
    policy = register_default_verifiers(VerifyPolicy())
    assert policy.consult("patch", {"mode": "patch"}).status == "ok"


# ── default terminal verifier (exit-code + grep, from the call's JSON) ───────


def test_default_terminal_verifier_ok_on_exit_zero():
    policy = register_default_verifiers(VerifyPolicy())
    result = json.dumps({"output": "done", "exit_code": 0})
    assert policy.consult("terminal", {"command": "make"}, result=result).status == "ok"


def test_default_terminal_verifier_mismatch_on_nonzero_exit():
    policy = register_default_verifiers(VerifyPolicy())
    result = json.dumps({"output": "boom", "exit_code": 1})
    assert (
        policy.consult("terminal", {"command": "make"}, result=result).status
        == "mismatch"
    )


def test_terminal_verifier_grep_requires_all_needles():
    verify = make_terminal_verifier(expect_in_output=["built", "OK"])
    good = json.dumps({"output": "built target OK", "exit_code": 0})
    assert verify(VerifyCall("terminal", {}, result=good)) is True
    # exit 0 but missing a needle → not confirmed.
    partial = json.dumps({"output": "built target", "exit_code": 0})
    assert verify(VerifyCall("terminal", {}, result=partial)) is False


def test_terminal_verifier_single_string_needle():
    verify = make_terminal_verifier(expect_in_output="artifact.bin")
    hit = json.dumps({"output": "wrote artifact.bin", "exit_code": 0})
    miss = json.dumps({"output": "wrote other.bin", "exit_code": 0})
    assert verify(VerifyCall("terminal", {}, result=hit)) is True
    assert verify(VerifyCall("terminal", {}, result=miss)) is False


def test_terminal_verifier_unreadable_result_is_confirmed():
    verify = make_terminal_verifier()
    # Non-JSON, empty, and JSON-without-exit_code all → nothing to check → True.
    assert verify(VerifyCall("terminal", {}, result="not json")) is True
    assert verify(VerifyCall("terminal", {}, result="")) is True
    assert verify(VerifyCall("terminal", {}, result=json.dumps({"output": "x"}))) is True
    assert verify(VerifyCall("terminal", {}, result=None)) is True


# ── retry policy: capped at one, then abort ──────────────────────────────────


def test_max_verify_retries_is_one():
    # The whole safety story rests on this: never loop more than once.
    assert MAX_VERIFY_RETRIES == 1


@pytest.mark.parametrize("status", ["ok", "skipped", "error"])
def test_decide_retry_no_action_on_non_mismatch(status):
    outcome = VerifyOutcome(status=status, tool_name="write_file", verifier="v")
    decision = decide_retry(outcome, attempts=0)
    assert decision == RetryDecision(retry=False, abort=False)
    assert decision.acted is False
    assert decision.feedback == ""


def test_decide_retry_first_mismatch_retries_and_surfaces_feedback():
    outcome = VerifyOutcome.mismatch("write_file", "default-file-exists", "not on disk")
    decision = decide_retry(outcome, attempts=0)
    assert decision.retry is True
    assert decision.abort is False
    assert decision.acted is True
    assert "write_file" in decision.feedback
    assert "Re-running" in decision.feedback


def test_decide_retry_second_mismatch_aborts():
    outcome = VerifyOutcome.mismatch("write_file", "default-file-exists", "still gone")
    decision = decide_retry(outcome, attempts=MAX_VERIFY_RETRIES)
    assert decision.retry is False
    assert decision.abort is True
    assert decision.acted is True
    assert "aborting to the user" in decision.feedback


def test_decide_retry_never_retries_more_than_once():
    outcome = VerifyOutcome.mismatch("patch", "default-patch-exists")
    # Any attempt count at or beyond the cap aborts; only attempts==0 retries.
    assert decide_retry(outcome, attempts=0).retry is True
    for attempts in range(MAX_VERIFY_RETRIES, MAX_VERIFY_RETRIES + 3):
        d = decide_retry(outcome, attempts=attempts)
        assert d.retry is False
        assert d.abort is True


def test_retry_decision_is_frozen():
    decision = RetryDecision(retry=True, abort=False)
    with pytest.raises(Exception):
        decision.retry = False  # type: ignore[misc]


# ── call_signature: stable per-call key for the cap ──────────────────────────


def test_call_signature_stable_for_same_call():
    a = call_signature("write_file", {"path": "/tmp/x", "content": "y"})
    b = call_signature("write_file", {"content": "y", "path": "/tmp/x"})
    # Arg order must not change the signature (sort_keys).
    assert a == b


def test_call_signature_distinguishes_different_calls():
    base = call_signature("write_file", {"path": "/tmp/x"})
    assert base != call_signature("write_file", {"path": "/tmp/y"})
    assert base != call_signature("patch", {"path": "/tmp/x"})


def test_call_signature_tolerates_unserializable_args():
    # Must not raise on values json can't serialize natively.
    sig = call_signature("terminal", {"obj": object()})
    assert sig.startswith("terminal::")


# ── agent state: defaults flag + per-call counter (gate-off invariant) ───────


def test_agent_verify_state_defaults_off_and_empty():
    state = _AgentVerifyState()
    # Fresh state registers nothing until bootstrap runs behind the gate.
    assert state.defaults_registered is False
    assert state.registry.registered_tools == frozenset()
    assert state.retry_attempts == {}
    assert state.outcomes == []


def test_agent_verify_state_counter_enforces_cap_across_calls():
    # Simulate the seam's bookkeeping: same signature bumps once, caps at 1.
    state = _AgentVerifyState()
    register_default_verifiers(state.registry)
    state.defaults_registered = True
    sig = call_signature("write_file", {"path": "/tmp/missing"})

    # First mismatch → retry, counter goes 0 → 1.
    first = decide_retry(VerifyOutcome.mismatch("write_file", "v"), state.retry_attempts.get(sig, 0))
    assert first.retry is True
    state.retry_attempts[sig] = state.retry_attempts.get(sig, 0) + 1

    # Second mismatch for the SAME signature → abort (cap hit).
    second = decide_retry(VerifyOutcome.mismatch("write_file", "v"), state.retry_attempts.get(sig, 0))
    assert second.abort is True
    assert state.retry_attempts[sig] == 1
