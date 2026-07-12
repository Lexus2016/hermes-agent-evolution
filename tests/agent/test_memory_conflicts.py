"""Tests for conflict-preserving memory detection (#908).

Covers the pure analysis in ``agent/memory_conflicts`` and the real wiring
into ``agent/memory_manager.MemoryManager``.

Only stdlib + pytest + unittest.mock. No live network calls.
"""

from __future__ import annotations

import pytest

from agent.memory_conflicts import (  # noqa: E402
    DEFAULT_CONFIG,
    ConflictReport,
    MemoryConflict,
    Note,
    analyze_conflicts,
    detect_conflicts,
    render_conflict_report,
    split_claim,
)


# ── split_claim ────────────────────────────────────────────────────────────────────


def test_split_claim_colon():
    assert split_claim("Deploy target: staging") == ("Deploy target", "staging")


def test_split_claim_is():
    assert split_claim("Favorite color is blue") == ("Favorite color", "blue")


def test_split_claim_are():
    assert split_claim("Tests are passing") == ("Tests", "passing")


def test_split_claim_equals():
    assert split_claim("timeout = 30s") == ("timeout", "30s")


def test_split_claim_prefers():
    assert split_claim("User prefers dark mode") == ("User", "dark mode")


def test_split_claim_colon_priority_over_is():
    # Colon is tried first even when "is" also appears.
    assert split_claim("Status: build is green") == ("Status", "build is green")


def test_split_claim_no_separator():
    assert split_claim("A plain sentence with no structure") is None


def test_split_claim_empty_topic():
    assert split_claim(": staging") is None


def test_split_claim_empty_value():
    assert split_claim("Deploy target:") is None


def test_split_claim_empty_string():
    assert split_claim("") is None


# ── detect_conflicts ─────────────────────────────────────────────────────────────


def test_detect_conflicts_flags_same_topic_different_value():
    a = Note(id="a", title="Deploy target: staging", content="")
    b = Note(id="b", title="Deploy target: production", content="")
    conflicts = detect_conflicts([a, b])
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c.note_a_id == "a"
    assert c.note_b_id == "b"
    assert c.value_a == "staging"
    assert c.value_b == "production"
    assert c.topic_similarity >= DEFAULT_CONFIG["topic_similarity_threshold"]
    assert c.value_similarity < DEFAULT_CONFIG["value_similarity_threshold"]


def test_detect_conflicts_is_order_independent():
    a = Note(id="z-note", title="Deploy target: staging", content="")
    b = Note(id="a-note", title="Deploy target: production", content="")
    forward = detect_conflicts([a, b])
    backward = detect_conflicts([b, a])
    assert len(forward) == len(backward) == 1
    # note_a_id is always the lexicographically smaller id, regardless of
    # traversal order.
    assert forward[0].note_a_id == "a-note"
    assert backward[0].note_a_id == "a-note"


def test_detect_conflicts_list_is_sorted_regardless_of_traversal_order():
    # With more than one conflict, the *list* itself must also be
    # order-independent, not just the id ordering within each MemoryConflict.
    notes_forward = [
        Note(id="c-note", title="Deploy target: staging", content=""),
        Note(id="d-note", title="Deploy target: production", content=""),
        Note(id="a-note", title="Favorite color: blue", content=""),
        Note(id="b-note", title="Favorite color: red", content=""),
    ]
    notes_backward = list(reversed(notes_forward))
    forward = detect_conflicts(notes_forward)
    backward = detect_conflicts(notes_backward)
    assert len(forward) == len(backward) == 2
    forward_pairs = [(c.note_a_id, c.note_b_id) for c in forward]
    backward_pairs = [(c.note_a_id, c.note_b_id) for c in backward]
    assert forward_pairs == backward_pairs == sorted(forward_pairs)


def test_detect_conflicts_same_value_is_not_a_conflict():
    # Same topic, same value (a near-duplicate claim, not a conflict) — that's
    # memory_staleness.detect_duplicates' territory, not this module's.
    a = Note(id="a", title="Deploy target: staging", content="")
    b = Note(id="b", title="Deploy target: staging", content="")
    assert detect_conflicts([a, b]) == []


def test_detect_conflicts_different_topic_no_conflict():
    a = Note(id="a", title="Deploy target: staging", content="")
    b = Note(id="b", title="Favorite color: blue", content="")
    assert detect_conflicts([a, b]) == []


def test_detect_conflicts_skips_notes_without_claim_structure():
    a = Note(id="a", title="Deploy target: staging", content="")
    b = Note(id="b", title="A free-form note with no structure", content="")
    assert detect_conflicts([a, b]) == []


def test_detect_conflicts_skips_deprecated():
    a = Note(id="a", title="Deploy target: staging", content="", deprecated=True)
    b = Note(id="b", title="Deploy target: production", content="")
    assert detect_conflicts([a, b]) == []


def test_detect_conflicts_skips_empty_value_word_set():
    # "---" strips to a non-empty string (passes split_claim) but tokenizes
    # to an empty word set (no alnum characters) — nothing to compare.
    a = Note(id="a", title="Deploy target: ---", content="")
    b = Note(id="b", title="Deploy target: production", content="")
    assert detect_conflicts([a, b]) == []


def test_detect_conflicts_no_pair_among_single_note():
    a = Note(id="a", title="Deploy target: staging", content="")
    assert detect_conflicts([a]) == []


def test_detect_conflicts_config_overrides_thresholds():
    a = Note(id="a", title="Deploy target: staging", content="")
    b = Note(id="b", title="Deploy target: production", content="")
    # Jaccard similarity is always >= 0.0, so a value_similarity_threshold of
    # 0.0 means "value_sim >= 0.0" is always true — no pair can ever be
    # "different enough" to register as a conflict.
    conflicts = detect_conflicts([a, b], config={"value_similarity_threshold": 0.0})
    assert conflicts == []


# ── analyze_conflicts ────────────────────────────────────────────────────────────


def test_analyze_conflicts_returns_report():
    notes = [
        Note(id="a", title="Deploy target: staging", content=""),
        Note(id="b", title="Deploy target: production", content=""),
        Note(id="c", title="A free-form note", content=""),
    ]
    report = analyze_conflicts(notes)
    assert isinstance(report, ConflictReport)
    assert report.total_notes == 3
    assert report.total_claims == 2
    assert len(report.conflicts) == 1


def test_analyze_conflicts_empty():
    report = analyze_conflicts([])
    assert report.total_notes == 0
    assert report.total_claims == 0
    assert report.conflicts == []


def test_analyze_conflicts_passes_config_through():
    notes = [
        Note(id="a", title="Deploy target: staging", content=""),
        Note(id="b", title="Deploy target: production", content=""),
    ]
    report = analyze_conflicts(notes, config={"value_similarity_threshold": 0.0})
    assert report.conflicts == []
    assert report.config["value_similarity_threshold"] == 0.0


def test_analyze_conflicts_total_claims_excludes_empty_word_set():
    # "Deploy target: ---" passes split_claim (non-empty topic/value strings)
    # but "---" tokenizes to an empty word set, so detect_conflicts() cannot
    # use it as a claim. total_claims must reflect that same exclusion
    # rather than counting it as a usable claim.
    notes = [
        Note(id="a", title="Deploy target: staging", content=""),
        Note(id="b", title="Deploy target: ---", content=""),
    ]
    report = analyze_conflicts(notes)
    assert report.total_claims == 1


# ── render_conflict_report ───────────────────────────────────────────────────────


def test_render_conflict_report_contains_sections():
    notes = [
        Note(id="a", title="Deploy target: staging", content=""),
        Note(id="b", title="Deploy target: production", content=""),
    ]
    report = analyze_conflicts(notes)
    md = render_conflict_report(report)
    assert "# Memory Conflict Report" in md
    assert "## Conflicts" in md
    assert "CONFLICT" in md
    assert "`a`" in md and "`b`" in md


def test_render_conflict_report_empty_corpus():
    report = analyze_conflicts([])
    md = render_conflict_report(report)
    assert "# Memory Conflict Report" in md
    assert "Total notes inspected: **0**" in md


def test_render_conflict_report_no_conflicts():
    notes = [Note(id="a", title="Deploy target: staging", content="")]
    report = analyze_conflicts(notes)
    md = render_conflict_report(report)
    assert "No conflicting claims detected" in md


# ── MemoryManager wiring (#908) ───────────────────────────────────────────────────
#
# These verify the REAL call site: MemoryManager.detect_memory_conflicts() must
# actually invoke agent.memory_conflicts.analyze_conflicts() on notes collected
# from the on-disk memory store — not just import it.


def test_detect_memory_conflicts_invokes_analyze_on_real_notes(monkeypatch):
    """detect_memory_conflicts() must call analyze_conflicts() with collected notes."""
    from agent.memory_manager import MemoryManager
    import agent.memory_conflicts as mc

    mm = MemoryManager()

    fake_notes = [
        Note(id="memory-0", title="Deploy target: staging", content=""),
        Note(id="memory-1", title="Deploy target: production", content=""),
    ]

    captured: dict = {}
    real_analyze = mc.analyze_conflicts  # save before monkeypatching

    def fake_analyze(notes, *, config=None):
        captured["notes"] = list(notes)
        captured["config"] = config
        return real_analyze(notes, config=config)

    monkeypatch.setattr(mm, "collect_notes", lambda: fake_notes)
    monkeypatch.setattr(mc, "analyze_conflicts", fake_analyze)

    report = mm.detect_memory_conflicts()
    assert isinstance(report, ConflictReport)
    assert captured["notes"] == fake_notes
    assert report.total_notes == 2
    assert len(report.conflicts) == 1


def test_detect_memory_conflicts_passes_config_through(monkeypatch):
    from agent.memory_manager import MemoryManager
    import agent.memory_conflicts as mc

    mm = MemoryManager()
    captured: dict = {}
    real_analyze = mc.analyze_conflicts  # save before monkeypatching

    def fake_analyze(notes, *, config=None):
        captured["config"] = config
        return real_analyze(notes, config=config)

    monkeypatch.setattr(mm, "collect_notes", lambda: [])
    monkeypatch.setattr(mc, "analyze_conflicts", fake_analyze)

    mm.detect_memory_conflicts(config={"topic_similarity_threshold": 0.9})
    assert captured["config"] == {"topic_similarity_threshold": 0.9}


def test_detect_memory_conflicts_empty_corpus(monkeypatch):
    from agent.memory_manager import MemoryManager

    mm = MemoryManager()
    monkeypatch.setattr(mm, "collect_notes", lambda: [])
    report = mm.detect_memory_conflicts()
    assert report.total_notes == 0
    assert report.conflicts == []


def test_render_memory_conflicts_produces_markdown(monkeypatch):
    """render_memory_conflicts() must call detect_memory_conflicts() and render markdown."""
    from agent.memory_manager import MemoryManager

    mm = MemoryManager()
    monkeypatch.setattr(
        mm,
        "collect_notes",
        lambda: [
            Note(id="memory-0", title="Deploy target: staging", content=""),
            Note(id="memory-1", title="Deploy target: production", content=""),
        ],
    )
    md = mm.render_memory_conflicts()
    assert "# Memory Conflict Report" in md
    assert "CONFLICT" in md


def test_collect_notes_reused_by_conflict_detection(monkeypatch):
    """detect_memory_conflicts() must read the real on-disk store, not a stub."""
    from agent.memory_manager import MemoryManager

    mm = MemoryManager()

    class _FakeStore:
        memory_entries = ["Deploy target: staging", "Deploy target: production"]
        user_entries = []

    monkeypatch.setattr("tools.memory_tool.load_on_disk_store", lambda: _FakeStore())
    report = mm.detect_memory_conflicts()
    assert report.total_notes == 2
    assert len(report.conflicts) == 1
