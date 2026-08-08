"""Tests for tools/content_provenance.py — content provenance tagging (#1799)."""

from tools.content_provenance import (
    EXTERNAL,
    TOOL_INVOKED,
    USER,
    ProvenanceTag,
    TaggingRegistry,
    TaggedContent,
    resolve_trust_level,
    tag_content,
    wrap_external,
)


# ── resolve_trust_level ─────────────────────────────────────────────────


class TestResolveTrustLevel:
    def test_user(self):
        assert resolve_trust_level("user") == USER

    def test_human_alias(self):
        assert resolve_trust_level("human") == USER

    def test_tool_invoked(self):
        assert resolve_trust_level("terminal") == TOOL_INVOKED
        assert resolve_trust_level("read_file") == TOOL_INVOKED
        assert resolve_trust_level("execute_code") == TOOL_INVOKED

    def test_external(self):
        assert resolve_trust_level("web_search") == EXTERNAL
        assert resolve_trust_level("web_extract") == EXTERNAL
        assert resolve_trust_level("github") == EXTERNAL
        assert resolve_trust_level("email") == EXTERNAL
        assert resolve_trust_level("mcp_server") == EXTERNAL

    def test_empty_source_defaults_external(self):
        assert resolve_trust_level("") == EXTERNAL
        assert resolve_trust_level(None) == EXTERNAL


# ── ProvenanceTag ───────────────────────────────────────────────────────


class TestProvenanceTag:
    def test_trust_rank_ordering(self):
        assert ProvenanceTag(USER, "user").trust_rank == 0
        assert ProvenanceTag(TOOL_INVOKED, "terminal").trust_rank == 1
        assert ProvenanceTag(EXTERNAL, "web_search").trust_rank == 2

    def test_unknown_trust_level(self):
        tag = ProvenanceTag("bogus", "x")
        assert tag.trust_rank == 3  # lowest trust

    def test_frozen(self):
        tag = ProvenanceTag(USER, "user")
        import pytest

        with pytest.raises(AttributeError):
            tag.trust_level = EXTERNAL


# ── tag_content + TaggedContent.render ──────────────────────────────────


class TestTagContent:
    def test_external_wraps_with_delimiters(self):
        tc = tag_content("evil payload", "web_search", url="https://evil.com")
        rendered = tc.render()
        assert "<untrusted-content" in rendered
        assert "evil payload" in rendered
        assert "</untrusted-content>" in rendered

    def test_external_includes_source_and_url(self):
        tc = tag_content("data", "github", url="https://github.com/x")
        rendered = tc.render()
        assert 'source="github"' in rendered
        assert 'url="https://github.com/x"' in rendered

    def test_user_not_wrapped(self):
        tc = tag_content("my message", "user")
        assert tc.render() == "my message"

    def test_tool_invoked_not_wrapped(self):
        tc = tag_content("ls output", "terminal")
        assert tc.render() == "ls output"

    def test_trust_level_override(self):
        tc = tag_content("x", "web_search", trust_level=USER)
        assert tc.tag.trust_level == USER
        assert tc.render() == "x"  # not wrapped as external

    def test_external_no_url(self):
        tc = tag_content("content", "mcp_server")
        rendered = tc.render()
        assert "<untrusted-content" in rendered
        assert 'url="' not in rendered


# ── wrap_external ───────────────────────────────────────────────────────


class TestWrapExternal:
    def test_convenience_function(self):
        result = wrap_external("untrusted data", "web_search")
        assert "<untrusted-content" in result
        assert "untrusted data" in result


# ── TaggingRegistry ─────────────────────────────────────────────────────


class TestTaggingRegistry:
    def test_add_and_external_sources(self):
        reg = TaggingRegistry()
        reg.add(ProvenanceTag(EXTERNAL, "web_search"))
        reg.add(ProvenanceTag(EXTERNAL, "github"))
        reg.add(ProvenanceTag(USER, "user"))
        assert set(reg.external_sources) == {"web_search", "github"}

    def test_has_external(self):
        reg = TaggingRegistry()
        assert not reg.has_external
        reg.add(ProvenanceTag(USER, "user"))
        assert not reg.has_external
        reg.add(ProvenanceTag(EXTERNAL, "web_search"))
        assert reg.has_external

    def test_min_trust_level(self):
        reg = TaggingRegistry()
        reg.add(ProvenanceTag(USER, "user"))
        reg.add(ProvenanceTag(TOOL_INVOKED, "terminal"))
        reg.add(ProvenanceTag(EXTERNAL, "web_search"))
        # min_trust_level returns the LOWEST trust (highest rank number)
        assert reg.min_trust_level == EXTERNAL

    def test_min_trust_level_empty(self):
        reg = TaggingRegistry()
        assert reg.min_trust_level == USER

    def test_clear(self):
        reg = TaggingRegistry()
        reg.add(ProvenanceTag(EXTERNAL, "web_search"))
        reg.clear()
        assert not reg.has_external
        assert reg.external_sources == []
