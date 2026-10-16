"""Tests for coordination-network edge recording on TrajectoryLog (#2996).

The "coordination measurement pass" slice: multi-agent runs record message /
file-write / file-read edges with token cost, so delegation overhead is
visible and comparable across topologies. Pure logging on the existing
TrajectoryLog — no behavior change, and the serialized shape of a trajectory
with no edges is byte-identical to before the feature.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_trajectory_logger import (
    CoordinationEdge,
    TrajectoryLog,
    load_trajectory,
)


class TestCoordinationEdge:
    def test_default_timestamp(self):
        e = CoordinationEdge("agent-a", "agent-b", "message")
        assert e.source == "agent-a"
        assert e.target == "agent-b"
        assert e.edge_type == "message"
        assert e.cost_tokens is None
        assert e.timestamp

    def test_to_dict_omits_cost_when_unset(self):
        e = CoordinationEdge("a", "b", "file_write")
        d = e.to_dict()
        assert "cost_tokens" not in d
        assert d["type"] == "file_write"

    def test_to_dict_includes_cost(self):
        e = CoordinationEdge("a", "shared.md", "file_write", cost_tokens=320)
        d = e.to_dict()
        assert d["cost_tokens"] == 320

    def test_from_dict_roundtrip(self):
        e = CoordinationEdge("a", "b", "message", cost_tokens=10, timestamp="t")
        restored = CoordinationEdge.from_dict(e.to_dict())
        assert restored.source == "a"
        assert restored.target == "b"
        assert restored.edge_type == "message"
        assert restored.cost_tokens == 10
        assert restored.timestamp == "t"


class TestTrajectoryLogEdges:
    def test_edges_absent_by_default(self):
        """A trajectory with no edges serializes exactly as before (no key)."""
        log = TrajectoryLog(session_id="s", date="2026-08-21")
        log.add_tool_call("terminal", {"cmd": "ls"}, "out")
        d = log.to_dict()
        assert "edges" not in d

    def test_add_and_serialize_edge(self):
        log = TrajectoryLog(session_id="s", date="2026-08-21")
        log.add_coordination_edge("research", "analysis", "message", cost_tokens=150)
        d = log.to_dict()
        assert d["edges"] == [
            {
                "timestamp": d["edges"][0]["timestamp"],
                "source": "research",
                "target": "analysis",
                "type": "message",
                "cost_tokens": 150,
            }
        ]

    def test_load_roundtrip_preserves_edges(self, tmp_path):
        log = TrajectoryLog(session_id="s1", date="2026-08-21")
        log.add_tool_call("patch", {"path": "x"})
        log.add_coordination_edge("agent-a", "shared.md", "file_write", cost_tokens=88)
        p = log.save(tmp_path)
        restored = load_trajectory(p)
        assert restored is not None
        assert len(restored.edges) == 1
        e = restored.edges[0]
        assert e.source == "agent-a"
        assert e.target == "shared.md"
        assert e.edge_type == "file_write"
        assert e.cost_tokens == 88
        # entries unaffected
        assert len(restored.entries) == 1

    def test_load_ignores_non_dict_edges(self, tmp_path):
        p = tmp_path / "x.json"
        p.write_text(json.dumps({"date": "2026-08-21", "edges": [1, "bad", {}]}),
                     encoding="utf-8")
        restored = load_trajectory(p)
        assert restored is not None
        # Non-dict entries (1, "bad") are skipped; the empty dict {} is a
        # valid dict and produces a default edge (source="", target="", …).
        assert len(restored.edges) == 1
        assert restored.edges[0].source == ""
