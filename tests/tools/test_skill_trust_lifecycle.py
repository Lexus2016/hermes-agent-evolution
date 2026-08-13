"""Tests for provisional→trusted skill lifecycle (#2256)."""

from tools.skill_usage import (
    _apply_trust_transition,
    get_trust_state,
    DEFAULT_TRUST_PROMOTION_THRESHOLD,
    DEFAULT_TRUST_DEMOTION_FAILURE_RATE,
)


def _prov_rec():
    """A minimal provisional record for testing."""
    return {
        "trust_state": "provisional",
        "consecutive_successes": 0,
        "recent_failure_rate": 0.0,
    }


def _trusted_rec():
    """A minimal trusted record for testing."""
    return {
        "trust_state": "trusted",
        "consecutive_successes": 5,
        "recent_failure_rate": 0.0,
    }


def test_promotion_after_threshold_successes():
    """A provisional skill promotes after N consecutive successes."""
    rec = _prov_rec()
    for _ in range(DEFAULT_TRUST_PROMOTION_THRESHOLD):
        _apply_trust_transition(rec, success=True)
    assert rec["trust_state"] == "trusted"


def test_no_premature_promotion():
    """One success below threshold stays provisional."""
    rec = _prov_rec()
    _apply_trust_transition(rec, success=True)
    assert rec["trust_state"] == "provisional"


def test_failure_resets_counter():
    """A failure resets consecutive_successes to 0 (no demotion if still low rate)."""
    rec = _prov_rec()
    _apply_trust_transition(rec, success=True)
    assert rec["consecutive_successes"] == 1
    rec["recent_failure_rate"] = 0.1  # below demotion threshold
    _apply_trust_transition(rec, success=False)
    assert rec["consecutive_successes"] == 0
    assert rec["trust_state"] == "provisional"


def test_demotion_on_high_failure_rate():
    """A trusted skill with failure_rate >= threshold demotes to provisional."""
    rec = _trusted_rec()
    rec["recent_failure_rate"] = DEFAULT_TRUST_DEMOTION_FAILURE_RATE
    _apply_trust_transition(rec, success=False)
    assert rec["trust_state"] == "provisional"
    assert rec["consecutive_successes"] == 0


def test_no_demotion_below_threshold():
    """A trusted skill with failure_rate < threshold stays trusted."""
    rec = _trusted_rec()
    rec["recent_failure_rate"] = 0.1  # below 0.5 threshold
    _apply_trust_transition(rec, success=False)
    assert rec["trust_state"] == "trusted"


def test_get_trust_state_default_trusted():
    """Pre-existing skills without the field default to trusted (no mass-demotion)."""
    assert get_trust_state("nonexistent-skill-xyz") == "trusted"


def test_get_trust_state_reads_record(monkeypatch):
    """get_trust_state reads the persisted trust_state field."""
    import tools.skill_usage as su

    monkeypatch.setattr(su, "load_usage", lambda: {"test-skill": {"trust_state": "provisional"}})
    assert su.get_trust_state("test-skill") == "provisional"

    monkeypatch.setattr(su, "load_usage", lambda: {"test-skill": {"trust_state": "trusted"}})
    assert su.get_trust_state("test-skill") == "trusted"
