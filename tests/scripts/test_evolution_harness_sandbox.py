"""Tests for scripts/evolution_harness_sandbox.py (#2614, parent #2525)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_harness_sandbox import (  # noqa: E402
    GateResult,
    apply_diff,
    default_gate_runner,
    validate_code_diff,
)

SURFACE = {
    "retry_count": 10,
    "backoff": {"base_delay_sec": 1.0, "multiplier": 2.0, "max_delay_sec": 60.0},
    "guard_conditions": [],
}
CODE_DIFF = {
    "surface": SURFACE,
    "changes": [{"field": "retry_count", "before": 10, "after": 3, "reason": "cap"}],
    "status": "proposed",
    "requires_human_review": True,
    "auto_apply": False,
}


def _green(_applied):
    return GateResult(True, 0, "all green")


def _red(_applied):
    return GateResult(False, 1, "1 failed")


def test_apply_diff_copies_and_never_mutates():
    original = json.loads(json.dumps(CODE_DIFF))
    out = apply_diff(CODE_DIFF)
    assert out["after"]["retry_count"] == 3
    assert out["before"]["retry_count"] == 10
    assert out["after"]["backoff"] == SURFACE["backoff"]
    assert CODE_DIFF == original


def test_apply_diff_malformed_returns_none():
    assert apply_diff(None) is None
    assert apply_diff({}) is None
    assert apply_diff({"surface": {}, "changes": []}) is None
    assert apply_diff({"surface": {}, "changes": "nope"}) is None


def test_green_gate_validates():
    v = validate_code_diff(CODE_DIFF, gate_runner=_green)
    assert v["status"] == "validated"
    assert v["gate"]["passed"] is True
    assert v["applied"]["after"]["retry_count"] == 3
    assert v["requires_human_review"] is True and v["auto_apply"] is False


def test_red_gate_rejects():
    v = validate_code_diff(CODE_DIFF, gate_runner=_red)
    assert v["status"] == "rejected"
    assert v["gate"]["passed"] is False
    assert "regression" in v["reason"]
    assert v["requires_human_review"] is True and v["auto_apply"] is False


def test_malformed_diff_is_invalid():
    v = validate_code_diff({}, gate_runner=_green)
    assert v["status"] == "invalid" and v["applied"] is None


def test_default_gate_runner_uses_explicit_encoding(tmp_path, monkeypatch):
    """The Windows-footgun fix: decoding must be deterministic on all platforms."""
    import subprocess

    calls = {}

    def fake_run(cmd, **kwargs):
        calls["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = default_gate_runner({}, repo_root=tmp_path)
    assert result.passed is True
    assert calls["kwargs"]["encoding"] == "utf-8"
    assert calls["kwargs"]["errors"] == "replace"
