"""Tests for the A2A Agent Card model and the /.well-known/agent.json endpoint.

Split in two: the model/serialization tests import only the light
``hermes_cli.a2a`` module; the endpoint tests spin up the FastAPI app via
TestClient (importing ``web_server`` registers the real tool set).
"""

from __future__ import annotations

import pytest

from hermes_cli import a2a


# --------------------------------------------------------------------------
# Agent Card model / serialization
# --------------------------------------------------------------------------


def test_agent_skill_to_dict():
    skill = a2a.AgentSkill(
        id="read_file", name="read_file", description="Read a file", tags=["files"]
    )
    assert skill.to_dict() == {
        "id": "read_file",
        "name": "read_file",
        "description": "Read a file",
        "tags": ["files"],
    }


def test_agent_card_to_dict_has_a2a_shape():
    card = a2a.AgentCard(
        name="Hermes",
        description="desc",
        url="https://host/a2a",
        version="1.2.3",
        skills=[a2a.AgentSkill(id="t", name="t", description="d", tags=["ts"])],
        capabilities={"streaming": False},
        authentication={"schemes": ["bearer"]},
        provider={"organization": "NousResearch", "url": "https://example.com"},
    )
    data = card.to_dict()

    # camelCase A2A keys present.
    assert set(data) >= {
        "name",
        "description",
        "url",
        "version",
        "capabilities",
        "authentication",
        "defaultInputModes",
        "defaultOutputModes",
        "skills",
        "provider",
    }
    assert data["url"] == "https://host/a2a"
    assert data["version"] == "1.2.3"
    assert data["authentication"] == {"schemes": ["bearer"]}
    assert data["defaultInputModes"] == ["text"]
    assert data["skills"] == [
        {"id": "t", "name": "t", "description": "d", "tags": ["ts"]}
    ]
    assert data["provider"]["organization"] == "NousResearch"


def test_agent_card_omits_provider_when_absent():
    card = a2a.AgentCard(name="H", description="", url="/a2a", version="0")
    assert "provider" not in card.to_dict()


def test_build_agent_card_resolves_relative_url():
    cfg = {"url": "/a2a", "name": "Hermes"}
    card = a2a.build_agent_card(cfg, capabilities=[], base_url="https://host:8080/")
    assert card.url == "https://host:8080/a2a"


def test_build_agent_card_leaves_absolute_url_untouched():
    cfg = {"url": "https://pinned.example/a2a"}
    card = a2a.build_agent_card(cfg, capabilities=[], base_url="https://host")
    assert card.url == "https://pinned.example/a2a"


def test_build_agent_card_injected_capabilities_and_version():
    caps = [a2a.AgentSkill(id="x", name="x")]
    card = a2a.build_agent_card({"name": "Hermes"}, capabilities=caps, version="9.9")
    assert card.version == "9.9"
    assert [s.id for s in card.skills] == ["x"]


def test_load_config_overlays_defaults(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("name: Custom\nenabled: false\n", encoding="utf-8")
    cfg = a2a.load_config(cfg_file)
    assert cfg["name"] == "Custom"  # overridden
    assert cfg["enabled"] is False  # overridden
    assert cfg["url"] == "/a2a"  # default preserved
    assert cfg["capabilities"]["streaming"] is False  # default preserved


def test_collect_capabilities_skill_toggle_and_cap():
    # Tools off, skills on: every entry is a SKILL.md skill, count is capped.
    caps = a2a.collect_capabilities({
        "expose": {"tools": False, "skills": True, "max_skills": 3}
    })
    assert len(caps) <= 3
    assert all(s.id.startswith("skill:") for s in caps)


def test_collect_capabilities_all_off_is_empty():
    assert a2a.collect_capabilities({"expose": {"tools": False, "skills": False}}) == []


def test_collect_capabilities_honours_exclude():
    caps = a2a.collect_capabilities({
        "expose": {"tools": False, "skills": True, "max_skills": 50}
    })
    assert caps, "expected at least one SKILL.md skill to exclude"
    victim = caps[0].id  # e.g. "skill:a2a"
    filtered = a2a.collect_capabilities({
        "expose": {
            "tools": False,
            "skills": True,
            "max_skills": 50,
            "exclude": [victim],
        }
    })
    assert victim not in {s.id for s in filtered}


# --------------------------------------------------------------------------
# /.well-known/agent.json endpoint
# --------------------------------------------------------------------------

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient  # noqa: E402

from hermes_cli import web_server  # noqa: E402


@pytest.fixture
def client():
    a2a.reset_discovery_cache()
    previous_auth_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.auth_required = False
    test_client = TestClient(web_server.app)
    test_client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    try:
        yield test_client
    finally:
        a2a.reset_discovery_cache()
        if previous_auth_required is None:
            try:
                delattr(web_server.app.state, "auth_required")
            except AttributeError:
                pass
        else:
            web_server.app.state.auth_required = previous_auth_required


def test_well_known_agent_json_endpoint(client):
    response = client.get("/.well-known/agent.json")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()

    # A2A card shape.
    assert set(body) >= {
        "name",
        "url",
        "version",
        "capabilities",
        "authentication",
        "skills",
    }
    assert body["name"]
    assert body["url"].endswith("/a2a")
    assert body["url"].startswith("http")  # relative default resolved to request host
    assert body["authentication"] == {"schemes": ["bearer"]}

    # Importing web_server registers the real tool set, so the card advertises
    # Hermes' own tools (id without the "skill:" prefix). No new core tool is
    # added -- these are existing tools surfaced as A2A capabilities.
    ids = [s["id"] for s in body["skills"]]
    assert ids, "agent card should advertise at least one capability"
    assert any(not i.startswith("skill:") for i in ids), (
        "expected registered tools in the card"
    )


def test_well_known_agent_json_disabled_returns_404(client, monkeypatch):
    monkeypatch.setattr(a2a, "load_config", lambda *a, **k: {"enabled": False})
    a2a.reset_discovery_cache()  # force rebuild so the patched config is used
    response = client.get("/.well-known/agent.json")
    assert response.status_code == 404


def test_well_known_agent_json_public_under_auth_gate():
    """The card MUST be reachable unauthenticated even when the OAuth gate is
    engaged -- A2A clients hold no dashboard cookie. Validates the
    _GATE_PUBLIC_PREFIXES allowlist entry added for this endpoint."""
    a2a.reset_discovery_cache()
    prev_required = getattr(web_server.app.state, "auth_required", None)
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    web_server.app.state.bound_host = "fly-app.fly.dev"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    try:
        gated = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
        # No session cookie, no session header -> still 200 (public allowlist).
        response = gated.get("/.well-known/agent.json")
        assert response.status_code == 200
        assert response.json()["url"].endswith("/a2a")
    finally:
        a2a.reset_discovery_cache()
        web_server.app.state.auth_required = prev_required
        web_server.app.state.bound_host = prev_host
        web_server.app.state.bound_port = prev_port
