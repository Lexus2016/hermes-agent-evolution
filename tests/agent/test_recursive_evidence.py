"""Tests for agent.recursive_evidence — evidence-tree model and replay API."""

import pytest

from agent.recursive_evidence import (
    EvidenceNode,
    EvidenceReplayBuffer,
    replay_evidence_path,
)


def _tool_turn(content, name="grep", tool_call_id="call-1"):
    """Build a tool-result turn matching make_tool_result_message's shape."""
    return {
        "role": "tool",
        "name": name,
        "tool_name": name,
        "content": content,
        "tool_call_id": tool_call_id,
    }


# ── EvidenceNode ─────────────────────────────────────────────────────────


class TestEvidenceNode:
    def test_defaults(self):
        node = EvidenceNode(node_id="n1", claim="a claim")
        assert node.supporting_passages == []
        assert node.children_ids == []
        assert node.parent_id is None
        assert node.confidence == 1.0
        assert node.source is None

    def test_is_root(self):
        root = EvidenceNode(node_id="n1", claim="root")
        child = EvidenceNode(node_id="n2", claim="child", parent_id="n1")
        assert root.is_root is True
        assert child.is_root is False


# ── Tree construction ────────────────────────────────────────────────────


class TestTreeConstruction:
    def test_add_claim_returns_node_and_is_retrievable(self):
        buf = EvidenceReplayBuffer()
        node = buf.add_claim("root claim")
        assert isinstance(node, EvidenceNode)
        assert buf.get(node.node_id) is node
        assert len(buf) == 1
        assert node.node_id in buf

    def test_auto_ids_are_unique(self):
        buf = EvidenceReplayBuffer()
        a = buf.add_claim("a")
        b = buf.add_claim("b")
        assert a.node_id != b.node_id
        assert len(buf) == 2

    def test_parent_child_links_are_bidirectional(self):
        buf = EvidenceReplayBuffer()
        root = buf.add_claim("root")
        child = buf.add_claim("child", parent_id=root.node_id)
        assert child.parent_id == root.node_id
        assert child.node_id in root.children_ids

    def test_confidence_is_clamped(self):
        buf = EvidenceReplayBuffer()
        assert buf.add_claim("hi", confidence=2.5).confidence == 1.0
        assert buf.add_claim("lo", confidence=-1.0).confidence == 0.0
        assert buf.add_claim("mid", confidence=0.4).confidence == 0.4

    def test_supporting_passages_are_copied_not_aliased(self):
        buf = EvidenceReplayBuffer()
        passages = ["p1"]
        node = buf.add_claim("c", supporting_passages=passages)
        passages.append("p2")
        assert node.supporting_passages == ["p1"]

    def test_explicit_node_id(self):
        buf = EvidenceReplayBuffer()
        node = buf.add_claim("c", node_id="custom")
        assert node.node_id == "custom"

    def test_duplicate_node_id_raises(self):
        buf = EvidenceReplayBuffer()
        buf.add_claim("c", node_id="dup")
        with pytest.raises(ValueError, match="duplicate node_id"):
            buf.add_claim("c2", node_id="dup")

    def test_unknown_parent_raises(self):
        buf = EvidenceReplayBuffer()
        with pytest.raises(ValueError, match="unknown parent_id"):
            buf.add_claim("orphan", parent_id="ghost")

    def test_roots_lists_only_roots_in_insertion_order(self):
        buf = EvidenceReplayBuffer()
        r1 = buf.add_claim("root one")
        buf.add_claim("child", parent_id=r1.node_id)
        r2 = buf.add_claim("root two")
        roots = buf.roots()
        assert [n.node_id for n in roots] == [r1.node_id, r2.node_id]


# ── Building nodes from tool-result turns ────────────────────────────────


class TestIngestToolResult:
    def test_string_content_becomes_single_passage(self):
        buf = EvidenceReplayBuffer()
        turn = _tool_turn("match found at foo.py:42", name="grep")
        node = buf.ingest_tool_result(turn, "the bug is in foo.py")
        assert node.supporting_passages == ["match found at foo.py:42"]
        assert node.source == "grep"
        assert node.claim == "the bug is in foo.py"

    def test_multimodal_content_yields_one_passage_per_text_part(self):
        buf = EvidenceReplayBuffer()
        content = [
            {"type": "text", "text": "line one"},
            {"type": "image_url", "image_url": {"url": "data:..."}},
            {"type": "text", "text": "line two"},
        ]
        node = buf.ingest_tool_result(_tool_turn(content), "claim")
        assert node.supporting_passages == ["line one", "line two"]

    def test_empty_content_yields_no_passages(self):
        buf = EvidenceReplayBuffer()
        node = buf.ingest_tool_result(_tool_turn(""), "claim")
        assert node.supporting_passages == []

    def test_source_falls_back_to_tool_name_key(self):
        buf = EvidenceReplayBuffer()
        turn = {"role": "tool", "tool_name": "read_file", "content": "data"}
        node = buf.ingest_tool_result(turn, "claim")
        assert node.source == "read_file"

    def test_ingest_links_under_parent(self):
        buf = EvidenceReplayBuffer()
        root = buf.add_claim("investigation")
        node = buf.ingest_tool_result(
            _tool_turn("evidence"), "derived claim", parent_id=root.node_id
        )
        assert node.parent_id == root.node_id
        assert node.node_id in root.children_ids

    def test_non_tool_turn_raises(self):
        buf = EvidenceReplayBuffer()
        with pytest.raises(ValueError, match="tool-result turn"):
            buf.ingest_tool_result({"role": "assistant", "content": "x"}, "claim")

    def test_non_dict_turn_raises(self):
        buf = EvidenceReplayBuffer()
        with pytest.raises(ValueError, match="tool-result turn"):
            buf.ingest_tool_result("not a turn", "claim")


# ── Path replay ──────────────────────────────────────────────────────────


class TestReplayEvidencePath:
    def test_empty_buffer_returns_empty(self):
        buf = EvidenceReplayBuffer()
        assert buf.replay_evidence_path("anything") == []

    def test_no_match_returns_empty(self):
        buf = EvidenceReplayBuffer()
        buf.add_claim("the sky is blue")
        assert buf.replay_evidence_path("quantum gravity") == []

    def test_empty_query_returns_empty(self):
        buf = EvidenceReplayBuffer()
        buf.add_claim("some claim")
        assert buf.replay_evidence_path("") == []
        assert buf.replay_evidence_path("   ") == []

    def test_single_chain_returns_claim_to_root(self):
        buf = EvidenceReplayBuffer()
        root = buf.add_claim("investigation root")
        mid = buf.add_claim("intermediate step", parent_id=root.node_id)
        leaf = buf.add_claim("the target file was modified", parent_id=mid.node_id)

        path = buf.replay_evidence_path("target file")
        assert [n.node_id for n in path] == [leaf.node_id, mid.node_id, root.node_id]
        # claim first, root last
        assert path[0].claim == "the target file was modified"
        assert path[-1].is_root is True

    def test_matching_root_returns_single_node(self):
        buf = EvidenceReplayBuffer()
        root = buf.add_claim("root evidence claim")
        path = buf.replay_evidence_path("root evidence")
        assert [n.node_id for n in path] == [root.node_id]

    def test_case_insensitive_match(self):
        buf = EvidenceReplayBuffer()
        root = buf.add_claim("The Bug Is In Parser")
        path = buf.replay_evidence_path("bug is in parser")
        assert path and path[0].node_id == root.node_id

    def test_multi_branch_returns_minimal_path(self):
        buf = EvidenceReplayBuffer()
        root = buf.add_claim("investigation root")
        # Shallow branch: match at depth 2 (node -> root).
        shallow = buf.add_claim("search shows the target file", parent_id=root.node_id)
        # Deep branch: match at depth 3 (node -> mid -> root).
        mid = buf.add_claim("first drill down", parent_id=root.node_id)
        deep = buf.add_claim("the target file was patched", parent_id=mid.node_id)

        path = buf.replay_evidence_path("target file")
        # Both `shallow` and `deep` claims contain "target file"; the shorter
        # chain (shallow -> root) must win.
        assert [n.node_id for n in path] == [shallow.node_id, root.node_id]
        assert deep.node_id not in {n.node_id for n in path}

    def test_module_level_function_delegates(self):
        buf = EvidenceReplayBuffer()
        root = buf.add_claim("root")
        leaf = buf.add_claim("a distinctive claim", parent_id=root.node_id)
        path = replay_evidence_path(buf, "distinctive")
        assert [n.node_id for n in path] == [leaf.node_id, root.node_id]

    def test_cycle_in_parent_links_does_not_hang(self):
        # Defensive: a corrupted parent chain must not loop forever.
        buf = EvidenceReplayBuffer()
        a = buf.add_claim("node a")
        b = buf.add_claim("node b cyclic", parent_id=a.node_id)
        a.parent_id = b.node_id  # force a cycle
        path = buf.replay_evidence_path("cyclic")
        ids = [n.node_id for n in path]
        assert len(ids) == len(set(ids))  # each node visited at most once

    def test_replay_over_tool_result_nodes(self):
        buf = EvidenceReplayBuffer()
        root = buf.add_claim("root question")
        buf.ingest_tool_result(
            _tool_turn("grep: hit in auth.py"),
            "auth.py contains the vulnerability",
            parent_id=root.node_id,
        )
        path = buf.replay_evidence_path("vulnerability")
        assert [n.claim for n in path] == [
            "auth.py contains the vulnerability",
            "root question",
        ]
        assert path[0].supporting_passages == ["grep: hit in auth.py"]
