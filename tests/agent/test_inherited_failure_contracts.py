"""Failure-path evidence for retained runtime fallbacks."""

from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


def test_tool_builder_failure_keeps_tools_and_valid_names_together(monkeypatch):
    from agent.conversation_loop import _refresh_bot_chat_tools
    from gateway.session_policy import _ACTIVE_POLICY

    tools = [{"type": "function", "function": {"name": "read_file"}}]
    names = {"read_file"}
    agent = SimpleNamespace(platform="desktop", session_id="synthetic", tools=tools,
                            valid_tool_names=names, enabled_toolsets=["file"], disabled_toolsets=[])

    def fail(**kwargs):
        raise RuntimeError("builder unavailable")

    monkeypatch.setattr("model_tools.get_tool_definitions", fail)
    token = _ACTIVE_POLICY.set(SimpleNamespace(toolsets=("file",)))
    try:
        _refresh_bot_chat_tools(agent)
    finally:
        _ACTIVE_POLICY.reset(token)
    assert agent.tools is tools
    assert agent.valid_tool_names is names
    assert agent.valid_tool_names == {"read_file"}
    assert [tool["function"]["name"] for tool in agent.tools] == ["read_file"]


@pytest.mark.parametrize("key", [None, "synthetic-explicit-key"])
def test_named_provider_lookup_failure_preserves_explicit_route_and_key(monkeypatch, key):
    from agent import auxiliary_client as ac

    def fail(name):
        raise RuntimeError("config unavailable")

    monkeypatch.setattr("hermes_cli.providers.get_provider", lambda name: None)
    monkeypatch.setattr("hermes_cli.runtime_provider._get_named_custom_provider", fail)
    result = ac._resolve_task_provider_model(provider="synthetic-named", model="model",
                                            base_url="https://example.invalid/v1", api_key=key)
    assert result == ("custom", "model", "https://example.invalid/v1", key, None)


def test_failed_snapshot_entry_does_not_drop_healthy_sibling(tmp_path, monkeypatch):
    from agent import prompt_builder as pb

    bad = tmp_path / "bad" / "SKILL.md"
    healthy = tmp_path / "healthy" / "SKILL.md"
    for path in (bad, healthy):
        path.parent.mkdir()
        path.write_text("---\nname: example\ndescription: healthy\n---\nContent", encoding="utf-8")
    build = pb._build_snapshot_entry

    def malformed(path, *args):
        if path == bad:
            raise TypeError("malformed index metadata")
        return build(path, *args)

    monkeypatch.setattr(pb, "_build_snapshot_entry", malformed)
    rows = pb._scan_extra_root(tmp_path, [bad, healthy], 1, "Invalid skill %s: %s")
    assert len(rows) == 1
    assert rows[0][0]["rel"] == "healthy/SKILL.md"
    assert rows[0][0]["root"] == tmp_path
    assert rows[0][0]["tier"] == 1


def test_profile_lookup_failure_keeps_api_session_namespaces(monkeypatch):
    from gateway.platforms.api_server import _derive_chat_session_id

    def fail(*args, **kwargs):
        raise RuntimeError("profile lookup unavailable")

    monkeypatch.setattr("hermes_cli.profiles.profile_matches_home", fail)
    ordinary = _derive_chat_session_id("system", "first")
    profile_a = _derive_chat_session_id("system", "first", profile="alpha")
    profile_b = _derive_chat_session_id("system", "first", profile="beta")
    assert len({ordinary, profile_a, profile_b}) == 3
    assert _derive_chat_session_id("system", "first", profile="alpha") == profile_a
