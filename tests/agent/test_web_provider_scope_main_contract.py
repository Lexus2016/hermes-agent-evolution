"""Web credentials must not escape the canonical profile scope on failure."""
import builtins
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent import secret_scope as ss
from agent.web_search_provider import get_provider_env

KEY = "SYNTHETIC_PROVIDER_KEY"


@pytest.fixture(autouse=True)
def isolated_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "launch"))
    monkeypatch.setenv(KEY, "synthetic-launch-value")
    monkeypatch.setenv("TAVILY_API_KEY", "synthetic-launch-value")
    previous = ss.is_multiplex_active()
    token = ss.set_secret_scope(None)
    ss.set_multiplex_active(False)
    try:
        yield tmp_path
    finally:
        ss.set_multiplex_active(previous)
        ss.reset_secret_scope(token)


@pytest.fixture(params=["provider", "tool"])
def lookup(request):
    if request.param == "provider":
        return get_provider_env
    from tools.web_tools import _env_value
    return _env_value


def assert_provider_refuses_without_request(monkeypatch):
    from plugins.web.tavily import provider

    request = Mock(side_effect=AssertionError("scope failure must not make a request"))
    monkeypatch.setattr(provider.httpx, "post", request)
    result = provider.TavilyWebSearchProvider().search("synthetic query")
    assert result["success"] is False
    assert "synthetic-launch-value" not in str(result)
    request.assert_not_called()


@contextmanager
def scope(home, values):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home_token = set_hermes_home_override(str(home))
    token = ss.set_secret_scope(values, profile_home=str(home))
    try:
        yield
    finally:
        ss.reset_secret_scope(token)
        reset_hermes_home_override(home_token)


@pytest.mark.parametrize("failure", ["unbound", "config", "import", "import_runtime", "import_all", "import_runtime_all", "scope", "multiplex"])
def test_scope_failure_never_returns_launch_credentials(isolated_profile, monkeypatch, lookup, failure):
    from hermes_cli import config

    ss.set_multiplex_active(True)
    if failure == "unbound":
        with pytest.raises(ss.UnscopedSecretError):
            lookup(KEY)
        assert_provider_refuses_without_request(monkeypatch)
        return
    with scope(isolated_profile / "profile-b", {}):
        if failure == "config":
            monkeypatch.setattr(config, "get_env_value", lambda _name: None)
            assert lookup(KEY) == ""
            return
        if failure.startswith("import"):
            original = builtins.__import__

            def unavailable(name, globals=None, locals=None, fromlist=(), level=0):
                if name == "agent.secret_scope" and (failure.endswith("_all") or "current_secret_scope" in fromlist):
                    error = RuntimeError if "runtime" in failure else ImportError
                    raise error("synthetic private failure payload")
                return original(name, globals, locals, fromlist, level)

            monkeypatch.setattr(builtins, "__import__", unavailable)
        else:
            monkeypatch.setattr(config, "get_env_value", lambda _name: None)

            def unavailable():
                raise ValueError("synthetic private failure payload")

            name = "current_secret_scope" if failure == "scope" else "is_multiplex_active"
            monkeypatch.setattr(ss, name, unavailable)
        with pytest.raises(RuntimeError, match="profile scope is unavailable") as caught:
            lookup(KEY)
        assert "private failure" not in str(caught.value)
        assert "synthetic-launch-value" not in str(caught.value)
        assert caught.value.__suppress_context__ is True
        assert_provider_refuses_without_request(monkeypatch)


@pytest.mark.parametrize("config_failure", [False, True])
def test_standalone_and_profile_a_b_a_compatibility(isolated_profile, monkeypatch, lookup, config_failure):
    from hermes_cli import config

    if config_failure:
        def unavailable(_name):
            raise RuntimeError("synthetic config failure")
        monkeypatch.setattr(config, "get_env_value", unavailable)
    assert lookup(KEY) == "synthetic-launch-value"
    ss.set_multiplex_active(True)
    with scope(isolated_profile / "profile-a", {KEY: "synthetic-a"}):
        assert lookup(KEY) == ("" if config_failure else "synthetic-a")
        with scope(isolated_profile / "profile-b", {KEY: "synthetic-b"}):
            assert lookup(KEY) == ("" if config_failure else "synthetic-b")
        with scope(isolated_profile / "profile-b", {}):
            assert lookup(KEY) == ""
        assert lookup(KEY) == ("" if config_failure else "synthetic-a")
    with pytest.raises(ss.UnscopedSecretError):
        lookup(KEY)
