# -*- coding: utf-8 -*-
"""Unit tests for Agentic Transaction Slice 1 — transaction envelope (#2762)."""

import pytest

from evolution.lib.agentic_tx import CompensationRegistry, TransactionEnvelope, begin


def test_begin_disabled_by_default_is_noop():
    """Disabled envelope: begin is a no-op, no compensations fire."""
    fired = []
    tx = begin(enabled=False)
    with tx.begin():
        tx.register("k", lambda: fired.append("k"))
    assert fired == []  # nothing fired, nothing rolled back


def test_rollback_fires_compensations_in_reverse_order():
    fired = []
    tx = begin(enabled=True)
    with tx.begin():
        tx.register("a", lambda: fired.append("a"))
        tx.register("b", lambda: fired.append("b"))
        tx.rollback()
    assert fired == ["b", "a"]  # reverse registration order


def test_commit_clears_compensations():
    fired = []
    tx = begin(enabled=True)
    with tx.begin():
        tx.register("a", lambda: fired.append("a"))
        tx.commit()
    assert fired == []  # committed -> no rollback


def test_exception_in_context_rolls_back():
    fired = []
    tx = begin(enabled=True)
    try:
        with tx.begin():
            tx.register("a", lambda: fired.append("a"))
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert fired == ["a"]


def test_compensation_registry_rollback_all():
    fired = []
    reg = CompensationRegistry()
    reg.register("x", lambda: fired.append("x"))
    reg.register("y", lambda: fired.append("y"))
    reg.rollback_all()
    assert fired == ["y", "x"]
    assert len(reg) == 0


def test_one_bad_compensation_does_not_block_others():
    fired = []

    def bad():
        raise RuntimeError("bad")

    reg = CompensationRegistry()
    reg.register("bad", bad)
    reg.register("good", lambda: fired.append("good"))
    reg.rollback_all()
    assert fired == ["good"]  # good still fired despite bad raising


def test_register_is_noop_when_disabled():
    reg = CompensationRegistry()
    tx = TransactionEnvelope(registry=reg, enabled=False)
    tx.register("k", lambda: None)
    assert len(reg) == 0


def test_begin_helper_returns_envelope():
    tx = begin(enabled=True)
    assert tx.enabled is True
    assert isinstance(tx.registry, CompensationRegistry)


# ── Slice 3: compensation for non-rollbackable mutations (#2764) ────────
# A merged PR cannot be un-merged; the compensate-don't-rollback branch
# registers a mitigation action at merge time and invokes it when a LATER
# failure in the same transaction makes the merge harmful.

def test_merged_pr_registers_compensation_invoked_on_later_failure():
    tx = begin(enabled=True)
    events = []

    with pytest.raises(RuntimeError):
        with tx.begin():
            # Mutation 1: a non-rollbackable side effect SUCCEEDS (PR merged).
            events.append("pr-123 merged")
            # Compensate-don't-rollback: register the mitigation NOW.
            tx.register("pr-123", lambda: events.append("revert PR opened"))
            # Mutation 2 (later): post-merge validation fails.
            raise RuntimeError("post-merge validation failed")

    # The merge is not un-merged — the compensation mitigates instead.
    assert events == ["pr-123 merged", "revert PR opened"]


def test_merged_pr_compensation_not_invoked_when_transaction_succeeds():
    tx = begin(enabled=True)
    events = []

    with tx.begin():
        events.append("pr-124 merged")
        tx.register("pr-124", lambda: events.append("revert PR opened"))
        # No later failure — commit clears the compensation silently.

    assert events == ["pr-124 merged"]
