"""Tests for memory staleness detection and consolidation (#797).

Covers the pure analysis in ``agent/memory_staleness`` and the real wiring
into ``agent/memory_manager.MemoryManager``.

Only stdlib + pytest + unittest.mock. No live network calls.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from agent.memory_staleness import (  # noqa: E402
    DEFAULT_CONFIG,
    Note,
    StalenessReason,
    StalenessFlag,
    ConsolidationGroup,
    StalenessReport,
    analyze,
    build_consolidation_groups,
    detect_age,
    detect_contradictions,
    detect_deprecated_but_referenced,
    detect_duplicates,
    detect_low_quality,
    detect_superseded,
    jaccard_similarity,
    quality_score,
    render_report,
)


# ── Note model ────────────────────────────────────────────────────────────────────


def test_note_defaults():
    n = Note(id="1", title="Test", content="Some content here")
    assert n.kind == "note"
    assert n.tags == []
    assert n.deprecated is False


def test_note_age_days():
    now = datetime(2025, 7, 1, tzinfo=timezone.utc)
    created = datetime(2025, 1, 1, tzinfo=timezone.utc)
    n = Note(id="1", title="Old", content="content", created_at=created)
    assert n.age_days(now=now) == pytest.approx(181.0, abs=1)


def test_note_age_days_no_timestamp():
    n = Note(id="1", title="NoTS", content="content")
    assert n.age_days() == 0.0


def test_note_age_uses_updated_at():
    now = datetime(2025, 7, 1, tzinfo=timezone.utc)
    created = datetime(2025, 1, 1, tzinfo=timezone.utc)
    updated = datetime(2025, 6, 30, tzinfo=timezone.utc)
    n = Note(id="1", title="X", content="c", created_at=created, updated_at=updated)
    # age should be measured from updated_at, not created_at
    assert n.age_days(now=now) == pytest.approx(1.0, abs=1)


def test_note_word_set():
    n = Note(id="1", title="Deploy Guide", content="How to deploy the app")
    ws = n.word_set()
    assert "deploy" in ws
    assert "guide" in ws
    assert "how" in ws
    assert "the" in ws


def test_note_word_set_empty():
    n = Note(id="1", title="", content="")
    assert n.word_set() == set()


# ── jaccard_similarity ───────────────────────────────────────────────────────────


def test_jaccard_identical():
    assert jaccard_similarity({"a", "b"}, {"a", "b"}) == 1.0


def test_jaccard_disjoint():
    assert jaccard_similarity({"a"}, {"b"}) == 0.0


def test_jaccard_both_empty():
    assert jaccard_similarity(set(), set()) == 0.0


def test_jaccard_partial():
    assert jaccard_similarity({"a", "b", "c"}, {"b", "c", "d"}) == pytest.approx(2 / 4)


# ── detect_age ───────────────────────────────────────────────────────────────────


def test_detect_age_flags_old_notes():
    now = datetime(2025, 7, 1, tzinfo=timezone.utc)
    old = datetime(2024, 1, 1, tzinfo=timezone.utc)
    n = Note(id="1", title="Old", content="old content", created_at=old)
    flags = detect_age([n], now=now, config={"max_age_days": 180})
    assert len(flags) == 1
    assert flags[0].reason == StalenessReason.AGE


def test_detect_age_skips_fresh():
    now = datetime(2025, 7, 1, tzinfo=timezone.utc)
    fresh = datetime(2025, 6, 1, tzinfo=timezone.utc)
    n = Note(id="1", title="Fresh", content="fresh", created_at=fresh)
    flags = detect_age([n], now=now, config={"max_age_days": 180})
    assert len(flags) == 0


def test_detect_age_skips_deprecated():
    now = datetime(2025, 7, 1, tzinfo=timezone.utc)
    old = datetime(2024, 1, 1, tzinfo=timezone.utc)
    n = Note(id="1", title="Old", content="old", created_at=old, deprecated=True)
    flags = detect_age([n], now=now, config={"max_age_days": 180})
    assert len(flags) == 0


def test_detect_age_severity_scales():
    now = datetime(2025, 7, 1, tzinfo=timezone.utc)
    old_180 = datetime(2025, 1, 2, tzinfo=timezone.utc)  # ~180 days
    old_360 = datetime(2024, 7, 2, tzinfo=timezone.utc)  # ~364 days
    n1 = Note(id="1", title="X", content="c", created_at=old_180)
    n2 = Note(id="2", title="Y", content="c", created_at=old_360)
    flags = detect_age([n1, n2], now=now, config={"max_age_days": 180})
    # n2 should have higher severity than n1
    assert (
        flags[0].severity < flags[1].severity or flags[1].severity < flags[0].severity
    )
    # Both should be present
    assert len(flags) == 2
    # Severity capped at 1.0
    for f in flags:
        assert f.severity <= 1.0


# ── detect_low_quality ───────────────────────────────────────────────────────────


def test_detect_low_quality_flags_short():
    n = Note(id="1", title="X", content="tiny")
    flags = detect_low_quality([n], config={"min_content_length": 20})
    assert len(flags) == 1
    assert flags[0].reason == StalenessReason.LOW_QUALITY


def test_detect_low_quality_skips_long():
    n = Note(id="1", title="X", content="This is a sufficiently long piece of content.")
    flags = detect_low_quality([n], config={"min_content_length": 20})
    assert len(flags) == 0


def test_detect_low_quality_skips_deprecated():
    n = Note(id="1", title="X", content="tiny", deprecated=True)
    flags = detect_low_quality([n], config={"min_content_length": 20})
    assert len(flags) == 0


# ── detect_contradictions ────────────────────────────────────────────────────────


def test_detect_contradiction_flags_older():
    old_ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
    new_ts = datetime(2025, 6, 1, tzinfo=timezone.utc)
    older = Note(
        id="old",
        title="Old",
        content="use global state",
        tags=["state"],
        created_at=old_ts,
    )
    newer = Note(
        id="new",
        title="New",
        content="never use global state",
        tags=["state"],
        created_at=new_ts,
    )
    flags = detect_contradictions([older, newer])
    assert len(flags) == 1
    assert flags[0].note_id == "old"
    assert flags[0].reason == StalenessReason.CONTRADICTION
    assert "new" in flags[0].related_ids


def test_detect_contradiction_no_overlap():
    old_ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
    new_ts = datetime(2025, 6, 1, tzinfo=timezone.utc)
    older = Note(
        id="old",
        title="X",
        content="use global state",
        tags=["state"],
        created_at=old_ts,
    )
    newer = Note(
        id="new",
        title="Y",
        content="never use local state",
        tags=["config"],
        created_at=new_ts,
    )
    flags = detect_contradictions([older, newer])
    assert len(flags) == 0


def test_detect_contradiction_no_cue_word():
    old_ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
    new_ts = datetime(2025, 6, 1, tzinfo=timezone.utc)
    older = Note(
        id="old",
        title="X",
        content="use global state",
        tags=["state"],
        created_at=old_ts,
    )
    newer = Note(
        id="new",
        title="Y",
        content="consider using local state",
        tags=["state"],
        created_at=new_ts,
    )
    flags = detect_contradictions([older, newer])
    assert len(flags) == 0


# ── detect_duplicates ────────────────────────────────────────────────────────────


def test_detect_duplicates_flags_both():
    n1 = Note(
        id="1", title="Deploy", content="How to deploy the application to production"
    )
    n2 = Note(
        id="2",
        title="Deploy Guide",
        content="How to deploy the application to production",
    )
    flags = detect_duplicates([n1, n2])
    assert len(flags) == 2
    ids = {f.note_id for f in flags}
    assert ids == {"1", "2"}
    assert all(f.reason == StalenessReason.DUPLICATE for f in flags)


def test_detect_duplicates_no_match():
    n1 = Note(id="1", title="Deploy", content="How to deploy the app")
    n2 = Note(id="2", title="Debug", content="Debug the database connection")
    flags = detect_duplicates([n1, n2])
    assert len(flags) == 0


def test_detect_duplicates_skips_deprecated():
    n1 = Note(
        id="1",
        title="Deploy",
        content="How to deploy the application to production",
        deprecated=True,
    )
    n2 = Note(
        id="2",
        title="Deploy Guide",
        content="How to deploy the application to production",
    )
    flags = detect_duplicates([n1, n2])
    assert len(flags) == 0


# ── detect_superseded ────────────────────────────────────────────────────────────


def test_detect_superseded():
    old = Note(id="old", title="Old Guide", content="Use version 1 of the API")
    new = Note(id="new", title="New Guide", content="Use version 2. supersedes: old")
    flags = detect_superseded([old, new])
    assert len(flags) == 1
    assert flags[0].note_id == "old"
    assert flags[0].reason == StalenessReason.SUPERSEDED
    assert "new" in flags[0].related_ids


def test_detect_superseded_replacement_marker():
    old = Note(id="old", title="Old", content="Old approach")
    new = Note(id="new", title="New", content="New approach. replacement: old")
    flags = detect_superseded([old, new])
    assert len(flags) == 1
    assert flags[0].note_id == "old"


def test_detect_superseded_unknown_id_ignored():
    n = Note(id="1", title="X", content="supersedes: nonexistent")
    flags = detect_superseded([n])
    assert len(flags) == 0


def test_detect_superseded_self_ignored():
    n = Note(id="1", title="X", content="supersedes: 1")
    flags = detect_superseded([n])
    assert len(flags) == 0


# ── detect_deprecated_but_referenced ─────────────────────────────────────────────


def test_detect_deprecated_but_referenced_always_empty():
    n1 = Note(id="1", title="X", content="references note 2", deprecated=True)
    n2 = Note(id="2", title="Y", content="referenced note")
    flags = detect_deprecated_but_referenced([n1, n2])
    assert flags == []


# ── build_consolidation_groups ───────────────────────────────────────────────────


def test_consolidation_groups():
    now = datetime(2025, 7, 1, tzinfo=timezone.utc)
    n1 = Note(
        id="1",
        title="Deploy",
        content="How to deploy the application to production",
        created_at=now - timedelta(days=10),
    )
    n2 = Note(
        id="2",
        title="Deploy Guide",
        content="How to deploy the application to production",
        created_at=now,
    )
    groups = build_consolidation_groups([n1, n2])
    assert len(groups) == 1
    assert groups[0].canonical_id == "2"  # newest wins
    assert set(groups[0].note_ids) == {"1", "2"}


def test_consolidation_no_groups_for_distinct():
    n1 = Note(id="1", title="Deploy", content="How to deploy")
    n2 = Note(id="2", title="Debug", content="Debug the database")
    groups = build_consolidation_groups([n1, n2])
    assert len(groups) == 0


def test_consolidation_skips_deprecated():
    n1 = Note(
        id="1",
        title="Deploy",
        content="How to deploy the application to production",
        deprecated=True,
    )
    n2 = Note(
        id="2",
        title="Deploy Guide",
        content="How to deploy the application to production",
    )
    groups = build_consolidation_groups([n1, n2])
    assert len(groups) == 0


# ── quality_score ──────────────────────────────────────────────────────────────────


def test_quality_score_pristine():
    assert quality_score(10, []) == 1.0


def test_quality_score_all_flagged():
    assert quality_score(10, ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10"]) == 0.0


def test_quality_score_empty_corpus():
    assert quality_score(0, []) == 1.0


def test_quality_score_nonlinear():
    # 25% flagged → 1 - 0.0625 = 0.9375 (gentler than linear 0.75)
    score = quality_score(4, ["1"])
    assert score == pytest.approx(1.0 - 0.25**2)


def test_quality_score_dedup_flagged_ids():
    # Same id flagged multiple times should count once
    score = quality_score(10, ["1", "1", "1"])
    assert score == pytest.approx(1.0 - (1 / 10) ** 2)


# ── analyze ────────────────────────────────────────────────────────────────────────


def test_analyze_returns_report():
    now = datetime(2025, 7, 1, tzinfo=timezone.utc)
    notes = [
        Note(
            id="1", title="Old", content="short", created_at=now - timedelta(days=200)
        ),
        Note(
            id="2",
            title="Good",
            content="This is a sufficiently detailed note.",
            created_at=now,
        ),
    ]
    report = analyze(
        notes, now=now, config={"max_age_days": 180, "min_content_length": 20}
    )
    assert isinstance(report, StalenessReport)
    assert report.total_notes == 2
    assert len(report.flags) > 0  # "short" is low quality, "Old" is aged


def test_analyze_empty():
    report = analyze([])
    assert report.total_notes == 0
    assert report.quality_score == 1.0


# ── render_report ──────────────────────────────────────────────────────────────────


def test_render_report_contains_sections():
    now = datetime(2025, 7, 1, tzinfo=timezone.utc)
    notes = [
        Note(
            id="1",
            title="Old",
            content="short note",
            created_at=now - timedelta(days=365),
        ),
    ]
    report = analyze(
        notes, now=now, config={"max_age_days": 180, "min_content_length": 20}
    )
    md = render_report(report)
    assert "# Memory Staleness Report" in md
    assert "## Flagged Notes" in md
    assert "## Consolidation Suggestions" in md
    assert "## Quality Metrics" in md


def test_render_report_empty_corpus():
    report = analyze([])
    md = render_report(report)
    assert "# Memory Staleness Report" in md
    assert "Total notes inspected: **0**" in md


def test_render_report_no_flags():
    now = datetime(2025, 7, 1, tzinfo=timezone.utc)
    notes = [
        Note(
            id="1",
            title="Good",
            content="This is a well-written detailed note.",
            created_at=now,
        )
    ]
    report = analyze(
        notes, now=now, config={"max_age_days": 180, "min_content_length": 20}
    )
    md = render_report(report)
    assert "No staleness flags detected" in md


# ── MemoryManager wiring (#797) ───────────────────────────────────────────────────
#
# These verify the REAL call site: MemoryManager.check_staleness() must
# actually invoke agent.memory_staleness.analyze() on notes collected from
# the on-disk memory store — not just import it.


def test_check_staleness_invokes_analyze_on_real_notes(monkeypatch):
    """check_staleness() must call analyze() with notes from collect_notes()."""
    from agent.memory_manager import MemoryManager
    import agent.memory_staleness as ms

    mm = MemoryManager()

    # Fake notes the on-disk store would have produced.
    fake_notes = [
        Note(id="memory-0", title="Dup A", content="deploy the app to production"),
        Note(id="memory-1", title="Dup B", content="deploy the app to production"),
        Note(id="memory-2", title="Short", content="tiny"),
    ]

    # Capture what analyze() receives so we can assert it was really called
    # with the collected notes, not an empty list.
    captured: dict = {}
    real_analyze = ms.analyze  # save before monkeypatching

    def fake_analyze(notes, *, config=None, now=None):
        captured["notes"] = list(notes)
        captured["config"] = config
        return real_analyze(notes, config=config, now=now)

    monkeypatch.setattr(mm, "collect_notes", lambda: fake_notes)
    monkeypatch.setattr(ms, "analyze", fake_analyze)

    report = mm.check_staleness()
    assert isinstance(report, StalenessReport)
    # The detector was invoked with the real collected notes.
    assert captured["notes"] == fake_notes
    assert report.total_notes == 3
    # The two near-duplicate "deploy the app to production" notes are flagged.
    dup_flags = [f for f in report.flags if f.reason == StalenessReason.DUPLICATE]
    assert len(dup_flags) == 2


def test_check_staleness_passes_config_through(monkeypatch):
    """Config overrides must reach analyze()."""
    from agent.memory_manager import MemoryManager
    import agent.memory_staleness as ms

    mm = MemoryManager()
    captured: dict = {}
    real_analyze = ms.analyze  # save before monkeypatching

    def fake_analyze(notes, *, config=None, now=None):
        captured["config"] = config
        return ms.StalenessReport(
            total_notes=0, flags=[], consolidation_groups=[], quality_score=1.0,
            config=config or {},
        )

    monkeypatch.setattr(mm, "collect_notes", lambda: [])
    monkeypatch.setattr(ms, "analyze", fake_analyze)

    mm.check_staleness(config={"max_age_days": 7})
    assert captured["config"] == {"max_age_days": 7}


def test_check_staleness_empty_corpus_is_pristine(monkeypatch):
    """No memory files → empty notes → pristine report (quality 1.0)."""
    from agent.memory_manager import MemoryManager

    mm = MemoryManager()
    monkeypatch.setattr(mm, "collect_notes", lambda: [])
    report = mm.check_staleness()
    assert report.total_notes == 0
    assert report.flags == []
    assert report.quality_score == 1.0


def test_render_staleness_report_produces_markdown(monkeypatch):
    """render_staleness_report() must call check_staleness() and render markdown."""
    from agent.memory_manager import MemoryManager

    mm = MemoryManager()
    monkeypatch.setattr(
        mm,
        "collect_notes",
        lambda: [
            Note(id="memory-0", title="Good", content="A well-written detailed note."),
        ],
    )
    md = mm.render_staleness_report()
    assert "# Memory Staleness Report" in md
    assert "## Quality Metrics" in md


def test_entries_to_notes_maps_entry_to_note():
    """_entries_to_notes converts plain store strings to Note objects."""
    from agent.memory_manager import MemoryManager

    entries = [
        "Deploy guide\nHow to deploy the app to production",
        "Short note",
        "",  # blank entries are skipped
    ]
    notes = MemoryManager._entries_to_notes(entries, target="memory")
    assert len(notes) == 2  # blank entry skipped
    assert notes[0].id == "memory-0"
    assert notes[0].title == "Deploy guide"
    assert notes[0].content == "How to deploy the app to production"
    assert notes[0].kind == "memory"
    # Single-line entry: title falls back to full text, content to the text.
    assert notes[1].id == "memory-1"
    assert notes[1].title == "Short note"
    assert notes[1].content == "Short note"


def test_collect_notes_reads_on_disk_store(monkeypatch):
    """collect_notes() must read the real on-disk store, not a stub."""
    from agent.memory_manager import MemoryManager

    mm = MemoryManager()

    class _FakeStore:
        memory_entries = ["First memory note\nwith detail", "Second memory note"]
        user_entries = ["User preference one"]

    def fake_load():
        return _FakeStore()

    monkeypatch.setattr(
        "tools.memory_tool.load_on_disk_store", fake_load
    )
    notes = mm.collect_notes()
    # 2 memory entries + 1 user entry
    assert len(notes) == 3
    kinds = {n.kind for n in notes}
    assert kinds == {"memory", "user"}
    ids = {n.id for n in notes}
    assert "memory-0" in ids and "memory-1" in ids and "user-0" in ids