"""Skills-as-Graph: a typed dependency/composition/governance graph over skills.

This is the first increment of the AIP ("A Graph Representation for Learning and
Governing Agent Skills") layer described in issue #246.  It models the four AIP
edge types between skills and exposes pure-Python composition/dependency queries
on top of the existing skill registry — no new runtime dependencies, no agent
behaviour change.

Edge types (declared in each skill's ``SKILL.md`` frontmatter under
``metadata.hermes.graph``)::

    metadata:
      hermes:
        graph:
          requires: [skill-a, skill-b]        # hard prerequisites (load these too)
          conflicts-with: [skill-c]           # cannot be co-loaded with these
          composes-with: [skill-d]            # canonical composition partners
          deprecates: [skill-e]               # supersedes these (governance)

Backward compatibility: the long-standing ``metadata.hermes.related_skills``
list (present on ~40 skills today) is folded into ``composes-with`` so the graph
has real edges from day one.  Skills with no graph declarations are isolated
nodes — they keep working exactly as before.

This module intentionally mirrors ``agent/skill_utils.py``: it avoids importing
the tool registry, CLI config, or any heavy dependency chain so it is safe to
import at module level.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from agent.skill_utils import (
    iter_skill_index_files,
    parse_frontmatter,
)

logger = logging.getLogger(__name__)

# The four AIP edge types, plus the order they are reported in.  ``requires`` is
# the only directed edge type that participates in closure/topological ordering;
# the others are governance/composition relations.
EDGE_TYPES: Tuple[str, ...] = (
    "requires",
    "conflicts-with",
    "composes-with",
    "deprecates",
)


def _normalize_edge_targets(value: Any) -> List[str]:
    """Coerce a frontmatter edge value into a clean list of skill names.

    Accepts an already-parsed list (the YAML-normal case), a single string, or
    a bracketed/comma-separated string (defensive, mirrors ``_parse_tags`` in
    ``skills_tool``).  Order is preserved and duplicates are dropped.
    """
    if not value:
        return []
    if isinstance(value, list):
        items = value
    else:
        text = str(value).strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        items = text.split(",")
    out: List[str] = []
    seen: Set[str] = set()
    for item in items:
        name = str(item).strip().strip("\"'")
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def extract_skill_edges(frontmatter: Dict[str, Any]) -> Dict[str, List[str]]:
    """Extract the typed graph edges declared by a skill's frontmatter.

    Reads ``metadata.hermes.graph.<edge-type>`` for each of the four AIP edge
    types and folds the legacy ``metadata.hermes.related_skills`` list into
    ``composes-with`` (deduplicated, related_skills appended after explicit
    composes-with so explicit declarations win on ordering).

    Returns a dict keyed by every entry in :data:`EDGE_TYPES` (empty lists when
    the skill declares nothing for that type).  Malformed structures are
    tolerated by returning empty lists rather than raising.
    """
    edges: Dict[str, List[str]] = {etype: [] for etype in EDGE_TYPES}

    metadata = frontmatter.get("metadata")
    if not isinstance(metadata, dict):
        return edges
    hermes = metadata.get("hermes")
    if not isinstance(hermes, dict):
        return edges

    graph = hermes.get("graph")
    if isinstance(graph, dict):
        for etype in EDGE_TYPES:
            edges[etype] = _normalize_edge_targets(graph.get(etype))

    # Legacy compatibility: related_skills becomes a composes-with edge.
    related = _normalize_edge_targets(hermes.get("related_skills"))
    if related:
        seen = set(edges["composes-with"])
        for name in related:
            if name not in seen:
                seen.add(name)
                edges["composes-with"].append(name)

    return edges


@dataclass
class SkillNode:
    """A single skill in the graph and its declared outbound edges."""

    name: str
    category: Optional[str] = None
    path: Optional[str] = None
    edges: Dict[str, List[str]] = field(default_factory=dict)

    def targets(self, edge_type: str) -> List[str]:
        return list(self.edges.get(edge_type, []))


@dataclass
class GraphValidation:
    """Result of :meth:`SkillGraph.validate`.

    ``ok`` is True only when there are no **errors**.  An error is either a
    missing ``requires`` target (a hard prerequisite that cannot be satisfied =
    a broken skill) or a ``requires`` cycle (no valid load order).

    Missing targets on the advisory/governance edge types (``composes-with``,
    ``conflicts-with``, ``deprecates``) are **warnings**, not errors: those
    edges legitimately point at skills installed in another profile, a plugin,
    or an optional-skills group, so a default profile must still validate as
    ``ok``.  They are surfaced in ``missing_warnings`` so the data-quality
    signal is never lost (a CI/registry "strict" mode that promotes them to
    errors is left for a later increment).

    ``conflicts`` are reported as facts — two skills declaring
    ``conflicts-with`` each other is something the agent must avoid co-loading,
    but it does not make the graph invalid.
    """

    ok: bool
    # Errors (drive ok=False):
    missing_requires: List[Tuple[str, str]]  # (source, missing_required_target)
    requires_cycles: List[List[str]]
    # Warnings (do NOT drive ok):
    missing_warnings: List[Tuple[str, str, str]]  # (source, edge_type, missing_target)
    conflicts: List[Tuple[str, str]]  # sorted (a, b) pairs

    @property
    def missing_targets(self) -> List[Tuple[str, str, str]]:
        """All missing targets (errors + warnings), for callers that want one list."""
        merged = [(s, "requires", t) for (s, t) in self.missing_requires]
        merged.extend(self.missing_warnings)
        merged.sort()
        return merged

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "missing_requires": [
                {"skill": s, "target": t} for (s, t) in self.missing_requires
            ],
            "requires_cycles": [list(c) for c in self.requires_cycles],
            "missing_warnings": [
                {"skill": s, "edge": e, "target": t}
                for (s, e, t) in self.missing_warnings
            ],
            "conflicts": [{"a": a, "b": b} for (a, b) in self.conflicts],
        }


class SkillGraph:
    """A typed graph over skills supporting dependency/composition queries.

    Build with :meth:`from_skills_dirs` (scans ``SKILL.md`` files) or
    :meth:`from_frontmatters` (in-memory, used by tests and the evolution
    pipeline before a skill is written to disk).
    """

    def __init__(self, nodes: Dict[str, SkillNode]):
        self._nodes: Dict[str, SkillNode] = nodes

    # ── Construction ──────────────────────────────────────────────────────

    @classmethod
    def from_frontmatters(
        cls,
        skills: Iterable[Tuple[str, Dict[str, Any]]],
        *,
        categories: Optional[Dict[str, str]] = None,
        paths: Optional[Dict[str, str]] = None,
    ) -> "SkillGraph":
        """Build a graph from ``(skill_name, frontmatter_dict)`` pairs.

        Later entries with a name already seen are ignored (first definition
        wins, matching the local-dir-precedence semantics of the registry).
        """
        categories = categories or {}
        paths = paths or {}
        nodes: Dict[str, SkillNode] = {}
        for name, frontmatter in skills:
            name = str(name).strip()
            if not name or name in nodes:
                continue
            nodes[name] = SkillNode(
                name=name,
                category=categories.get(name),
                path=paths.get(name),
                edges=extract_skill_edges(frontmatter or {}),
            )
        return cls(nodes)

    @classmethod
    def from_skills_dirs(cls, skills_dirs: Iterable[Path]) -> "SkillGraph":
        """Build a graph by scanning ``SKILL.md`` files under each directory.

        Directories are scanned in order; the first definition of a given skill
        name wins (local dir precedence).  Unreadable or unparseable skill files
        are skipped with a debug log, never raising — a single bad skill must
        not break graph queries over the rest.
        """
        nodes: Dict[str, SkillNode] = {}
        for skills_dir in skills_dirs:
            if not skills_dir.is_dir():
                continue
            for skill_md in iter_skill_index_files(skills_dir, "SKILL.md"):
                try:
                    content = skill_md.read_text(encoding="utf-8")
                    frontmatter, _ = parse_frontmatter(content)
                except Exception as exc:  # noqa: BLE001 - one bad skill is non-fatal
                    logger.debug("skill_graph: skipping %s: %s", skill_md, exc)
                    continue
                name = str(frontmatter.get("name") or skill_md.parent.name).strip()
                if not name or name in nodes:
                    continue
                category = skill_md.parent.parent.name if skill_md.parent.parent else None
                try:
                    rel = str(skill_md.relative_to(skills_dir))
                except ValueError:
                    rel = str(skill_md)
                nodes[name] = SkillNode(
                    name=name,
                    category=category,
                    path=rel,
                    edges=extract_skill_edges(frontmatter),
                )
        return cls(nodes)

    # ── Introspection ─────────────────────────────────────────────────────

    def __contains__(self, name: str) -> bool:
        return name in self._nodes

    def __len__(self) -> int:
        return len(self._nodes)

    def names(self) -> List[str]:
        return sorted(self._nodes)

    def node(self, name: str) -> Optional[SkillNode]:
        return self._nodes.get(name)

    def edges_of(self, name: str) -> Dict[str, List[str]]:
        """Outbound edges of *name*, or all-empty lists if the skill is unknown."""
        node = self._nodes.get(name)
        if node is None:
            return {etype: [] for etype in EDGE_TYPES}
        return {etype: node.targets(etype) for etype in EDGE_TYPES}

    # ── Validation ────────────────────────────────────────────────────────

    def validate(self) -> GraphValidation:
        """Validate the graph.

        Errors (set ``ok=False``):
          * a ``requires`` edge points at a skill not in the graph (a hard
            prerequisite that cannot be satisfied);
          * a cycle exists in the ``requires`` relation.

        Warnings (reported, do not affect ``ok``):
          * a ``composes-with`` / ``conflicts-with`` / ``deprecates`` edge points
            at a skill not in the graph — these are advisory/governance edges
            that may legitimately reference skills in another profile or plugin;
          * declared ``conflicts-with`` pairs.
        """
        missing_requires: List[Tuple[str, str]] = []
        missing_warnings: List[Tuple[str, str, str]] = []
        for node in self._nodes.values():
            for etype in EDGE_TYPES:
                for target in node.targets(etype):
                    if target in self._nodes:
                        continue
                    if etype == "requires":
                        missing_requires.append((node.name, target))
                    else:
                        missing_warnings.append((node.name, etype, target))

        cycles = self._find_requires_cycles()
        conflicts = self._collect_conflicts()

        ok = not missing_requires and not cycles
        # Sort for deterministic output.
        missing_requires.sort()
        missing_warnings.sort()
        conflicts = sorted(conflicts)
        return GraphValidation(
            ok=ok,
            missing_requires=missing_requires,
            requires_cycles=cycles,
            missing_warnings=missing_warnings,
            conflicts=conflicts,
        )

    def _requires_targets(self, name: str) -> List[str]:
        """``requires`` targets of *name* that actually exist in the graph."""
        node = self._nodes.get(name)
        if node is None:
            return []
        return [t for t in node.targets("requires") if t in self._nodes]

    def _find_requires_cycles(self) -> List[List[str]]:
        """Return every elementary cycle in the ``requires`` relation.

        Uses iterative DFS with a three-colour marking (white/grey/black).  A
        back-edge to a grey node closes a cycle; the cycle path is reconstructed
        from the current DFS stack.  Each distinct cycle (by its set of nodes)
        is reported once, normalised to start at its lexicographically smallest
        member for stable output.
        """
        WHITE, GREY, BLACK = 0, 1, 2
        colour: Dict[str, int] = {n: WHITE for n in self._nodes}
        cycles: List[List[str]] = []
        seen_cycle_keys: Set[frozenset] = set()

        for start in sorted(self._nodes):
            if colour[start] != WHITE:
                continue
            # Stack frames: (node, iterator over its requires targets).
            stack: List[Tuple[str, Iterable[str]]] = [
                (start, iter(self._requires_targets(start)))
            ]
            path: List[str] = [start]
            colour[start] = GREY
            while stack:
                node, it = stack[-1]
                advanced = False
                for target in it:
                    if colour[target] == WHITE:
                        colour[target] = GREY
                        path.append(target)
                        stack.append((target, iter(self._requires_targets(target))))
                        advanced = True
                        break
                    if colour[target] == GREY:
                        # Back-edge: cycle from target..node.
                        idx = path.index(target)
                        cycle = path[idx:]
                        key = frozenset(cycle)
                        if key not in seen_cycle_keys:
                            seen_cycle_keys.add(key)
                            cycles.append(_canonical_cycle(cycle))
                if advanced:
                    continue
                # Exhausted: pop.
                colour[node] = BLACK
                stack.pop()
                if path:
                    path.pop()
        cycles.sort()
        return cycles

    def _collect_conflicts(self) -> List[Tuple[str, str]]:
        """Collect declared ``conflicts-with`` pairs (deduped, undirected)."""
        pairs: Set[Tuple[str, str]] = set()
        for node in self._nodes.values():
            for target in node.targets("conflicts-with"):
                if target == node.name:
                    continue
                pair = tuple(sorted((node.name, target)))
                pairs.add(pair)  # type: ignore[arg-type]
        return list(pairs)

    # ── Queries ───────────────────────────────────────────────────────────

    def closure(self, name: str) -> List[str]:
        """Minimal set of skills that must be loaded to satisfy *name*.

        Returns the transitive ``requires`` closure of *name* **including**
        *name* itself, in dependency-first topological order (a skill appears
        after every skill it requires).  Raises :class:`KeyError` if *name* is
        not in the graph.  ``requires`` cycles are tolerated — the closure is
        still complete; ordering within a cycle is arbitrary but stable.

        Edge targets that do not exist in the graph are silently skipped (they
        are reported by :meth:`validate` as missing targets, not here).
        """
        if name not in self._nodes:
            raise KeyError(name)

        # Collect the reachable set via the requires relation.
        reachable: Set[str] = set()
        stack = [name]
        while stack:
            current = stack.pop()
            if current in reachable:
                continue
            reachable.add(current)
            for target in self._requires_targets(current):
                if target not in reachable:
                    stack.append(target)

        return self._topo_sort(reachable)

    def blast(self, name: str) -> Dict[str, List[str]]:
        """Every skill affected if *name* were added, changed, or removed.

        Returns a dict with three keys:
          * ``dependents`` — skills that transitively ``require`` *name* (they
            break if *name* changes/disappears);
          * ``conflicts`` — skills that declare a ``conflicts-with`` edge to or
            from *name* (adding *name* would collide with them);
          * ``composes`` — direct ``composes-with`` partners of *name* (their
            canonical composition with *name* changes).

        *name* itself is never included.  ``KeyError`` is raised when *name* is
        unknown, so callers can distinguish "no blast radius" from "no such
        skill".
        """
        if name not in self._nodes:
            raise KeyError(name)

        # Reverse requires-reachability: who depends on `name`?
        dependents: Set[str] = set()
        stack = [name]
        while stack:
            current = stack.pop()
            for other in self._nodes.values():
                if other.name == name or other.name in dependents:
                    continue
                if current in self._requires_targets(other.name):
                    dependents.add(other.name)
                    stack.append(other.name)
        dependents.discard(name)

        conflicts: Set[str] = set()
        node = self._nodes[name]
        for target in node.targets("conflicts-with"):
            if target in self._nodes and target != name:
                conflicts.add(target)
        for other in self._nodes.values():
            if other.name == name:
                continue
            if name in other.targets("conflicts-with"):
                conflicts.add(other.name)

        composes: Set[str] = {
            t for t in node.targets("composes-with") if t in self._nodes and t != name
        }

        return {
            "dependents": sorted(dependents),
            "conflicts": sorted(conflicts),
            "composes": sorted(composes),
        }

    def topological_order(self) -> List[str]:
        """Full graph in dependency-first ``requires`` order.

        A skill appears after every skill it requires.  If ``requires`` cycles
        exist the order is still total and stable but the cycle members'
        relative order is arbitrary (a cycle has no valid topological order).
        """
        return self._topo_sort(set(self._nodes))

    def _topo_sort(self, subset: Set[str]) -> List[str]:
        """Dependency-first topological sort restricted to *subset*.

        Deterministic (ties broken by name).  Cycle-tolerant: nodes still in the
        cycle when no zero-in-degree node remains are emitted in name order so
        the result is always a complete permutation of *subset*.
        """
        # Build in-degree over the requires relation, restricted to subset.
        deps: Dict[str, Set[str]] = {}
        for n in subset:
            deps[n] = {t for t in self._requires_targets(n) if t in subset}

        result: List[str] = []
        placed: Set[str] = set()
        remaining = set(subset)
        while remaining:
            ready = sorted(
                n for n in remaining if deps[n] <= placed
            )
            if not ready:
                # Cycle: nothing has all deps satisfied. Emit the rest in name
                # order to guarantee termination and a complete result.
                ready = sorted(remaining)
            for n in ready:
                result.append(n)
                placed.add(n)
                remaining.discard(n)
        return result

    # ── Rendering ─────────────────────────────────────────────────────────

    def to_dot(self) -> str:
        """Render the graph in Graphviz DOT format.

        Edge styling encodes the relation type so a rendered graph is legible:
        ``requires`` solid, ``conflicts-with`` red dashed (no direction),
        ``composes-with`` dotted, ``deprecates`` bold grey.  ``conflicts-with``
        is rendered once per undirected pair to avoid double arrows.
        """
        lines: List[str] = ["digraph skills {", "  rankdir=LR;", "  node [shape=box];"]
        for name in sorted(self._nodes):
            lines.append(f'  "{name}";')

        seen_conflicts: Set[Tuple[str, str]] = set()
        styles = {
            "requires": '[label="requires"]',
            "conflicts-with": '[label="conflicts-with" color="red" style="dashed" dir="none"]',
            "composes-with": '[label="composes-with" style="dotted"]',
            "deprecates": '[label="deprecates" color="gray" style="bold"]',
        }
        for name in sorted(self._nodes):
            node = self._nodes[name]
            for etype in EDGE_TYPES:
                for target in node.targets(etype):
                    if etype == "conflicts-with":
                        pair = tuple(sorted((name, target)))
                        if pair in seen_conflicts:
                            continue
                        seen_conflicts.add(pair)  # type: ignore[arg-type]
                    lines.append(f'  "{name}" -> "{target}" {styles[etype]};')
        lines.append("}")
        return "\n".join(lines)


def _canonical_cycle(cycle: List[str]) -> List[str]:
    """Rotate *cycle* to start at its lexicographically smallest member.

    Keeps cycle reporting stable regardless of which node the DFS entered from.
    """
    if not cycle:
        return cycle
    start = min(range(len(cycle)), key=lambda i: cycle[i])
    return cycle[start:] + cycle[:start]
