"""Tests for the Gather-Act-Verify verify-hook registry (#293).

Covers the registry contract (register / lookup / consult), the three verifier
kinds (command, file-existence, symbolic predicate), the advisory ``consult``
outcomes, the opt-in enable gate, and the #293 policy assertion that every
mutating tool can register a verifier — without any retry/blocking (that is the
sibling #294).
"""

from __future__ import annotations

import os

import pytest

from agent.verify_policy import (
    MUTATING_TOOL_NAMES,
    RegisteredVerifier,
    VerifyCall,
    VerifyOutcome,
    VerifyPolicy,
    make_command_verifier,
    make_file_exists_verifier,
    make_predicate_verifier,
    split_command,
    verify_policy_enabled,
)


# ── VerifyOutcome primitives ─────────────────────────────────────────────────


def test_verify_outcome_constructors_set_status_and_flags():
    ok = VerifyOutcome.ok("write_file", "exists", "landed")
    assert ok.status == "ok"
    assert ok.confirmed is True
    assert ok.ran is True
    assert ok.verifier == "exists"
    assert ok.detail == "landed"

    mismatch = VerifyOutcome.mismatch("patch", "exists")
    assert mismatch.status == "mismatch"
    assert mismatch.confirmed is False
    assert mismatch.ran is True

    err = VerifyOutcome.error("terminal", "cmd", "boom")
    assert err.status == "error"
    assert err.confirmed is False
    assert err.ran is True

    skipped = VerifyOutcome.skipped("memory")
    assert skipped.status == "skipped"
    assert skipped.confirmed is False
    assert skipped.ran is False


def test_verify_outcome_is_frozen():
    outcome = VerifyOutcome.ok("write_file", "exists")
    with pytest.raises(Exception):
        outcome.status = "mismatch"  # type: ignore[misc]


# ── registry: register / lookup ──────────────────────────────────────────────


def test_register_and_lookup_roundtrip():
    policy = VerifyPolicy()
    assert policy.has_verifier("write_file") is False
    assert policy.lookup("write_file") == ()

    policy.register("write_file", lambda call: True, name="always")
    assert policy.has_verifier("write_file") is True
    registered = policy.lookup("write_file")
    assert len(registered) == 1
    assert isinstance(registered[0], RegisteredVerifier)
    assert registered[0].name == "always"
    assert policy.registered_tools == frozenset({"write_file"})


def test_register_defaults_name_from_callable():
    policy = VerifyPolicy()

    def my_checker(call: VerifyCall) -> bool:
        return True

    policy.register("patch", my_checker)
    assert policy.lookup("patch")[0].name == "my_checker"


def test_register_rejects_empty_tool_name_and_non_callable():
    policy = VerifyPolicy()
    with pytest.raises(ValueError):
        policy.register("", lambda call: True)
    with pytest.raises(TypeError):
        policy.register("write_file", "not callable")  # type: ignore[arg-type]


def test_unregister_removes_all_verifiers_for_tool():
    policy = VerifyPolicy()
    policy.register("write_file", lambda call: True)
    policy.register("write_file", lambda call: True)
    assert len(policy.lookup("write_file")) == 2
    policy.unregister("write_file")
    assert policy.has_verifier("write_file") is False
    # idempotent
    policy.unregister("write_file")


# ── #293 policy: mutating tools MUST be verifiable ───────────────────────────


def test_canonical_mutating_tool_names():
    assert MUTATING_TOOL_NAMES == frozenset(
        {"write_file", "patch", "terminal", "write_to_file"}
    )


def test_missing_verifier_tools_reports_uncovered_mutating_tools():
    policy = VerifyPolicy()
    # Nothing registered → every mutating tool is uncovered.
    assert policy.missing_verifier_tools() == MUTATING_TOOL_NAMES

    for tool in MUTATING_TOOL_NAMES:
        policy.register(tool, lambda call: True)
    # Full coverage → no gaps.
    assert policy.missing_verifier_tools() == frozenset()


# ── consult: advisory outcomes ───────────────────────────────────────────────


def test_consult_skips_when_no_verifier_registered():
    policy = VerifyPolicy()
    outcome = policy.consult("write_file", {"path": "/tmp/x"})
    assert outcome.status == "skipped"
    assert outcome.tool_name == "write_file"
    assert outcome.ran is False


def test_consult_ok_when_verifier_confirms():
    policy = VerifyPolicy()
    policy.register("write_file", lambda call: True, name="ok-checker")
    outcome = policy.consult("write_file", {"path": "/tmp/x"}, result="{}")
    assert outcome.status == "ok"
    assert outcome.confirmed is True
    assert outcome.verifier == "ok-checker"


def test_consult_mismatch_when_verifier_denies():
    policy = VerifyPolicy()
    policy.register("patch", lambda call: False, name="denier")
    outcome = policy.consult("patch", {"path": "/tmp/x"})
    assert outcome.status == "mismatch"
    assert outcome.confirmed is False
    assert outcome.verifier == "denier"


def test_consult_error_when_verifier_raises_and_does_not_propagate():
    policy = VerifyPolicy()

    def boom(call: VerifyCall) -> bool:
        raise RuntimeError("kaboom")

    policy.register("terminal", boom, name="exploder")
    # Must NOT raise — a buggy verifier can never crash the turn.
    outcome = policy.consult("terminal", {"command": "true"})
    assert outcome.status == "error"
    assert outcome.verifier == "exploder"
    assert "kaboom" in outcome.detail


def test_consult_runs_verifiers_in_order_first_failure_wins():
    policy = VerifyPolicy()
    calls: list[str] = []

    def first(call: VerifyCall) -> bool:
        calls.append("first")
        return True

    def second(call: VerifyCall) -> bool:
        calls.append("second")
        return False

    def third(call: VerifyCall) -> bool:  # should never run
        calls.append("third")
        return True

    policy.register("write_file", first, name="first")
    policy.register("write_file", second, name="second")
    policy.register("write_file", third, name="third")
    outcome = policy.consult("write_file", {"path": "/tmp/x"})
    assert outcome.status == "mismatch"
    assert outcome.verifier == "second"
    assert calls == ["first", "second"]  # short-circuits before third


def test_consult_passes_call_view_to_verifier():
    policy = VerifyPolicy()
    seen: dict[str, object] = {}

    def capture(call: VerifyCall) -> bool:
        seen["tool"] = call.tool_name
        seen["args"] = dict(call.args)
        seen["result"] = call.result
        return True

    policy.register("write_file", capture)
    policy.consult("write_file", {"path": "/tmp/x"}, result="done")
    assert seen == {
        "tool": "write_file",
        "args": {"path": "/tmp/x"},
        "result": "done",
    }


# ── file-existence verifier ──────────────────────────────────────────────────


def test_file_exists_verifier_confirms_existing_path(tmp_path):
    f = tmp_path / "created.txt"
    f.write_text("hi")
    policy = VerifyPolicy()
    policy.register("write_file", make_file_exists_verifier(), name="exists")
    outcome = policy.consult("write_file", {"path": str(f)})
    assert outcome.status == "ok"


def test_file_exists_verifier_mismatch_on_missing_path(tmp_path):
    missing = tmp_path / "nope.txt"
    policy = VerifyPolicy()
    policy.register("write_file", make_file_exists_verifier(), name="exists")
    outcome = policy.consult("write_file", {"path": str(missing)})
    assert outcome.status == "mismatch"


def test_file_exists_verifier_reads_write_to_file_arg(tmp_path):
    f = tmp_path / "wtf.txt"
    f.write_text("x")
    verify = make_file_exists_verifier()
    assert verify(VerifyCall("write_to_file", {"file": str(f)})) is True
    assert verify(VerifyCall("write_to_file", {"file": str(tmp_path / "absent")})) is False


def test_file_exists_verifier_no_path_is_treated_as_nothing_to_check():
    verify = make_file_exists_verifier()
    # terminal-style call: no path arg → not this verifier's job → confirmed
    assert verify(VerifyCall("terminal", {"command": "ls"})) is True


def test_file_exists_verifier_custom_resolver(tmp_path):
    a = tmp_path / "a"
    a.write_text("1")
    b = tmp_path / "b"  # missing
    verify = make_file_exists_verifier(
        path_resolver=lambda call: [str(a), str(b)]
    )
    assert verify(VerifyCall("patch", {})) is False
    b.write_text("2")
    assert verify(VerifyCall("patch", {})) is True


# ── command verifier ─────────────────────────────────────────────────────────


def test_command_verifier_shell_string_exit_zero_confirms():
    policy = VerifyPolicy()
    policy.register("terminal", make_command_verifier("true"), name="cmd")
    assert policy.consult("terminal", {"command": "x"}).status == "ok"


def test_command_verifier_shell_string_nonzero_mismatch():
    policy = VerifyPolicy()
    policy.register("terminal", make_command_verifier("false"), name="cmd")
    assert policy.consult("terminal", {"command": "x"}).status == "mismatch"


def test_command_verifier_argv_list_form(tmp_path):
    target = tmp_path / "artifact"
    target.write_text("ok")
    verify = make_command_verifier(["test", "-f", str(target)])
    assert verify(VerifyCall("terminal", {})) is True
    target.unlink()
    assert verify(VerifyCall("terminal", {})) is False


def test_command_verifier_timeout_surfaces_as_error_not_mismatch():
    policy = VerifyPolicy()
    policy.register(
        "terminal", make_command_verifier("sleep 5", timeout=0.05), name="slow"
    )
    outcome = policy.consult("terminal", {"command": "x"})
    # A checker that can't finish is an *error*, not proof the mutation failed.
    assert outcome.status == "error"


def test_command_verifier_uses_cwd_resolver(tmp_path):
    (tmp_path / "marker").write_text("1")
    verify = make_command_verifier(
        ["test", "-f", "marker"],
        cwd_resolver=lambda call: str(tmp_path),
    )
    assert verify(VerifyCall("terminal", {})) is True


def test_split_command_helper():
    assert split_command("pytest -q tests/") == ["pytest", "-q", "tests/"]


# ── symbolic predicate verifier ──────────────────────────────────────────────


def test_predicate_verifier_wraps_arbitrary_check():
    verify = make_predicate_verifier(
        lambda call: call.args.get("mode") == "replace"
    )
    policy = VerifyPolicy()
    policy.register("patch", verify, name="mode-check")
    assert policy.consult("patch", {"mode": "replace"}).status == "ok"
    assert policy.consult("patch", {"mode": "patch"}).status == "mismatch"


def test_predicate_verifier_can_inspect_result_payload():
    verify = make_predicate_verifier(
        lambda call: isinstance(call.result, str) and "bytes_written" in call.result
    )
    policy = VerifyPolicy()
    policy.register("write_file", verify)
    assert policy.consult("write_file", {}, result='{"bytes_written": 5}').status == "ok"
    assert policy.consult("write_file", {}, result='{"error": "x"}').status == "mismatch"


# ── enable gate (opt-in, default OFF) ────────────────────────────────────────


def test_verify_policy_disabled_by_default(monkeypatch):
    monkeypatch.delenv("HERMES_VERIFY_POLICY", raising=False)
    # Force config resolution to fail/return nothing → default OFF.
    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda *a, **k: {}, raising=False
    )
    assert verify_policy_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_verify_policy_env_enables(monkeypatch, val):
    monkeypatch.setenv("HERMES_VERIFY_POLICY", val)
    assert verify_policy_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
def test_verify_policy_env_disables(monkeypatch, val):
    monkeypatch.setenv("HERMES_VERIFY_POLICY", val)
    assert verify_policy_enabled() is False


def test_verify_policy_config_enables_when_env_absent(monkeypatch):
    monkeypatch.delenv("HERMES_VERIFY_POLICY", raising=False)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda *a, **k: {"verify_policy": {"enabled": True}},
        raising=False,
    )
    assert verify_policy_enabled() is True
