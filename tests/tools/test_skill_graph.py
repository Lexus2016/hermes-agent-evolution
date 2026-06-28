"""Tests for agent/skill_graph.py — the Skills-as-Graph (AIP) layer (issue #246).

Covers the success criteria from the issue:
  * empty graph,
  * single-edge requires resolution,
  * multi-hop transitive closure,
  * conflict detection (conflicts-with),
  * cycle detection (requires cycle = hard error),
  * deprecation chains,
  * blast radius (dependents / conflicts / composes),
  * missing edge-target detection,
  * topological ordering,
  * legacy related_skills -> composes-with folding,
  * DOT rendering,
  * the tools.skills_tool.skill_relationships wiring over a real skills dir.
"""

import json
from pathlib import Path

import pytest

from agent.skill_graph import (
    EDGE_TYPES,
    SkillGraph,
    extract_skill_edges,
    extract_skill_provides,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _fm(**graph_edges):
    """Build a frontmatter dict with metadata.hermes.graph edges."""
    graph = {k.replace("_", "-"): v for k, v in graph_edges.items()}
    return {"metadata": {"hermes": {"graph": graph}}}


def _graph(spec):
    """Build a SkillGraph from a {name: frontmatter} dict."""
    return SkillGraph.from_frontmatters(spec.items())


# ── extract_skill_edges ───────────────────────────────────────────────────


def test_extract_edges_empty_frontmatter():
    edges = extract_skill_edges({})
    assert set(edges) == set(EDGE_TYPES)
    assert all(v == [] for v in edges.values())


def test_extract_edges_all_types():
    fm = _fm(
        requires=["a", "b"],
        conflicts_with=["c"],
        composes_with=["d"],
        deprecates=["e"],
    )
    edges = extract_skill_edges(fm)
    assert edges["requires"] == ["a", "b"]
    assert edges["conflicts-with"] == ["c"]
    assert edges["composes-with"] == ["d"]
    assert edges["deprecates"] == ["e"]


def test_extract_edges_folds_related_skills_into_composes():
    fm = {
        "metadata": {
            "hermes": {
                "related_skills": ["legacy-x", "legacy-y"],
                "graph": {"composes-with": ["explicit-z"]},
            }
        }
    }
    edges = extract_skill_edges(fm)
    # Explicit composes-with first, then related_skills appended, deduped.
    assert edges["composes-with"] == ["explicit-z", "legacy-x", "legacy-y"]


def test_extract_edges_related_skills_only():
    fm = {"metadata": {"hermes": {"related_skills": ["x"]}}}
    edges = extract_skill_edges(fm)
    assert edges["composes-with"] == ["x"]
    assert edges["requires"] == []


def test_extract_edges_string_and_bracket_forms():
    # Defensive: malformed YAML can leave strings in place of lists.
    fm = {"metadata": {"hermes": {"graph": {"requires": "[a, b]"}}}}
    assert extract_skill_edges(fm)["requires"] == ["a", "b"]
    fm2 = {"metadata": {"hermes": {"graph": {"requires": "a, b"}}}}
    assert extract_skill_edges(fm2)["requires"] == ["a", "b"]


def test_extract_edges_malformed_metadata_is_tolerated():
    assert extract_skill_edges({"metadata": "oops"}) == {e: [] for e in EDGE_TYPES}
    assert extract_skill_edges({"metadata": {"hermes": "oops"}}) == {
        e: [] for e in EDGE_TYPES
    }
    assert extract_skill_edges({"metadata": {"hermes": {"graph": "oops"}}}) == {
        e: [] for e in EDGE_TYPES
    }


# ── Empty / trivial graphs ──────────────────────────────────────────────────


def test_empty_graph_is_valid():
    g = _graph({})
    assert len(g) == 0
    assert g.names() == []
    v = g.validate()
    assert v.ok
    assert v.missing_targets == []
    assert v.missing_requires == []
    assert v.missing_warnings == []
    assert v.requires_cycles == []
    assert v.conflicts == []
    assert g.topological_order() == []


def test_isolated_nodes_valid():
    g = _graph({"a": {}, "b": {}})
    v = g.validate()
    assert v.ok
    assert g.closure("a") == ["a"]
    assert g.blast("a") == {"dependents": [], "conflicts": [], "composes": []}


# ── Closure (transitive requires) ───────────────────────────────────────────


def test_single_edge_closure():
    g = _graph({"app": _fm(requires=["lib"]), "lib": {}})
    # closure includes the skill itself, deps first.
    assert g.closure("app") == ["lib", "app"]
    assert g.closure("lib") == ["lib"]


def test_multi_hop_closure_is_dependency_first():
    # app -> mid -> base, plus app -> util
    g = _graph(
        {
            "app": _fm(requires=["mid", "util"]),
            "mid": _fm(requires=["base"]),
            "util": {},
            "base": {},
        }
    )
    order = g.closure("app")
    assert set(order) == {"app", "mid", "util", "base"}
    # Every dependency appears before its dependent.
    assert order.index("base") < order.index("mid")
    assert order.index("mid") < order.index("app")
    assert order.index("util") < order.index("app")


def test_diamond_closure_no_duplicates():
    # a -> b, a -> c, b -> d, c -> d  (diamond)
    g = _graph(
        {
            "a": _fm(requires=["b", "c"]),
            "b": _fm(requires=["d"]),
            "c": _fm(requires=["d"]),
            "d": {},
        }
    )
    order = g.closure("a")
    assert sorted(order) == ["a", "b", "c", "d"]
    assert len(order) == len(set(order))
    assert order.index("d") < order.index("b")
    assert order.index("d") < order.index("c")
    assert order[-1] == "a"


def test_closure_skips_missing_targets():
    g = _graph({"a": _fm(requires=["ghost"])})
    # ghost is not in the graph; closure is just {a}, missing reported by validate.
    assert g.closure("a") == ["a"]


def test_closure_unknown_skill_raises_keyerror():
    g = _graph({"a": {}})
    with pytest.raises(KeyError):
        g.closure("nope")


def test_closure_tolerates_requires_cycle():
    g = _graph({"a": _fm(requires=["b"]), "b": _fm(requires=["a"])})
    assert sorted(g.closure("a")) == ["a", "b"]


# ── Cycle detection ─────────────────────────────────────────────────────────


def test_requires_cycle_is_hard_error():
    g = _graph({"a": _fm(requires=["b"]), "b": _fm(requires=["a"])})
    v = g.validate()
    assert not v.ok
    assert len(v.requires_cycles) == 1
    assert set(v.requires_cycles[0]) == {"a", "b"}
    # Canonicalised to start at smallest member.
    assert v.requires_cycles[0][0] == "a"


def test_self_requires_cycle_detected():
    g = _graph({"a": _fm(requires=["a"])})
    v = g.validate()
    assert not v.ok
    assert any(set(c) == {"a"} for c in v.requires_cycles)


def test_three_node_cycle():
    g = _graph(
        {
            "a": _fm(requires=["b"]),
            "b": _fm(requires=["c"]),
            "c": _fm(requires=["a"]),
        }
    )
    v = g.validate()
    assert not v.ok
    assert len(v.requires_cycles) == 1
    assert set(v.requires_cycles[0]) == {"a", "b", "c"}


def test_conflicts_and_composes_cycles_are_not_errors():
    # Only requires cycles are hard errors; mutual conflicts/composes are fine.
    g = _graph(
        {
            "a": _fm(conflicts_with=["b"], composes_with=["c"]),
            "b": _fm(conflicts_with=["a"]),
            "c": _fm(composes_with=["a"]),
        }
    )
    v = g.validate()
    assert v.requires_cycles == []
    assert v.ok  # conflicts are warnings, not errors


# ── Conflict detection ──────────────────────────────────────────────────────


def test_conflict_detection_dedupes_undirected_pairs():
    g = _graph(
        {
            "a": _fm(conflicts_with=["b"]),
            "b": _fm(conflicts_with=["a"]),  # same pair, declared both ways
            "c": {},
        }
    )
    v = g.validate()
    assert v.conflicts == [("a", "b")]


def test_conflict_one_directional_still_detected():
    g = _graph({"a": _fm(conflicts_with=["b"]), "b": {}})
    v = g.validate()
    assert v.conflicts == [("a", "b")]


# ── Missing edge targets ────────────────────────────────────────────────────


def test_missing_requires_target_is_error():
    g = _graph({"a": _fm(requires=["ghost"])})
    v = g.validate()
    assert not v.ok
    assert ("a", "ghost") in v.missing_requires
    assert ("a", "requires", "ghost") in v.missing_targets


def test_missing_soft_edge_target_is_warning_not_error():
    # composes-with / conflicts-with / deprecates can point at skills in another
    # profile or a plugin — a missing target is a warning, the graph stays ok.
    g = _graph(
        {
            "a": _fm(
                composes_with=["plugin-skill"],
                conflicts_with=["other-profile-skill"],
                deprecates=["old-skill"],
            )
        }
    )
    v = g.validate()
    assert v.ok  # no missing requires, no cycle
    assert v.missing_requires == []
    warned = {(s, e, t) for (s, e, t) in v.missing_warnings}
    assert ("a", "composes-with", "plugin-skill") in warned
    assert ("a", "conflicts-with", "other-profile-skill") in warned
    assert ("a", "deprecates", "old-skill") in warned


def test_missing_target_across_edge_types_splits_severity():
    g = _graph(
        {
            "a": _fm(
                requires=["g1"],
                conflicts_with=["g2"],
                composes_with=["g3"],
                deprecates=["g4"],
            )
        }
    )
    v = g.validate()
    assert not v.ok  # the missing requires target g1 is a hard error
    assert v.missing_requires == [("a", "g1")]
    warned = {(s, e, t) for (s, e, t) in v.missing_warnings}
    assert ("a", "conflicts-with", "g2") in warned
    assert ("a", "composes-with", "g3") in warned
    assert ("a", "deprecates", "g4") in warned
    # The merged view still exposes everything.
    assert ("a", "requires", "g1") in v.missing_targets


def test_legacy_related_skills_missing_is_only_a_warning():
    # related_skills folds into composes-with; a dangling legacy ref must NOT
    # break validation of a default profile (the 16-dangling-refs case in the
    # real bundled skills).
    g = SkillGraph.from_frontmatters(
        [("a", {"metadata": {"hermes": {"related_skills": ["not-bundled"]}}})]
    )
    v = g.validate()
    assert v.ok
    assert ("a", "composes-with", "not-bundled") in v.missing_warnings


# ── Deprecation chains ──────────────────────────────────────────────────────


def test_deprecation_chain_validates():
    # v3 deprecates v2 deprecates v1 — a governance chain, all valid.
    g = _graph(
        {
            "tool-v3": _fm(deprecates=["tool-v2"]),
            "tool-v2": _fm(deprecates=["tool-v1"]),
            "tool-v1": {},
        }
    )
    v = g.validate()
    assert v.ok
    assert g.edges_of("tool-v3")["deprecates"] == ["tool-v2"]
    assert g.edges_of("tool-v2")["deprecates"] == ["tool-v1"]


# ── Blast radius ────────────────────────────────────────────────────────────


def test_blast_dependents_transitive():
    # base <- mid <- app  (who depends on base?)
    g = _graph(
        {
            "app": _fm(requires=["mid"]),
            "mid": _fm(requires=["base"]),
            "base": {},
        }
    )
    blast = g.blast("base")
    assert blast["dependents"] == ["app", "mid"]
    # Changing the leaf affects nobody.
    assert g.blast("app")["dependents"] == []


def test_blast_conflicts_both_directions():
    g = _graph(
        {
            "a": _fm(conflicts_with=["b"]),
            "b": {},
            "c": _fm(conflicts_with=["a"]),
        }
    )
    blast = g.blast("a")
    assert blast["conflicts"] == ["b", "c"]


def test_blast_composes_direct_partners():
    g = _graph({"a": _fm(composes_with=["b", "c"]), "b": {}, "c": {}})
    assert g.blast("a")["composes"] == ["b", "c"]


def test_blast_unknown_skill_raises():
    g = _graph({"a": {}})
    with pytest.raises(KeyError):
        g.blast("nope")


# ── Topological order ───────────────────────────────────────────────────────


def test_topological_order_full_graph():
    g = _graph(
        {
            "app": _fm(requires=["lib"]),
            "lib": _fm(requires=["core"]),
            "core": {},
            "standalone": {},
        }
    )
    order = g.topological_order()
    assert set(order) == {"app", "lib", "core", "standalone"}
    assert order.index("core") < order.index("lib") < order.index("app")


def test_topological_order_deterministic():
    spec = {"a": _fm(requires=["b"]), "b": {}, "c": {}}
    g1 = _graph(spec)
    g2 = _graph(spec)
    assert g1.topological_order() == g2.topological_order()


def test_topological_order_complete_even_with_cycle():
    g = _graph(
        {
            "a": _fm(requires=["b"]),
            "b": _fm(requires=["a"]),
            "c": {},
        }
    )
    order = g.topological_order()
    assert sorted(order) == ["a", "b", "c"]


# ── DOT rendering ───────────────────────────────────────────────────────────


def test_to_dot_contains_nodes_and_edges():
    g = _graph(
        {
            "a": _fm(requires=["b"], conflicts_with=["c"]),
            "b": {},
            "c": {},
        }
    )
    dot = g.to_dot()
    assert dot.startswith("digraph skills {")
    assert dot.rstrip().endswith("}")
    assert '"a";' in dot
    assert '"a" -> "b" [label="requires"];' in dot
    assert 'conflicts-with' in dot


def test_to_dot_conflict_rendered_once():
    g = _graph({"a": _fm(conflicts_with=["b"]), "b": _fm(conflicts_with=["a"])})
    dot = g.to_dot()
    # Undirected conflict pair should appear exactly once.
    assert dot.count('label="conflicts-with"') == 1


# ── provides / capabilities (issue #297 + #299) ─────────────────────────────


def test_extract_provides_empty_and_malformed():
    assert extract_skill_provides({}) == []
    assert extract_skill_provides({"metadata": "oops"}) == []
    assert extract_skill_provides({"metadata": {"hermes": {"graph": "oops"}}}) == []


def test_extract_provides_list_and_string_forms():
    fm = _fm(provides=["web-search", "pdf-export"])
    assert extract_skill_provides(fm) == ["web-search", "pdf-export"]
    fm2 = {"metadata": {"hermes": {"graph": {"provides": "a, b"}}}}
    assert extract_skill_provides(fm2) == ["a", "b"]


def test_node_carries_provides():
    g = _graph({"searcher": _fm(provides=["web-search"]), "plain": {}})
    assert g.node("searcher").provided() == ["web-search"]
    assert g.node("plain").provided() == []


def test_capability_surface_maps_capability_to_providers():
    g = _graph(
        {
            "brave": _fm(provides=["web-search"]),
            "google": _fm(provides=["web-search"]),
            "pdfkit": _fm(provides=["pdf-export"]),
            "plain": {},
        }
    )
    surface = g.capability_surface()
    assert surface == {
        "pdf-export": ["pdfkit"],
        "web-search": ["brave", "google"],  # sorted providers
    }


def test_requires_satisfied_by_capability_provider():
    # `report` requires the `web-search` capability, provided by `brave`.
    g = _graph(
        {
            "report": _fm(requires=["web-search"]),
            "brave": _fm(provides=["web-search"]),
        }
    )
    v = g.validate()
    assert v.ok  # capability requirement is satisfied, not a missing target
    assert v.missing_requires == []


def test_requires_capability_with_no_provider_is_missing():
    g = _graph({"report": _fm(requires=["web-search"])})
    v = g.validate()
    assert not v.ok
    assert ("report", "web-search") in v.missing_requires


def test_capability_provided_twice_is_a_warning_not_error():
    g = _graph(
        {
            "brave": _fm(provides=["web-search"]),
            "google": _fm(provides=["web-search"]),
        }
    )
    v = g.validate()
    assert v.ok  # ambiguous provider is a warning, graph stays valid
    assert ("web-search", ["brave", "google"]) in v.capability_conflicts


def test_single_provider_is_not_a_capability_conflict():
    g = _graph({"brave": _fm(provides=["web-search"]), "other": {}})
    v = g.validate()
    assert v.capability_conflicts == []


def test_validation_as_dict_includes_capability_conflicts():
    g = _graph(
        {"a": _fm(provides=["cap"]), "b": _fm(provides=["cap"])}
    )
    d = g.validate().as_dict()
    assert d["capability_conflicts"] == [
        {"capability": "cap", "providers": ["a", "b"]}
    ]


def test_capability_requires_does_not_pollute_skill_closure():
    # closure stays skill-name based; a capability requirement is validated but
    # not auto-expanded into the load closure (deferred to #298).
    g = _graph(
        {
            "report": _fm(requires=["web-search", "lib"]),
            "lib": {},
            "brave": _fm(provides=["web-search"]),
        }
    )
    assert g.validate().ok
    assert g.closure("report") == ["lib", "report"]


# ── from_skills_dirs + skill_relationships wiring ───────────────────────────


def _write_skill(skills_dir: Path, category: str, name: str, graph_block: str = ""):
    d = skills_dir / category / name
    d.mkdir(parents=True, exist_ok=True)
    fm_graph = f"\n    graph:\n{graph_block}" if graph_block else ""
    (d / "SKILL.md").write_text(
        f"""---
name: {name}
description: test skill {name}
metadata:
  hermes:
    tags: [test]{fm_graph}
---

# {name}
""",
        encoding="utf-8",
    )
    return d


def test_from_skills_dirs_scans_and_extracts(tmp_path):
    skills = tmp_path / "skills"
    _write_skill(
        skills,
        "cat",
        "app",
        graph_block="      requires: [lib]\n      conflicts-with: [other]",
    )
    _write_skill(skills, "cat", "lib")
    _write_skill(skills, "cat", "other")

    g = SkillGraph.from_skills_dirs([skills])
    assert set(g.names()) == {"app", "lib", "other"}
    assert g.edges_of("app")["requires"] == ["lib"]
    assert g.edges_of("app")["conflicts-with"] == ["other"]
    assert g.closure("app") == ["lib", "app"]
    v = g.validate()
    assert v.ok
    assert v.conflicts == [("app", "other")]


def test_from_skills_dirs_missing_dir_is_empty():
    g = SkillGraph.from_skills_dirs([Path("/nonexistent/skills/dir/xyz")])
    assert len(g) == 0
    assert g.validate().ok


def test_skill_relationships_tool_over_real_dir(tmp_path, monkeypatch):
    """End-to-end: the tools.skills_tool.skill_relationships agent surface."""
    home = tmp_path / ".hermes"
    skills = home / "skills"
    _write_skill(
        skills,
        "cat",
        "app",
        graph_block="      requires: [lib]\n      provides: [report-gen]",
    )
    _write_skill(skills, "cat", "lib", graph_block="      provides: [data-access]")

    monkeypatch.setenv("HERMES_HOME", str(home))
    import importlib

    import tools.skills_tool as mod

    importlib.reload(mod)
    # SKILLS_DIR is resolved at import time from HERMES_HOME.
    monkeypatch.setattr(mod, "SKILLS_DIR", skills)

    # Whole-graph mode.
    whole = json.loads(mod.skill_relationships())
    assert whole["success"] is True
    assert whole["skill_count"] == 2
    assert whole["validation"]["ok"] is True
    assert whole["topological_order"].index("lib") < whole[
        "topological_order"
    ].index("app")
    # Capability surface ("what can I do?") is exposed.
    assert whole["capability_surface"] == {
        "data-access": ["lib"],
        "report-gen": ["app"],
    }
    assert whole["validation"]["capability_conflicts"] == []

    # Single-skill mode.
    one = json.loads(mod.skill_relationships("app"))
    assert one["success"] is True
    assert one["skill"] == "app"
    assert one["edges"]["requires"] == ["lib"]
    assert one["provides"] == ["report-gen"]
    assert one["closure"] == ["lib", "app"]
    assert one["blast_radius"] == {
        "dependents": [],
        "conflicts": [],
        "composes": [],
    }

    # Unknown skill mode.
    miss = json.loads(mod.skill_relationships("ghost"))
    assert miss["success"] is False
    assert "not found" in miss["error"]

    # Reload again so other tests get a clean module bound to the real HERMES_HOME.
    monkeypatch.undo()
    importlib.reload(mod)
