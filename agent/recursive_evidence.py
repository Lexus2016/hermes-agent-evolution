"""Core evidence-tree model and replay API for recursive evidence reasoning.

This module is a standalone, import-safe data model. It records *claims* and
the *supporting passages* that back them as nodes in a tree, where each node
links to the parent claim it refines and to the child claims derived from it.
``EvidenceReplayBuffer`` builds those nodes directly from real tool-result
turns (the OpenAI-style ``{"role": "tool", "name": ..., "content": ...}`` shape
produced by :func:`agent.tool_dispatch_helpers.make_tool_result_message`), so
the buffer never invents its own turn shape.

``replay_evidence_path`` answers the reasoning question "why do we believe this
claim?" by returning the minimal chain from a matching claim back to the root
evidence it descends from.

Nothing here touches the live conversation loop — wiring is a follow-up. The
model is pure and unit-testable:

    buffer = EvidenceReplayBuffer()
    root = buffer.add_claim("investigation start")
    node = buffer.ingest_tool_result(turn, "grep found the bug", parent_id=root.node_id)
    chain = buffer.replay_evidence_path("the bug")  # [node, root]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from agent.message_content import flatten_message_text


def _clamp01(value: float) -> float:
    """Clamp a confidence score into the inclusive ``[0.0, 1.0]`` range."""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if score < 0.0:
        return 0.0
    if score > 1.0:
        return 1.0
    return score


@dataclass
class EvidenceNode:
    """A single claim in the evidence tree.

    Attributes
    ----------
    node_id:
        Stable identifier, unique within a single buffer.
    claim:
        The assertion this node represents.
    supporting_passages:
        Text passages (typically extracted from a tool result) that back the
        claim. May be empty for a purely structural / intermediate claim.
    parent_id:
        ``node_id`` of the claim this one refines, or ``None`` for a root.
    children_ids:
        ``node_id`` of each claim derived from this one. Maintained by the
        owning :class:`EvidenceReplayBuffer`; do not mutate directly.
    confidence:
        How strongly the passages support the claim, clamped to ``[0.0, 1.0]``.
    source:
        Optional provenance label, e.g. the name of the tool whose result
        produced the node.
    """

    node_id: str
    claim: str
    supporting_passages: list[str] = field(default_factory=list)
    parent_id: Optional[str] = None
    children_ids: list[str] = field(default_factory=list)
    confidence: float = 1.0
    source: Optional[str] = None

    @property
    def is_root(self) -> bool:
        """True when this node has no parent claim."""
        return self.parent_id is None


def _passages_from_turn(turn: dict[str, Any]) -> list[str]:
    """Extract supporting passages from a tool-result turn's content.

    Mirrors how the rest of the codebase reads message content: string content
    becomes a single passage; list content (multimodal parts) yields one
    passage per non-empty text part, skipping image/audio parts. Uses
    :func:`agent.message_content.flatten_message_text` so the extraction stays
    consistent with the provider-facing shape.
    """
    content = turn.get("content")
    if isinstance(content, list):
        passages: list[str] = []
        for part in content:
            # Wrap each part in a one-element list so flatten_message_text
            # routes it through its list branch, which drops non-text parts
            # (images/audio) instead of stringifying them.
            text = flatten_message_text([part]).strip()
            if text:
                passages.append(text)
        return passages
    text = flatten_message_text(content).strip()
    return [text] if text else []


class EvidenceReplayBuffer:
    """A lightweight, in-memory evidence tree.

    Nodes are keyed by ``node_id`` and linked into one or more trees (a forest
    is allowed — each node whose ``parent_id`` is ``None`` roots its own tree).
    Insertion order is preserved so replay is deterministic.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, EvidenceNode] = {}
        self._order: list[str] = []
        self._counter: int = 0

    # ── inserts ──────────────────────────────────────────────────────────

    def _next_id(self) -> str:
        self._counter += 1
        return f"node-{self._counter}"

    def add_claim(
        self,
        claim: str,
        *,
        parent_id: Optional[str] = None,
        node_id: Optional[str] = None,
        supporting_passages: Optional[list[str]] = None,
        confidence: float = 1.0,
        source: Optional[str] = None,
    ) -> EvidenceNode:
        """Insert a claim node and link it under ``parent_id`` (if given).

        Parameters
        ----------
        claim:
            The assertion to record.
        parent_id:
            Existing node to attach under; ``None`` makes this a root.
        node_id:
            Explicit id; auto-generated when omitted.
        supporting_passages:
            Backing passages. Copied so the caller's list is not aliased.
        confidence:
            Clamped into ``[0.0, 1.0]``.
        source:
            Optional provenance label.

        Raises
        ------
        ValueError
            If ``node_id`` already exists, or ``parent_id`` is unknown.
        """
        if node_id is None:
            node_id = self._next_id()
        elif node_id in self._nodes:
            raise ValueError(f"duplicate node_id: {node_id!r}")

        if parent_id is not None and parent_id not in self._nodes:
            raise ValueError(f"unknown parent_id: {parent_id!r}")

        node = EvidenceNode(
            node_id=node_id,
            claim=claim,
            supporting_passages=list(supporting_passages or []),
            parent_id=parent_id,
            confidence=_clamp01(confidence),
            source=source,
        )
        self._nodes[node_id] = node
        self._order.append(node_id)
        if parent_id is not None:
            self._nodes[parent_id].children_ids.append(node_id)
        return node

    def ingest_tool_result(
        self,
        turn: dict[str, Any],
        claim: str,
        *,
        parent_id: Optional[str] = None,
        node_id: Optional[str] = None,
        confidence: float = 1.0,
    ) -> EvidenceNode:
        """Build a node from a tool-result turn, backing ``claim``.

        ``turn`` must be a tool-result message (``role == "tool"``), the shape
        emitted by :func:`agent.tool_dispatch_helpers.make_tool_result_message`.
        Its content becomes the node's supporting passages and its tool name
        becomes the node's ``source``.

        Raises
        ------
        ValueError
            If ``turn`` is not a tool-result turn.
        """
        if not isinstance(turn, dict) or turn.get("role") != "tool":
            raise ValueError(
                "ingest_tool_result expects a tool-result turn (role='tool')"
            )

        source = turn.get("name") or turn.get("tool_name")
        return self.add_claim(
            claim,
            parent_id=parent_id,
            node_id=node_id,
            supporting_passages=_passages_from_turn(turn),
            confidence=confidence,
            source=str(source) if source is not None else None,
        )

    # ── accessors ────────────────────────────────────────────────────────

    def get(self, node_id: str) -> Optional[EvidenceNode]:
        """Return the node for ``node_id`` or ``None`` if absent."""
        return self._nodes.get(node_id)

    def roots(self) -> list[EvidenceNode]:
        """Return every root node, in insertion order."""
        return [self._nodes[nid] for nid in self._order if self._nodes[nid].is_root]

    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, node_id: object) -> bool:
        return node_id in self._nodes

    # ── replay ───────────────────────────────────────────────────────────

    def _path_to_root(self, node: EvidenceNode) -> list[EvidenceNode]:
        """Walk parent links from ``node`` up to its root.

        Returns ``[node, parent, ..., root]``. Guards against cycles so a
        corrupted ``parent_id`` chain cannot loop forever.
        """
        path: list[EvidenceNode] = []
        seen: set[str] = set()
        current: Optional[EvidenceNode] = node
        while current is not None and current.node_id not in seen:
            path.append(current)
            seen.add(current.node_id)
            if current.parent_id is None:
                break
            current = self._nodes.get(current.parent_id)
        return path

    def _matching_nodes(self, query: str) -> list[EvidenceNode]:
        """Return claim nodes matching ``query``, in insertion order.

        A node matches when the normalized query is contained in the normalized
        claim (case-insensitive; substring covers exact equality). Empty queries
        match nothing.
        """
        needle = (query or "").strip().casefold()
        if not needle:
            return []
        matches: list[EvidenceNode] = []
        for nid in self._order:
            node = self._nodes[nid]
            claim = node.claim.strip().casefold()
            if needle in claim:
                matches.append(node)
        return matches

    def replay_evidence_path(self, query: str) -> list[EvidenceNode]:
        """Return the minimal claim-to-root chain for a claim matching ``query``.

        The chain is ordered ``[matched_claim, parent, ..., root]``. When the
        query matches claims in several branches, the shortest chain to a root
        wins (ties broken by insertion order). Returns an empty list when the
        buffer is empty or nothing matches.
        """
        best: Optional[list[EvidenceNode]] = None
        for node in self._matching_nodes(query):
            path = self._path_to_root(node)
            if best is None or len(path) < len(best):
                best = path
        return best if best is not None else []


def replay_evidence_path(
    buffer: EvidenceReplayBuffer, query: str
) -> list[EvidenceNode]:
    """Module-level replay API: the minimal claim-to-root chain in ``buffer``.

    Thin wrapper over :meth:`EvidenceReplayBuffer.replay_evidence_path` for
    callers that prefer a free function over a method.
    """
    return buffer.replay_evidence_path(query)
