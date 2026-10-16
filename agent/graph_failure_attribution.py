"""Graph-structured failure attribution for the evolution pipeline's diagnosis stage.

Implements #3278: Builds an adaptive influence graph over linear execution traces
and traverses dependency edges to localize the critical root cause of failures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


@dataclass
class ExecutionNode:
    """A discrete node representing an action, tool call, or stage in the execution trace."""

    node_id: str
    step_index: int
    stage: str
    action: str
    status: str  # "success" | "failure" | "timeout"
    error: Optional[str] = None
    dependencies: set[str] = field(default_factory=set)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AttributionReport:
    """Structured attribution localizing the root cause of a trace failure."""

    critical_node_id: str
    culprit_stage: str
    culprit_action: str
    root_cause: str
    influence_path: list[str]
    confidence: float = 0.9

    def render_markdown(self) -> str:
        lines = [
            "[GRAPH-STRUCTURED FAILURE ATTRIBUTION]",
            f"• Culprit Stage: {self.culprit_stage}",
            f"• Culprit Action: {self.culprit_action} (Node: {self.critical_node_id})",
            f"• Root Cause: {self.root_cause}",
            f"• Influence Path: {' -> '.join(self.influence_path)}",
        ]
        return "\n".join(lines)


class InfluenceGraph:
    """Directed Acyclic Graph over trace execution nodes."""

    def __init__(self):
        self.nodes: dict[str, ExecutionNode] = {}
        self.edges: dict[str, set[str]] = {}  # source -> targets
        self.reverse_edges: dict[str, set[str]] = {}  # target -> sources

    def add_node(self, node: ExecutionNode) -> None:
        self.nodes[node.node_id] = node
        if node.node_id not in self.edges:
            self.edges[node.node_id] = set()
        if node.node_id not in self.reverse_edges:
            self.reverse_edges[node.node_id] = set()

        for dep in node.dependencies:
            self.add_edge(dep, node.node_id)

    def add_edge(self, source_id: str, target_id: str) -> None:
        if source_id not in self.edges:
            self.edges[source_id] = set()
        self.edges[source_id].add(target_id)

        if target_id not in self.reverse_edges:
            self.reverse_edges[target_id] = set()
        self.reverse_edges[target_id].add(source_id)

    @classmethod
    def build_from_trace(cls, trace_steps: list[dict[str, Any]]) -> InfluenceGraph:
        """Construct an influence graph from a sequential list of trace steps."""
        graph = cls()
        prev_node_id: Optional[str] = None

        for idx, step in enumerate(trace_steps):
            node_id = f"step_{idx}"
            stage = step.get("stage", "execution")
            action = step.get("action", step.get("tool", "llm_turn"))
            status = "failure" if step.get("error") or step.get("failed") else "success"
            error = step.get("error")

            deps = set()
            if prev_node_id:
                deps.add(prev_node_id)
            for dep in step.get("depends_on", []):
                deps.add(str(dep))

            node = ExecutionNode(
                node_id=node_id,
                step_index=idx,
                stage=stage,
                action=action,
                status=status,
                error=error,
                dependencies=deps,
                metadata=step.get("metadata", {}),
            )
            graph.add_node(node)
            prev_node_id = node_id

        return graph

    def attribute_failure(self) -> Optional[AttributionReport]:
        """Backward traversal to find the root culprit node."""
        if not self.nodes:
            return None

        # Find failed nodes
        failed_nodes = [
            node for node in self.nodes.values() if node.status == "failure"
        ]
        if not failed_nodes:
            # Check last node
            last_node = list(self.nodes.values())[-1]
            return AttributionReport(
                critical_node_id=last_node.node_id,
                culprit_stage=last_node.stage,
                culprit_action=last_node.action,
                root_cause="No explicit failure detected; last step reached without error",
                influence_path=[last_node.node_id],
                confidence=0.5,
            )

        # Start from the first failing node
        primary_failure = failed_nodes[0]
        visited = set()
        influence_path = []
        current = primary_failure.node_id

        # Backward traversal along reverse edges to trace lineage
        while current and current not in visited:
            visited.add(current)
            influence_path.append(current)
            sources = self.reverse_edges.get(current, set())
            if not sources:
                break
            # Find earliest failing or prerequisite source
            current = min(sources, key=lambda nid: self.nodes[nid].step_index) if sources else None

        influence_path.reverse()
        root_node = self.nodes[primary_failure.node_id]

        return AttributionReport(
            critical_node_id=root_node.node_id,
            culprit_stage=root_node.stage,
            culprit_action=root_node.action,
            root_cause=root_node.error or f"Action {root_node.action} failed during {root_node.stage}",
            influence_path=influence_path,
            confidence=0.95,
        )
