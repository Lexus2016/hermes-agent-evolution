"""Content provenance tagging — trust-level metadata on external content.

#1799 — Slice 2 of Task Shield (#1659).

Every piece of external content (search results, web pages, MCP outputs)
carries a trust-level metadata tag. Three levels: ``user`` > ``tool-invoked``
> ``external``. External content is wrapped in ``<untrusted-content>``
delimiters when rendered so the LLM and safety filters can distinguish
trust boundaries.

This module is a standalone utility — content entry points (web_search,
web_extract, mcp_tool) can import and use it. Integration with the Task
Shield pre-execution validator (#1798) happens when Slice 1 merges.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ── Trust levels ────────────────────────────────────────────────────────

# Ordered from highest to lowest trust.
USER = "user"  # user's own messages
TOOL_INVOKED = "tool-invoked"  # results from tools the user explicitly invoked
EXTERNAL = "external"  # search results, web pages, email bodies, MCP responses

_TRUST_ORDER = [USER, TOOL_INVOKED, EXTERNAL]


@dataclass(frozen=True)
class ProvenanceTag:
    """Immutable provenance metadata for a piece of content."""

    trust_level: str
    source: str  # e.g. "web_search", "github", "email", "mcp_server"
    url: str = ""
    fetched_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def trust_rank(self) -> int:
        """Lower rank = higher trust. USER=0, TOOL_INVOKED=1, EXTERNAL=2."""
        try:
            return _TRUST_ORDER.index(self.trust_level)
        except ValueError:
            return len(_TRUST_ORDER)  # unknown = lowest trust


def resolve_trust_level(source: str) -> str:
    """Map a source string to a trust level.

    >>> resolve_trust_level("user")
    'user'
    >>> resolve_trust_level("terminal")
    'tool-invoked'
    >>> resolve_trust_level("web_search")
    'external'
    """
    if not source:
        return EXTERNAL

    source_lower = source.lower()

    # User-originated content is the highest trust.
    if source_lower in ("user", "human", "me"):
        return USER

    # Core agent tools the user explicitly invoked are medium trust.
    _tool_invoked = frozenset({
        "terminal",
        "execute_code",
        "read_file",
        "search_files",
        "write_file",
        "patch",
        "delegate_task",
        "skill_view",
        "session_search",
    })
    if source_lower in _tool_invoked:
        return TOOL_INVOKED

    # Everything else (web, email, MCP, social) is external/untrusted.
    return EXTERNAL


@dataclass
class TaggedContent:
    """Content wrapped with provenance metadata."""

    content: str
    tag: ProvenanceTag

    def render(self) -> str:
        """Render content for LLM context.

        External content is wrapped in ``<untrusted-content>`` delimiters so
        the LLM and downstream safety filters can distinguish trust boundaries.
        User and tool-invoked content is rendered as-is (no wrapping).
        """
        if self.tag.trust_level == EXTERNAL:
            delimiter_tag = f' source="{self.tag.source}"'
            if self.tag.url:
                delimiter_tag += f' url="{self.tag.url}"'
            return f"<untrusted-content{delimiter_tag}>\n{self.content}\n</untrusted-content>"
        return self.content


def tag_content(
    content: str,
    source: str,
    *,
    trust_level: str | None = None,
    url: str = "",
) -> TaggedContent:
    """Tag raw content with provenance metadata.

    Args:
        content: The raw text content.
        source: Where the content came from (e.g. "web_search", "terminal").
        trust_level: Override the auto-resolved trust level.
        url: Optional URL the content was fetched from.

    Returns:
        A TaggedContent instance.
    """
    resolved = trust_level if trust_level is not None else resolve_trust_level(source)
    tag = ProvenanceTag(trust_level=resolved, source=source, url=url)
    return TaggedContent(content=content, tag=tag)


def wrap_external(content: str, source: str = "external", url: str = "") -> str:
    """One-shot convenience: tag and render external content.

    Returns the rendered string with ``<untrusted-content>`` delimiters.
    """
    return tag_content(content, source, url=url).render()


@dataclass
class TaggingRegistry:
    """Turn-level accumulator for auditing content entry points.

    Tracks what content entered the context this turn, from where, and at
    what trust level. Used for logging and safety audits.
    """

    _entries: list[ProvenanceTag] = field(default_factory=list)

    def add(self, tag: ProvenanceTag) -> None:
        self._entries.append(tag)

    @property
    def external_sources(self) -> list[str]:
        """Source names of all external (untrusted) content this turn."""
        return [t.source for t in self._entries if t.trust_level == EXTERNAL]

    @property
    def has_external(self) -> bool:
        """Whether any external (untrusted) content was added this turn."""
        return any(t.trust_level == EXTERNAL for t in self._entries)

    @property
    def min_trust_level(self) -> str:
        """The lowest trust level among all entries (for gating decisions).

        Returns the trust_level string of the entry with the HIGHEST rank
        number (= lowest trust = most dangerous).
        """
        if not self._entries:
            return USER
        return max(self._entries, key=lambda t: t.trust_rank).trust_level

    def clear(self) -> None:
        self._entries.clear()
