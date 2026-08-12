"""Tests for subagent model routing wiring (issue #2317).

The routing abstraction (tools/model_routing_table.py) was shipped as dead
code. This verifies the live call site in delegate_tool: when
``delegation.routing.enabled`` is true, a subagent is routed to a model via
the routing table; otherwise it inherits the parent's model. Fail-open: a
routing error must never break delegation.
"""

import pytest

from tools.delegate_tool import _route_subagent_model


class TestRouteSubagentModel:
    def test_disabled_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            "tools.delegate_tool._load_config",
            lambda: {"routing": {"enabled": False}},
        )
        assert _route_subagent_model("write a poem", None, 0) is None

    def test_enabled_no_models_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            "tools.delegate_tool._load_config",
            lambda: {"routing": {"enabled": True, "models": []}},
        )
        assert _route_subagent_model("write a poem", None, 0) is None

    def test_enabled_routes_to_model(self, monkeypatch):
        monkeypatch.setattr(
            "tools.delegate_tool._load_config",
            lambda: {"routing": {"enabled": True, "models": ["model-a", "model-b"]}},
        )
        routed = _route_subagent_model("write a poem", None, 0)
        assert routed in {"model-a", "model-b"}

    def test_fail_open_on_config_error(self, monkeypatch):
        def boom():
            raise RuntimeError("config load failed")

        monkeypatch.setattr("tools.delegate_tool._load_config", boom)
        # A routing failure must never break delegation — returns None.
        assert _route_subagent_model("write a poem", None, 0) is None

    def test_fail_open_on_missing_routing_key(self, monkeypatch):
        monkeypatch.setattr("tools.delegate_tool._load_config", lambda: {})
        assert _route_subagent_model("write a poem", None, 0) is None
