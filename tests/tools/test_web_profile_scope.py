"""Web readiness and selection use the same scoped credential boundary as providers."""
import builtins
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent import secret_scope as ss
from tools import web_tools

KEY = "SYNTHETIC_WEB_READINESS_KEY"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv(KEY, "synthetic-launch")
    monkeypatch.setattr(ss, "_MULTIPLEX_ACTIVE", False)


@contextmanager
def bound(values, profile="profile-b", multiplex=True):
    token = ss.set_secret_scope(values, profile_home=str(Path.home() / profile))
    mode = ss.set_multiplex_context(multiplex)
    try:
        yield
    finally:
        ss.reset_multiplex_context(mode)
        ss.reset_secret_scope(token)


def test_actual_readiness_a_b_a_and_missing_key():
    with bound({KEY: "synthetic-a"}, "profile-a"):
        assert web_tools._env_value(KEY) == "synthetic-a"
        with bound({}, "profile-b"):
            assert web_tools._env_value(KEY) == ""
            assert web_tools._has_env(KEY) is False
        assert web_tools._env_value(KEY) == "synthetic-a"


def test_unbound_multiplex_refuses_readiness():
    with bound(None):
        with pytest.raises(ss.UnscopedSecretError):
            web_tools._has_env(KEY)


def test_unknown_scope_refuses_readiness(monkeypatch):
    original = builtins.__import__

    def unavailable(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "agent.secret_scope" and "current_secret_scope" in fromlist:
            raise ImportError("synthetic internal detail")
        return original(name, globals, locals, fromlist, level)

    with bound({}):
        monkeypatch.setattr(builtins, "__import__", unavailable)
        with pytest.raises(RuntimeError, match="profile scope is unavailable"):
            web_tools._has_env(KEY)


def test_positive_standalone_readiness():
    with bound(None, multiplex=False):
        assert web_tools._env_value(KEY) == "synthetic-launch"
        assert web_tools._has_env(KEY) is True
