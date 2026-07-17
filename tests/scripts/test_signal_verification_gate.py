"""Tests for the post-merge signal-verification gate (#1140).

Verifies that:
1. signal_verified returns True only for confirmed verdicts.
2. signal_verified returns False for no-signal/regressed/no-verdict.
3. should_close_issue gates closure on the verdict.
4. should_close_issue allows closure for untracked issues.
5. should_close_issue allows closure for matured-unverified issues.
6. should_close_issue blocks closure for regressed signals.
"""

from __future__ import annotations

from scripts.evolution_realized_impact import (
    signal_verified,
    should_close_issue,
    VERDICTS_GOOD,
    VERDICTS_BAD,
)


# ── signal_verified ──────────────────────────────────────────────────────


def test_signal_verified_confirmed():
    """A confirmed verdict means the signal dropped — verified."""
    records = [{"issue": 100, "merged_at": "2026-07-10", "verdict": "confirmed",
                "verified_at": "2026-07-15", "note": "signal dropped"}]
    assert signal_verified(records, 100) is True


def test_signal_verified_no_signal():
    """A no-signal verdict means the fix didn't help — NOT verified."""
    records = [{"issue": 100, "merged_at": "2026-07-10", "verdict": "no-signal",
                "verified_at": "2026-07-15", "note": "flat"}]
    assert signal_verified(records, 100) is False


def test_signal_verified_regressed():
    """A regressed verdict means the signal got worse — NOT verified."""
    records = [{"issue": 100, "merged_at": "2026-07-10", "verdict": "regressed",
                "verified_at": "2026-07-15", "note": "worse"}]
    assert signal_verified(records, 100) is False


def test_signal_verified_no_verdict_yet():
    """No verdict recorded yet — NOT verified (awaiting verification)."""
    records = [{"issue": 100, "merged_at": "2026-07-10", "predicted_impact": 0.8,
                "target": "terminal spirals"}]
    assert signal_verified(records, 100) is False


def test_signal_verified_not_tracked():
    """Issue not in ledger — NOT verified."""
    records = [{"issue": 200, "merged_at": "2026-07-10", "verdict": "confirmed"}]
    assert signal_verified(records, 100) is False


def test_signal_verified_latest_verdict_wins():
    """Multiple verdicts for same issue — latest wins (folded ledger)."""
    records = [
        {"issue": 100, "merged_at": "2026-07-10"},
        {"issue": 100, "verdict": "confirmed", "verified_at": "2026-07-12"},
        {"issue": 100, "verdict": "regressed", "verified_at": "2026-07-15"},
    ]
    # The latest verdict (regressed) should win
    assert signal_verified(records, 100) is False


# ── should_close_issue ──────────────────────────────────────────────────


def test_should_close_confirmed_signal():
    """Confirmed signal drop — should close."""
    records = [{"issue": 100, "merged_at": "2026-07-10", "verdict": "confirmed",
                "verified_at": "2026-07-15"}]
    should, reason = should_close_issue(records, 100, "2026-07-17")
    assert should is True
    assert "confirmed" in reason


def test_should_close_regressed_signal():
    """Regressed signal — should NOT close (keep open for regression)."""
    records = [{"issue": 100, "merged_at": "2026-07-10", "verdict": "regressed",
                "verified_at": "2026-07-15"}]
    should, reason = should_close_issue(records, 100, "2026-07-17")
    assert should is False
    assert "regressed" in reason


def test_should_close_no_signal():
    """No-signal verdict — should NOT close."""
    records = [{"issue": 100, "merged_at": "2026-07-10", "verdict": "no-signal",
                "verified_at": "2026-07-15"}]
    should, reason = should_close_issue(records, 100, "2026-07-17")
    assert should is False
    assert "no-signal" in reason


def test_should_close_not_tracked():
    """Issue not in ledger — should close (nothing to verify)."""
    records = [{"issue": 200, "merged_at": "2026-07-10"}]
    should, reason = should_close_issue(records, 100, "2026-07-17")
    assert should is True
    assert "not tracked" in reason


def test_should_close_awaiting_verification():
    """Merge recent, no verdict yet — should NOT close (awaiting verification)."""
    records = [{"issue": 100, "merged_at": "2026-07-15", "predicted_impact": 0.8}]
    should, reason = should_close_issue(records, 100, "2026-07-17", maturity_days=5)
    assert should is False
    assert "awaiting" in reason.lower()


def test_should_close_matured_unverified():
    """Merge old enough, no verdict — should close (can't wait forever)."""
    records = [{"issue": 100, "merged_at": "2026-07-01", "predicted_impact": 0.8}]
    should, reason = should_close_issue(records, 100, "2026-07-17", maturity_days=5)
    assert should is True
    assert "matured" in reason.lower()