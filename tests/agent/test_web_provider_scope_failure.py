"""Provider fallback requires positively established profile-scope state."""
from contextlib import contextmanager
import builtins
from pathlib import Path

import pytest

from agent import secret_scope as ss
from agent.web_search_provider import get_provider_env

KEY = "SYNTHETIC_PROVIDER_KEY"


@pytest.fixture(autouse=True)
def isolated_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv(KEY, "synthetic-launch-value")
    monkeypatch.setattr(ss, "_MULTIPLEX_ACTIVE", False)
    with scope(None, multiplex=False):
        yield tmp_path


@contextmanager
def scope(values, *, multiplex=True, profile="profile-b"):
    token = ss.set_secret_scope(
        values, profile_home=str(Path.home() / profile) if values is not None else None
    )
    mode = ss.set_multiplex_context(multiplex)
    try:
        yield
    finally:
        ss.reset_multiplex_context(mode)
        ss.reset_secret_scope(token)


@pytest.mark.parametrize("error", [ImportError, RuntimeError])
@pytest.mark.parametrize("blocked_symbol", [None, "current_secret_scope"])
def test_scope_loader_failure_refuses_ambient_fallback(monkeypatch, error, blocked_symbol):
    original = builtins.__import__

    def import_without_state(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "agent.secret_scope" and (blocked_symbol is None or blocked_symbol in fromlist):
            raise error("synthetic private failure payload")
        return original(name, globals, locals, fromlist, level)

    with scope({}):
        monkeypatch.setattr(builtins, "__import__", import_without_state)
        with pytest.raises(RuntimeError, match="profile scope is unavailable") as caught:
            get_provider_env(KEY)  # Real canonical config getter sees the empty scope.
    assert "private failure" not in str(caught.value)
    assert "synthetic-launch-value" not in str(caught.value)
    assert caught.value.__suppress_context__ is True


@pytest.mark.parametrize("function", ["current_secret_scope", "is_multiplex_active"])
def test_scope_query_failure_is_sanitized(monkeypatch, function):
    from hermes_cli import config

    monkeypatch.setattr(config, "get_env_value", lambda _name: None)

    def unavailable():
        raise ValueError("synthetic private failure payload")

    with scope({}):
        monkeypatch.setattr(ss, function, unavailable)
        with pytest.raises(RuntimeError, match="profile scope is unavailable") as caught:
            get_provider_env(KEY)
    assert "private failure" not in str(caught.value)


def test_real_profile_a_b_a_and_missing_key():
    with scope({KEY: "synthetic-a"}, profile="profile-a"):
        assert get_provider_env(KEY) == "synthetic-a"
        with scope({KEY: "synthetic-b"}, profile="profile-b"):
            assert get_provider_env(KEY) == "synthetic-b"
        with scope({}):
            assert get_provider_env(KEY) == ""
        assert get_provider_env(KEY) == "synthetic-a"


def test_unbound_multiplex_scope_preserves_denial():
    with scope(None):
        with pytest.raises(ss.UnscopedSecretError):
            get_provider_env(KEY)


@pytest.mark.parametrize("scoped", [False, True])
def test_config_error_fallback_requires_positive_unscoped_state(monkeypatch, scoped):
    from hermes_cli import config

    def unavailable(_name):
        raise RuntimeError("synthetic config failure")

    monkeypatch.setattr(config, "get_env_value", unavailable)
    with scope({} if scoped else None, multiplex=scoped):
        assert get_provider_env(KEY) == ("" if scoped else "synthetic-launch-value")


def test_normal_standalone_environment_positive():
    assert get_provider_env(KEY) == "synthetic-launch-value"
