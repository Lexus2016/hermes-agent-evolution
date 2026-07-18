# -*- coding: utf-8 -*-
"""Unit tests for recheck suppression (#1041)."""

from __future__ import annotations

from agent.recheck_suppression import (
    CalibrationLog,
    RecheckClassifier,
    RecheckController,
    RecheckResult,
    RecheckSuppressionConfig,
    RecheckVerdict,
)


# --- config -------------------------------------------------------------------


def test_config_defaults_off():
    cfg = RecheckSuppressionConfig()
    assert cfg.enabled is False
    assert 0.0 <= cfg.min_confidence <= 1.0


def test_config_from_mapping():
    cfg = RecheckSuppressionConfig.from_mapping(
        {"enabled": True, "min_confidence": 0.7, "log_capacity": 10}
    )
    assert cfg.enabled is True
    assert cfg.min_confidence == 0.7
    assert cfg.log_capacity == 10


def test_config_from_mapping_clamps_and_defaults():
    assert RecheckSuppressionConfig.from_mapping(None).enabled is False
    cfg = RecheckSuppressionConfig.from_mapping({"min_confidence": 5, "log_capacity": -3})
    assert cfg.min_confidence == 1.0
    assert cfg.log_capacity == 1


# --- classifier ---------------------------------------------------------------


def test_mutating_tool_never_recheck():
    r = RecheckClassifier().classify(
        "write_file", is_idempotent=False, is_immediate_repeat=True, prior_succeeded=True
    )
    assert r.verdict is RecheckVerdict.rethink
    assert r.confidence == 0.0


def test_immediate_repeat_of_successful_idempotent_is_recheck():
    r = RecheckClassifier().classify(
        "read_file", is_idempotent=True, is_immediate_repeat=True, prior_succeeded=True
    )
    assert r.verdict is RecheckVerdict.recheck
    assert r.confidence >= 0.85


def test_non_immediate_repeat_is_rethink():
    r = RecheckClassifier().classify(
        "read_file", is_idempotent=True, is_immediate_repeat=False, prior_succeeded=True
    )
    assert r.verdict is RecheckVerdict.rethink


def test_immediate_repeat_of_failed_call_is_not_suppressed():
    r = RecheckClassifier().classify(
        "read_file", is_idempotent=True, is_immediate_repeat=True, prior_succeeded=False
    )
    assert r.verdict is RecheckVerdict.rethink


# --- controller + calibration log ---------------------------------------------


def test_controller_disabled_by_default():
    assert RecheckController().enabled is False


def test_controller_suppresses_high_confidence_recheck_and_logs():
    ctrl = RecheckController(RecheckSuppressionConfig(enabled=True, min_confidence=0.85))
    suppress, result = ctrl.decide(
        "read_file", is_idempotent=True, is_immediate_repeat=True, prior_succeeded=True
    )
    assert suppress is True
    assert result.verdict is RecheckVerdict.recheck
    assert len(ctrl.calibration_log) == 1
    assert ctrl.calibration_log.suppressed_count == 1
    assert ctrl.calibration_log.entries()[0]["suppressed"] is True


def test_controller_does_not_suppress_below_min_confidence():
    ctrl = RecheckController(RecheckSuppressionConfig(enabled=True, min_confidence=0.95))
    # immediate repeat confidence is 0.9 < 0.95 -> not suppressed, but still logged
    suppress, result = ctrl.decide(
        "read_file", is_idempotent=True, is_immediate_repeat=True, prior_succeeded=True
    )
    assert suppress is False
    assert len(ctrl.calibration_log) == 1
    assert ctrl.calibration_log.suppressed_count == 0


def test_controller_logs_rethink_decisions_too():
    ctrl = RecheckController(RecheckSuppressionConfig(enabled=True))
    ctrl.decide("read_file", is_idempotent=True, is_immediate_repeat=False, prior_succeeded=True)
    assert len(ctrl.calibration_log) == 1
    assert ctrl.calibration_log.suppressed_count == 0


def test_calibration_log_is_bounded():
    log = CalibrationLog(capacity=3)
    for _ in range(10):
        log.record(RecheckResult(RecheckVerdict.recheck, 0.9), suppressed=True)
    assert len(log) == 3


def test_from_mapping_builds_controller():
    ctrl = RecheckController.from_mapping({"enabled": True, "min_confidence": 0.8})
    assert ctrl.enabled is True
    assert ctrl.config.min_confidence == 0.8
