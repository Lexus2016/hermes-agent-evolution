"""Tests for scripts/evolution_trace_holdout.py (#3226 first increment)."""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_trace_holdout import evaluate_holdout, _cluster_key  # noqa: E402

_SCRIPT = str(
    Path(__file__).resolve().parents[2] / "scripts" / "evolution_trace_holdout.py"
)


def _record(kind="tool_failure", tool="terminal", occurrences=10):
    return {
        "kind": kind,
        "tool": tool,
        "occurrences": occurrences,
        "severity": occurrences,
    }


class TestClusterKey:
    def test_tool_failure_key(self):
        assert _cluster_key(_record(tool="terminal")) == "tool_failure:terminal"

    def test_provider_error_uses_signature(self):
        r = {"kind": "provider_error", "signature": "429:rate_limit", "severity": 5}
        assert _cluster_key(r) == "provider_error:429:rate_limit"


class TestEvaluateHoldout:
    def test_passes_when_train_evidence_clears_threshold(self):
        # 10 occurrences -> 8 train, threshold = 4.0 (5 * 0.8), passes.
        r = evaluate_holdout([_record(occurrences=10)], total_sessions=10)
        assert r["generalized"] == 1 and r["not_generalized"] == 0
        assert r["clusters"][0]["generalized"] is True

    def test_fails_when_cluster_too_sparse(self):
        # 4 occurrences -> 3.2 train, below threshold 4.0, fails.
        r = evaluate_holdout([_record(occurrences=4)], total_sessions=10)
        assert r["not_generalized"] == 1
        assert r["clusters"][0]["generalized"] is False

    def test_counts_match(self):
        r = evaluate_holdout(
            [_record(tool="a"), _record(tool="b", occurrences=100)], total_sessions=10
        )
        assert r["total_clusters"] == 2
        assert r["generalized"] + r["not_generalized"] == 2

    def test_total_sessions_is_context_only(self):
        # Verdict depends only on occurrences, not total_sessions.
        r1 = evaluate_holdout([_record(occurrences=10)], total_sessions=2)
        r2 = evaluate_holdout([_record(occurrences=10)], total_sessions=1000)
        assert r1["clusters"][0]["generalized"] == r2["clusters"][0]["generalized"]

    def test_cli_reads_stdin(self):
        payload = {"sessions_scanned": 10, "weaknesses": [_record(occurrences=10)]}
        proc = subprocess.run(
            [sys.executable, _SCRIPT],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0
        out = json.loads(proc.stdout)
        assert out["total_clusters"] == 1 and out["generalized"] == 1

    def test_cli_invalid_json_exits_nonzero(self):
        proc = subprocess.run(
            [sys.executable, _SCRIPT],
            input="not-json",
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 2
