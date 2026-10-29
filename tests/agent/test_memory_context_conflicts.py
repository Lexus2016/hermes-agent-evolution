"""An obsolete recalled note does not reach the prompt beside its replacement.

Retrieval can surface both versions of one fact, and the stale one is worse than an unrelated
note of the same size (arXiv:2610.36308, #3539): on FactConsolidation a bigger evidence budget
lowered accuracy even when the newest statement was provably present. The prompt therefore
drops the older of two conflicting claim bullets before assembly.

Every rule fails open. This module pins both halves: the drop when the age of both versions is
stated, and the keep when it is not - a silently deleted note is worse than a stale one.
"""
from __future__ import annotations

from agent.memory_manager import build_memory_context_block


def _body(block: str) -> str:
    """The provider's own content, without the wrapper and the system note before it."""
    return block.split("]\n\n", 1)[1].rsplit("\n</memory-context>", 1)[0]


def test_a_stale_claim_is_dropped_before_the_fresh_one():
    """The regression fixture for #3539: a stale+fresh pair cannot both reach the prompt."""
    raw = (
        "- deploy target: staging (logged 2026-10-20)\n"
        "- deploy target: production (logged 2026-10-27)\n"
    )

    assert _body(build_memory_context_block(raw)).splitlines() == [
        "- deploy target: production (logged 2026-10-27)"
    ]


def test_the_newer_bullet_wins_regardless_of_position():
    raw = (
        "- deploy target: production (logged 2026-10-27)\n"
        "- deploy target: staging (logged 2026-10-20)\n"
    )

    assert _body(build_memory_context_block(raw)).splitlines() == [
        "- deploy target: production (logged 2026-10-27)"
    ]


def test_three_versions_keep_only_the_newest():
    raw = (
        "- deploy target: staging (logged 2026-10-04)\n"
        "- deploy target: canary-fleet (logged 2026-10-12)\n"
        "- deploy target: production (logged 2026-10-27)\n"
    )

    assert _body(build_memory_context_block(raw)).splitlines() == [
        "- deploy target: production (logged 2026-10-27)"
    ]


def test_without_dates_both_bullets_survive():
    """No age signal, no winner: order is not evidence of recency."""
    raw = "- deploy target: staging\n- deploy target: production\n"

    assert _body(build_memory_context_block(raw)).splitlines() == [
        "- deploy target: staging",
        "- deploy target: production",
    ]


def test_same_date_keeps_both():
    raw = (
        "- deploy target: staging (logged 2026-10-27)\n"
        "- deploy target: production (logged 2026-10-27)\n"
    )

    assert _body(build_memory_context_block(raw)).splitlines() == [
        "- deploy target: staging (logged 2026-10-27)",
        "- deploy target: production (logged 2026-10-27)",
    ]


def test_a_near_duplicate_value_is_not_a_conflict():
    """The values must genuinely differ; a longer phrasing is a duplicate, not a replacement."""
    raw = (
        "- release channel: stable (logged 2026-10-20)\n"
        "- release channel: stable canary (logged 2026-10-21)\n"
    )

    assert _body(build_memory_context_block(raw)).splitlines() == [
        "- release channel: stable (logged 2026-10-20)",
        "- release channel: stable canary (logged 2026-10-21)",
    ]


def test_a_different_topic_is_never_evicted():
    raw = (
        "- deploy target: staging (logged 2026-10-20)\n"
        "- backup target: s3 (logged 2026-10-27)\n"
    )

    assert _body(build_memory_context_block(raw)).splitlines() == [
        "- deploy target: staging (logged 2026-10-20)",
        "- backup target: s3 (logged 2026-10-27)",
    ]


def test_a_bullet_with_provenance_continuation_lines_is_never_dropped():
    """Dropping it would re-parent the continuation's provenance under another entry."""
    raw = (
        "- deploy target: staging\n"
        "  (logged 2026-10-20 by builtin)\n"
        "- deploy target: production (logged 2026-10-27)\n"
    )

    assert _body(build_memory_context_block(raw)).splitlines() == [
        "- deploy target: staging",
        "  (logged 2026-10-20 by builtin)",
        "- deploy target: production (logged 2026-10-27)",
    ]


def test_sections_are_compared_separately():
    """A heading opens a new scope, so "Profile" never evicts "Recently changed"."""
    raw = (
        "Profile:\n"
        "- deploy target: staging (logged 2026-10-20)\n"
        "Recently changed:\n"
        "- deploy target: production (logged 2026-10-27)\n"
    )

    assert _body(build_memory_context_block(raw)) == raw


def test_non_claim_bullets_are_left_alone():
    raw = (
        "- reviewed the deploy notes on 2026-10-20\n"
        "- reviewed the deploy notes on 2026-10-27\n"
    )

    assert _body(build_memory_context_block(raw)) == raw
