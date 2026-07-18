"""Tests for the post-merge signal-verification gate (#1140).

Covers signal_verified, should_close_issue, and the check-close CLI subcommand
that wires the gate into the close loop (introspection skill).
"""
from __future__ import annotations

import pytest

from scripts.evolution_realized_impact import signal_verified, should_close_issue, main


# ── signal_verified ──────────────────────────────────────────────────────


@pytest.mark.parametrize("verdict,expected", [
    ("confirmed", True),
    ("no-signal", False),
    ("regressed", False),
])
def test_signal_verified_verdicts(verdict, expected):
    recs = [{"issue": 100, "merged_at": "2026-07-10", "verdict": verdict, "verified_at": "2026-07-15"}]
    assert signal_verified(recs, 100) is expected


def test_signal_verified_no_verdict_yet():
    assert signal_verified([{"issue": 100, "merged_at": "2026-07-10"}], 100) is False


def test_signal_verified_not_tracked():
    assert signal_verified([{"issue": 200, "verdict": "confirmed"}], 100) is False


def test_signal_verified_latest_verdict_wins():
    recs = [
        {"issue": 100, "merged_at": "2026-07-10"},
        {"issue": 100, "verdict": "confirmed", "verified_at": "2026-07-12"},
        {"issue": 100, "verdict": "regressed", "verified_at": "2026-07-15"},
    ]
    assert signal_verified(recs, 100) is False


# ── should_close_issue ──────────────────────────────────────────────────


@pytest.mark.parametrize("verdict,should,frag", [
    ("confirmed", True, "confirmed"),
    ("regressed", False, "regressed"),
    ("no-signal", False, "no-signal"),
])
def test_should_close_verdict_gating(verdict, should, frag):
    """confirmed → close; regressed/no-signal → keep open."""
    s, reason = should_close_issue(
        [{"issue": 100, "merged_at": "2026-07-10", "verdict": verdict}], 100, "2026-07-17")
    assert s is should and frag in reason


def test_should_close_not_tracked():
    s, reason = should_close_issue([{"issue": 200}], 100, "2026-07-17")
    assert s is True and "not tracked" in reason


def test_should_close_awaiting_then_matured():
    """Recent merge → awaiting; old merge → matured (close unverified)."""
    s, reason = should_close_issue(
        [{"issue": 100, "merged_at": "2026-07-15"}], 100, "2026-07-17", maturity_days=5)
    assert s is False and "awaiting" in reason.lower()
    s, reason = should_close_issue(
        [{"issue": 100, "merged_at": "2026-07-01"}], 100, "2026-07-17", maturity_days=5)
    assert s is True and "matured" in reason.lower()


# ── check-close CLI subcommand (#1140 wire-in) ──────────────────────────


def _write_ledger(tmp_path, line):
    ledger = tmp_path / "realized" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(line + "\n", encoding="utf-8")


@pytest.mark.parametrize("ledger_line,extra_args,rc_expected,frag", [
    ('{"issue": 100, "merged_at": "2026-07-10", "verdict": "confirmed"}', [], 0, "confirmed"),
    ('{"issue": 100, "merged_at": "2026-07-10", "verdict": "regressed"}', [], 1, "regressed"),
    ('{"issue": 100, "merged_at": "2026-07-15", "predicted_impact": 0.8}', ["--maturity-days", "5"], 1, "awaiting"),
])
def test_check_close_cli(capsys, tmp_path, monkeypatch, ledger_line, extra_args, rc_expected, frag):
    """check-close exits 0 (may stay closed) or 1 (re-open) per the verdict."""
    _write_ledger(tmp_path, ledger_line)
    monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
    rc = main(["prog", "check-close", "100", "--today", "2026-07-17"] + extra_args)
    assert rc == rc_expected
    assert frag in capsys.readouterr().out.lower()