"""Tests for the SEP-2640 skills extension in mcp_serve (#2708).

Pins the extension id, the capability declaration, and the skills/list +
skills/get handlers against a temp skills tree and the real FastMCP server.
"""

import asyncio

import pytest

pytest.importorskip("mcp")
pytest.importorskip("yaml")

import mcp_serve  # noqa: E402
from mcp.types import ServerCapabilities  # noqa: E402


def _make_skill(root, name, description="test skill", frontmatter=None):
    d = root / name
    d.mkdir(parents=True)
    fm = frontmatter if frontmatter is not None else (
        f"name: {name}\ndescription: {description}\n"
    )
    (d / "SKILL.md").write_text(f"---\n{fm}---\n# body\n", encoding="utf-8")


def test_extension_constants_match_spec():
    assert mcp_serve.SKILLS_EXTENSION_ID == "io.modelcontextprotocol/skills"
    assert mcp_serve.MAX_SKILLS_PER_SERVER == 5  # SEP-2640 bound


def test_index_capped_at_five_and_skips_invalid(tmp_path):
    for i in range(7):  # more than the bound — listing must cap
        _make_skill(tmp_path, f"skill-{i}")
    _make_skill(tmp_path, "no-desc", frontmatter="name: no-desc\n---\n")
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "SKILL.md").write_text("no frontmatter", encoding="utf-8")

    entries = mcp_serve._iter_repo_skills(roots=[tmp_path])
    assert len(entries) == mcp_serve.MAX_SKILLS_PER_SERVER
    assert entries[0]["name"] == "skill-0"
    assert entries[0]["uri"] == "skill://skill-0/SKILL.md"


def test_skills_list_and_get_handlers(tmp_path, monkeypatch):
    _make_skill(tmp_path, "demo", description="demonstration skill")
    original = mcp_serve._iter_repo_skills
    monkeypatch.setattr(
        mcp_serve, "_iter_repo_skills",
        lambda roots=None: original(roots=[tmp_path]),
    )
    listed = asyncio.run(mcp_serve._skills_list({}))
    assert [s["name"] for s in listed["skills"]] == ["demo"]
    assert listed["skills"][0]["uri"] == "skill://demo/SKILL.md"
    assert listed["skills"][0]["mimeType"] == "text/markdown"

    got = asyncio.run(mcp_serve._skills_get({"uri": "skill://demo/SKILL.md"}))
    assert got["name"] == "demo" and got["description"] == "demonstration skill"

    with pytest.raises(ValueError):
        asyncio.run(mcp_serve._skills_get({"uri": "skill://missing/SKILL.md"}))
    with pytest.raises(ValueError):  # only <name>/SKILL.md is addressable
        asyncio.run(mcp_serve._skills_get({"uri": "skill://demo/scripts/x.py"}))


def test_registration_declares_extension_and_handlers(monkeypatch):
    monkeypatch.setattr(mcp_serve, "_iter_repo_skills", lambda roots=None: [])
    server = mcp_serve.create_mcp_server()
    inner = server._mcp_server
    # The handshake: get_capabilities must advertise the skills extension.
    from mcp.server import NotificationOptions

    caps = inner.get_capabilities(NotificationOptions(), {})
    assert "io.modelcontextprotocol/skills" in (caps.experimental or {})
    assert isinstance(caps, ServerCapabilities)
    # Registration path: the handlers are registered on the server.
    assert "skills/list" in inner.request_handlers
    assert "skills/get" in inner.request_handlers
