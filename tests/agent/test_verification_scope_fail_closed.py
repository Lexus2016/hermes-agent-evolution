"""An active verification scope must never fail open on file reads or writes."""

import builtins
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent import file_safety
from tools import file_tools
from agent.verification_scope import VerificationScope, VerificationScopeEnforcer
from tools.file_operations import ShellFileOperations
from tools.environments.local import LocalEnvironment


@pytest.fixture
def target(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(file_safety, "_ACTIVE_VERIFICATION_SCOPE", None)
    path = tmp_path / "report.txt"
    path.write_text("original")
    return path


def test_active_enforcer_exception_denies_real_file_write(target, monkeypatch, caplog):
    monkeypatch.setattr(file_safety, "_ACTIVE_VERIFICATION_SCOPE", VerificationScope())
    monkeypatch.setattr(
        VerificationScopeEnforcer,
        "check_file_access",
        Mock(side_effect=RuntimeError("secret-token /private/fixture")),
    )
    result = ShellFileOperations(LocalEnvironment("/")).write_file(
        str(target), "changed"
    )
    assert result.error
    assert "verification scope" in result.error.lower()
    assert "could not be evaluated" in result.error
    assert str(target) not in result.error and "secret-token" not in result.error
    assert "/private/fixture" not in result.error
    assert target.read_text() == "original"
    assert "secret-token" not in caplog.text and "/private/fixture" not in caplog.text
    assert "verification scope evaluation failed" in caplog.text.lower()


def test_active_scope_missing_enforcer_is_distinct_denial(target, monkeypatch):
    monkeypatch.setattr(file_safety, "_ACTIVE_VERIFICATION_SCOPE", VerificationScope())
    monkeypatch.setitem(sys.modules, "agent.verification_scope", None)
    result = ShellFileOperations(LocalEnvironment("/")).write_file(
        str(target), "changed"
    )
    assert result.error and "unavailable" in result.error
    assert target.read_text() == "original"


def test_unconfigured_scope_does_not_require_optional_enforcer(target, monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.verification_scope", None)
    result = ShellFileOperations(LocalEnvironment("/")).write_file(
        str(target), "changed"
    )
    assert result.error is None
    assert target.read_text() == "changed"


@pytest.mark.parametrize("failure", ["exception", "unavailable", "violation"])
def test_active_scope_denies_real_file_read(target, monkeypatch, failure):
    monkeypatch.setattr(
        file_safety,
        "_ACTIVE_VERIFICATION_SCOPE",
        VerificationScope(denied_paths=[str(target)]),
    )
    if failure == "exception":
        monkeypatch.setattr(
            VerificationScopeEnforcer,
            "check_file_access",
            Mock(side_effect=RuntimeError("secret-token /private/fixture")),
        )
    elif failure == "unavailable":
        monkeypatch.setitem(sys.modules, "agent.verification_scope", None)
    ops = ShellFileOperations(LocalEnvironment("/"))
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id: ops)
    result = json.loads(file_tools.read_file_tool(str(target)))
    assert result["error"]
    assert "original" not in str(result)
    if failure != "violation":
        assert str(target) not in result["error"]
        assert "secret-token" not in result["error"]
        assert "/private/fixture" not in result["error"]
        assert (
            "unavailable" if failure == "unavailable" else "could not be evaluated"
        ) in result["error"]


def test_provider_read_guard_does_not_swallow_active_internal_error(
    target, monkeypatch
):
    monkeypatch.setattr(file_safety, "_ACTIVE_VERIFICATION_SCOPE", VerificationScope())
    monkeypatch.setattr(
        file_safety,
        "get_read_block_error",
        Mock(side_effect=RuntimeError("secret-token /private/fixture")),
    )
    with pytest.raises(ValueError, match="could not be evaluated") as exc:
        file_safety.raise_if_read_blocked(str(target))
    assert str(target) not in str(exc.value) and "secret-token" not in str(exc.value)


def test_unconfigured_provider_read_retains_best_effort(target, monkeypatch):
    monkeypatch.setattr(
        file_safety, "get_read_block_error", Mock(side_effect=RuntimeError())
    )
    file_safety.raise_if_read_blocked(str(target))


def test_unconfigured_read_does_not_require_optional_enforcer(target, monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.verification_scope", None)
    monkeypatch.setattr(
        file_tools,
        "_get_file_ops",
        lambda task_id: ShellFileOperations(LocalEnvironment("/")),
    )
    result = json.loads(file_tools.read_file_tool(str(target)))
    assert not result.get("error")
    assert "original" in str(result)


def test_valid_active_scope_allows_real_read_and_write(target, monkeypatch):
    monkeypatch.setattr(file_safety, "_ACTIVE_VERIFICATION_SCOPE",
                        VerificationScope(allowed_paths=[str(target)]))
    ops = ShellFileOperations(LocalEnvironment("/"))
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id: ops)
    result = ops.write_file(str(target), "allowed-content")
    assert result.error is None
    assert target.read_text() == "allowed-content"
    read = json.loads(file_tools.read_file_tool(str(target)))
    assert not read.get("error")
    assert "allowed-content" in str(read)
    file_safety.raise_if_read_blocked(str(target))


# Main security backport matrix also covers import-time errors and exception chaining.
@pytest.fixture
def main_target(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "state"))
    previous = file_safety.get_active_verification_scope()
    file_safety.set_active_verification_scope(None)
    path = tmp_path / "report.txt"
    path.write_text("original", encoding="utf-8")
    try:
        yield path
    finally:
        file_safety.set_active_verification_scope(previous)


@pytest.mark.parametrize("state", ["exception", "unavailable", "import_runtime", "violation", "allowed", "unset"])
def test_real_file_read_write_require_evaluable_active_scope(main_target, monkeypatch, state):
    if state != "unset":
        scope = VerificationScope(denied_paths=[str(main_target)]) if state == "violation" else VerificationScope(allowed_paths=[str(main_target)])
        file_safety.set_active_verification_scope(scope)
    if state == "exception":
        monkeypatch.setattr(VerificationScopeEnforcer, "check_file_access", Mock(side_effect=RuntimeError("synthetic private failure")))
    elif state in {"unavailable", "unset"}:
        monkeypatch.setitem(sys.modules, "agent.verification_scope", None)
    elif state == "import_runtime":
        original = builtins.__import__

        def unavailable(name, *args, **kwargs):
            if name == "agent.verification_scope":
                raise RuntimeError("synthetic private failure")
            return original(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", unavailable)
    ops = ShellFileOperations(LocalEnvironment(str(main_target.parent)))
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id: ops)
    write = ops.write_file(str(main_target), "changed")
    read = json.loads(file_tools.read_file_tool(str(main_target)))
    if state in {"allowed", "unset"}:
        assert write.error is None and not read.get("error")
        assert main_target.read_text(encoding="utf-8") == "changed"
        assert "changed" in str(read)
        file_safety.raise_if_read_blocked(str(main_target))
    else:
        assert write.error and read.get("error")
        assert main_target.read_text(encoding="utf-8") == "original"
        assert "original" not in str(read)
        with pytest.raises(ValueError, match="verification scope"):
            file_safety.raise_if_read_blocked(str(main_target))
        if state != "violation":
            for error in (write.error, read["error"]):
                assert str(main_target) not in error and "private failure" not in error
                assert ("unavailable" if state == "unavailable" else "could not be evaluated") in error


@pytest.mark.parametrize("active", [False, True])
def test_provider_read_guard_cannot_swallow_active_failure(main_target, monkeypatch, active):
    if active:
        file_safety.set_active_verification_scope(VerificationScope())
    monkeypatch.setattr(file_safety, "get_read_block_error", Mock(side_effect=RuntimeError("synthetic private failure")))
    if active:
        with pytest.raises(ValueError, match="could not be evaluated") as caught:
            file_safety.raise_if_read_blocked(str(main_target))
        assert "private failure" not in str(caught.value)
        assert caught.value.__suppress_context__ is True
    else:
        file_safety.raise_if_read_blocked(str(main_target))
