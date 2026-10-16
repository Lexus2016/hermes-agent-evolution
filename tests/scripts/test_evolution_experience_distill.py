"""Tests for scripts/evolution_experience_distill.py — deterministic pattern distiller.

Hermetic by construction: the autouse fixtures in ``tests/conftest.py``
redirect ``HERMES_HOME`` to a per-test tempdir, and both the bank and the
distiller resolve ``get_hermes_home()`` lazily on every call.  Entries are
seeded through the bank's own ``append_entry`` so the tests exercise the real
storage path.  All runs pass an explicit ``now`` — nothing here depends on
the wall clock.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from agent import experience_bank as eb
from agent.experience_bank import ExperienceEntry, ExperiencePattern
import evolution_experience_distill as dist


NOW = 1_750_000_000.0
DAY = 86400.0


def _entry(**overrides) -> ExperienceEntry:
    """A high-confidence failure inside the evidence window by default."""
    base = dict(
        ts=NOW - DAY,
        session_id="sess",
        platform="cli",
        model="test-model",
        success=False,
        confidence="high",
        terminal_reason="completed",
        primary_dimension="tool",
        failure_category="network",
        tool="web_search",
        analysis="connection refused",
        stats={"tool_calls": 10, "tool_errors": 3, "iterations": 5},
    )
    base.update(overrides)
    return ExperienceEntry(**base)


def _seed_diverse(count: int = 3, **overrides) -> None:
    """Seed `count` evidence entries from distinct sessions."""
    for i in range(count):
        kwargs = dict(session_id=f"s{i}", ts=NOW - (i + 1) * DAY)
        kwargs.update(overrides)
        eb.append_entry(_entry(**kwargs))


# ---------------------------------------------------------------------------
# Clustering -> pattern
# ---------------------------------------------------------------------------

def test_diverse_failures_become_pattern():
    _seed_diverse(3)

    stats = dist.run_distillation(now=NOW)

    assert stats["entries_seen"] == 3
    assert stats["evidence_entries"] == 3
    assert stats["clusters"] == 1
    assert stats["patterns_written"] == 1
    assert stats["patterns_dropped"] == 0

    patterns = eb.load_patterns()
    assert len(patterns) == 1
    p = patterns[0]
    assert p.id == eb.pattern_id("tool", "network", "web_search")
    assert p.dimension == "tool" and p.category == "network"
    assert p.tool == "web_search"
    assert p.evidence_count == 3
    assert p.first_seen == NOW - 3 * DAY
    assert p.last_seen == NOW - DAY
    # Guidance is specific and parameterized with the tool name.
    assert "web_search" in p.guidance
    assert "{tool}" not in p.guidance and "{tool}" not in p.trigger


def test_same_session_repeats_do_not_create_pattern():
    # One broken cron job repeating is not evidence diversity.
    for i in range(3):
        eb.append_entry(_entry(session_id="same", ts=NOW - (i + 1) * DAY))

    stats = dist.run_distillation(now=NOW)

    assert stats["evidence_entries"] == 1
    assert stats["clusters"] == 1
    assert stats["patterns_written"] == 0
    assert eb.load_patterns() == []


def test_low_confidence_and_unknown_success_are_not_evidence():
    _seed_diverse(3, confidence="low")
    _seed_diverse(3, success=None)  # unknown outcome, high confidence
    _seed_diverse(3, success=True)

    stats = dist.run_distillation(now=NOW)

    assert stats["entries_seen"] == 9
    assert stats["evidence_entries"] == 0
    assert stats["patterns_written"] == 0
    assert eb.load_patterns() == []


def test_old_failures_outside_window_are_not_evidence():
    _seed_diverse(3, ts=NOW - 100 * DAY)

    stats = dist.run_distillation(now=NOW)

    assert stats["evidence_entries"] == 0
    assert stats["patterns_written"] == 0


def test_honest_null_entries_are_unattributed_not_patterns():
    _seed_diverse(3, primary_dimension=None, failure_category=None)
    _seed_diverse(2, failure_category=None)  # partial attribution also null

    stats = dist.run_distillation(now=NOW)

    assert stats["evidence_entries"] == 3
    assert stats["unattributed"] == 3
    assert stats["clusters"] == 0
    assert stats["patterns_written"] == 0


def test_only_latest_session_revision_counts_as_evidence():
    """Successful resumed revisions replace earlier failures as evidence.

    Успішні пізніші редакції замінюють ранні помилки у наборі доказів.
    """
    for i in range(3):
        eb.append_entry(
            _entry(
                session_id=f"s{i}",
                ts=NOW - 3 * DAY,
                stats={"source_last_message_id": 10 + i},
            )
        )
        eb.append_entry(
            _entry(
                session_id=f"s{i}",
                ts=NOW - DAY,
                success=True,
                stats={"source_last_message_id": 20 + i},
            )
        )

    stats = dist.run_distillation(now=NOW)

    assert stats["entries_seen"] == 6
    assert stats["evidence_entries"] == 0
    assert stats["patterns_written"] == 0
    assert eb.load_patterns() == []
    # Compaction operates on the raw log, so all in-window revisions survive.
    # Ущільнення працює із сирим журналом, тому актуальні редакції зберігаються.
    assert len(list(eb.iter_entries())) == 6


def test_combo_without_template_emits_no_pattern():
    # resource_limit -> generation is an ambiguous attribution with no template.
    _seed_diverse(3, primary_dimension="generation", failure_category="resource_limit", tool=None)

    stats = dist.run_distillation(now=NOW)

    assert stats["clusters"] == 1
    assert stats["no_template"] == 1
    assert stats["patterns_written"] == 0
    assert eb.load_patterns() == []


def test_tool_scoped_template_without_tool_emits_no_pattern():
    # (tool, network) guidance names {tool}; a tool-less cluster can't fill it.
    _seed_diverse(3, tool=None)

    stats = dist.run_distillation(now=NOW)

    assert stats["clusters"] == 1
    assert stats["no_template"] == 1
    assert stats["patterns_written"] == 0


def test_tool_less_template_produces_tool_less_pattern():
    _seed_diverse(3, primary_dimension="context", failure_category="not_found", tool=None)

    stats = dist.run_distillation(now=NOW)

    assert stats["patterns_written"] == 1
    (p,) = eb.load_patterns()
    assert p.id == eb.pattern_id("context", "not_found")
    assert p.tool is None
    assert p.guidance  # real text, not a placeholder


# ---------------------------------------------------------------------------
# Merge + staleness
# ---------------------------------------------------------------------------

def test_merge_preserves_first_seen_and_refreshes_rest():
    pid = eb.pattern_id("tool", "network", "web_search")
    eb.save_patterns(
        [
            ExperiencePattern(
                id=pid,
                dimension="tool",
                category="network",
                tool="web_search",
                trigger="old trigger",
                guidance="old guidance",
                evidence_count=3,
                first_seen=NOW - 60 * DAY,
                last_seen=NOW - 40 * DAY,
            )
        ]
    )
    _seed_diverse(3)

    stats = dist.run_distillation(now=NOW)

    assert stats["patterns_written"] == 1
    (p,) = eb.load_patterns()
    assert p.first_seen == NOW - 60 * DAY  # preserved
    assert p.last_seen == NOW - DAY  # newest evidence
    assert p.evidence_count == 3  # qualifying-entry count
    assert p.guidance != "old guidance" and "web_search" in p.guidance


def test_stale_pattern_dropped_fresh_untouched_pattern_kept():
    eb.save_patterns(
        [
            ExperiencePattern(
                id="tool-permission-terminal",
                dimension="tool",
                category="permission",
                tool="terminal",
                trigger="t",
                guidance="g",
                evidence_count=3,
                first_seen=NOW - 50 * DAY,
                last_seen=NOW - 31 * DAY,  # older than 30d -> stale
            ),
            ExperiencePattern(
                id="context-not_found",
                dimension="context",
                category="not_found",
                trigger="t",
                guidance="g",
                evidence_count=4,
                first_seen=NOW - 20 * DAY,
                last_seen=NOW - 10 * DAY,  # within 30d -> survives
            ),
        ]
    )

    stats = dist.run_distillation(now=NOW)

    assert stats["patterns_dropped"] == 1
    assert stats["patterns_written"] == 1
    (p,) = eb.load_patterns()
    assert p.id == "context-not_found"


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------

def test_compaction_prunes_old_entries_and_keeps_valid_jsonl():
    eb.append_entry(_entry(session_id="old1", ts=NOW - 100 * DAY, success=True))
    eb.append_entry(_entry(session_id="old2", ts=NOW - 91 * DAY, success=True))
    eb.append_entry(_entry(session_id="edge", ts=NOW - 90 * DAY, success=True))
    _seed_diverse(3)

    stats = dist.run_distillation(now=NOW)

    assert stats["entries_seen"] == 6
    assert stats["entries_pruned"] == 2

    remaining = list(eb.iter_entries())
    assert [e.session_id for e in remaining] == ["edge", "s0", "s1", "s2"]
    # File is still valid JSONL, one compact object per line.
    lines = eb.entries_path().read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    for line in lines:
        assert isinstance(json.loads(line), dict)


def test_missing_entries_file_is_tolerated():
    stats = dist.run_distillation(now=NOW)

    assert stats["entries_seen"] == 0
    assert stats["entries_pruned"] == 0
    assert stats["patterns_written"] == 0


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------

def test_locked_run_skips_and_writes_nothing():
    _seed_diverse(3)
    entries_before = eb.entries_path().read_bytes()

    with eb.experience_lock() as acquired:
        assert acquired is True
        stats = dist.run_distillation(now=NOW)

    assert stats["skipped"] == "locked"
    assert not eb.patterns_path().exists()
    assert eb.entries_path().read_bytes() == entries_before


def test_lock_released_after_run():
    _seed_diverse(3)
    stats = dist.run_distillation(now=NOW)
    assert "skipped" not in stats

    # The lock is free again: a second party can take it immediately.
    # Блокування знову вільне: інший процес може одразу його отримати.
    with eb.experience_lock() as acquired:
        assert acquired is True


def test_pattern_write_failure_is_reported(monkeypatch):
    _seed_diverse(3)
    monkeypatch.setattr(dist, "save_patterns", lambda _patterns: False)
    stats = dist.run_distillation(now=NOW)
    assert stats["patterns_written"] == 0
    assert stats["write_failures"] == 1


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_two_runs_are_byte_identical():
    _seed_diverse(3)
    _seed_diverse(3, primary_dimension="context", failure_category="not_found", tool=None)
    _seed_diverse(3, primary_dimension="output", failure_category="syntax_error", tool="execute_code")

    first = dist.run_distillation(now=NOW)
    bytes_first = eb.patterns_path().read_bytes()
    entries_first = eb.entries_path().read_bytes()

    second = dist.run_distillation(now=NOW)

    assert second == first
    assert eb.patterns_path().read_bytes() == bytes_first
    assert eb.entries_path().read_bytes() == entries_first
    # Patterns sorted by id in the file.
    ids = [p["id"] for p in json.loads(bytes_first)]
    assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

def test_main_prints_one_json_line(capsys):
    # main() uses the real clock, so seed relative to it.
    import time

    now = time.time()
    for i in range(3):
        eb.append_entry(_entry(session_id=f"s{i}", ts=now - (i + 1) * DAY))

    assert dist.main([]) == 0
    out = capsys.readouterr().out.strip()
    stats = json.loads(out)
    assert stats["patterns_written"] == 1
    assert "skipped" not in stats
