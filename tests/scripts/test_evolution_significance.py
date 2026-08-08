"""Tests for GSME significance and sealed-test gates (#1497)."""

import pytest
from scripts.evolution_significance import (
    calculate_paired_z_score,
    check_activation_gate,
    calculate_retention_rate,
)


def test_paired_z_score_significant():
    baseline = [0.1, 0.2, 0.3, 0.2, 0.1, 0.2]
    candidate = [0.8, 0.9, 0.7, 0.8, 0.9, 0.8]
    z, sig = calculate_paired_z_score(baseline, candidate)
    assert sig is True
    assert z >= 1.96


def test_paired_z_score_insignificant():
    baseline = [0.5, 0.5, 0.5, 0.5]
    candidate = [0.51, 0.49, 0.50, 0.52]
    z, sig = calculate_paired_z_score(baseline, candidate)
    assert sig is False
    assert z < 1.96


def test_activation_gate():
    log = "Triggered rule: context_compaction_reinject due to dropped constraint"
    assert check_activation_gate("context_compaction_reinject", log) is True
    assert check_activation_gate("mcp_timeout_retry", log) is False
    assert check_activation_gate("", log) is True


def test_retention_rate():
    res = calculate_retention_rate(0.20, 0.15)
    assert res["retention_rate"] == pytest.approx(0.75)
    assert res["is_phantom_win"] == 0.0

    phantom = calculate_retention_rate(0.20, -0.05)
    assert phantom["is_phantom_win"] == 1.0
