"""Tests for scripts/evolution_harness_validator.py (#3227)."""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_harness_validator import score_candidate  # noqa: E402

_SCRIPT = str(
    Path(__file__).resolve().parents[2] / "scripts" / "evolution_harness_validator.py"
)


def _candidate(key="tool_failure:terminal", occurrences=10, sessions=5):
    return {"key": key, "occurrences": occurrences, "sessions": sessions}


def _batch(key="tool_failure:terminal", sessions=4):
    return [{"key": key, "sessions": sessions}]


class TestScoreCandidate:
    def test_selects_generalized_corroborated_candidate(self):
        v = score_candidate(_candidate(), _batch())
        assert v["verdict"] == "select"
        assert v["own_generalized"] is True
        assert v["batch_sessions"] == 4

    def test_rejects_sparse_own_evidence(self):
        # 4 occurrences -> 3.2 train, below threshold 4.0 -> not generalized.
        v = score_candidate(_candidate(occurrences=4), _batch())
        assert v["verdict"] == "reject"
        assert v["own_generalized"] is False

    def test_rejects_uncorroborated_candidate(self):
        # Own evidence generalizes, but the held-out batch has too few sessions.
        v = score_candidate(_candidate(), _batch(sessions=1), min_sessions=3)
        assert v["verdict"] == "reject"
        assert v["own_generalized"] is True
        assert v["batch_sessions"] == 1

    def test_rejects_when_batch_misses_key(self):
        v = score_candidate(_candidate(), _batch(key="other:cluster"))
        assert v["verdict"] == "reject"
        assert v["batch_sessions"] == 0

    def test_cli_reads_stdin(self):
        payload = {
            "candidates": [_candidate()],
            "holdout_batch": _batch(),
        }
        proc = subprocess.run(
            [sys.executable, _SCRIPT],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0
        out = json.loads(proc.stdout)
        assert out["verdicts"][0]["verdict"] == "select"

    def test_cli_invalid_json_exits_nonzero(self):
        proc = subprocess.run(
            [sys.executable, _SCRIPT],
            input="not-json",
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 2
