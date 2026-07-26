"""Tests for #1324 — session-normalized failure-rate comparison.

Raw failure counts grow with session volume, producing false ``regressed``
verdicts.  These tests verify:

1. ``introspection_extract.build_digest`` emits ``tool_failure_rates`` and
   ``*_per_session`` fields alongside the raw counts.
2. ``evolution_realized_impact.record_merge`` stores an optional
   ``baseline_failure_rate`` snapshot.
3. ``compare_failure_rate`` classifies per-tool changes as
   ``improved`` / ``flat`` / ``regressed`` / ``no-baseline`` / ``new``
   using the threshold — NOT the raw count.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_realized_impact import (  # noqa: E402
    compare_failure_rate,
    load_ledger,
    record_merge,
)
from introspection_extract import build_digest  # noqa: E402


# ── introspection_extract: per-session normalization ─────────────────────────


def _session(tmp_path, name, lines, *, age_days=0):
    p = tmp_path / f"{name}.jsonl"
    p.write_text("\n".join(json.dumps(o) for o in lines) + "\n", encoding="utf-8")
    if age_days:
        old = time.time() - age_days * 86400
        import os

        os.utime(p, (old, old))
    return p


def _asst(tool, cid):
    return {
        "role": "assistant",
        "tool_calls": [{"id": cid, "function": {"name": tool, "arguments": "{}"}}],
    }


def _tool(cid, content):
    return {"role": "tool", "tool_call_id": cid, "content": content}


def _fail_term(error="error"):
    return json.dumps({"output": "", "exit_code": 1, "error": error})


def _ok_term():
    return json.dumps({"output": "done", "exit_code": 0})


class TestDigestNormalization:
    """#1324 — the digest emits per-session rates alongside raw counts."""

    def test_emits_tool_failure_rates(self, tmp_path):
        _session(
            tmp_path,
            "s1",
            [
                _asst("terminal", "c1"),
                _tool("c1", _fail_term()),
                _asst("terminal", "c2"),
                _tool("c2", _ok_term()),
            ],
        )
        d = build_digest(tmp_path, window_days=7)
        sig = d["signals"]
        assert "tool_failure_rates" in sig
        assert sig["tool_failure_rates"]["terminal"] == round(1 / 1, 4)

    def test_rate_normalizes_across_session_volume(self, tmp_path):
        # 1 failure / 1 session = rate 1.0; 2 failures / 2 sessions = 1.0
        _session(
            tmp_path,
            "s1",
            [_asst("terminal", "c1"), _tool("c1", _fail_term())],
        )
        _session(
            tmp_path,
            "s2",
            [_asst("terminal", "c2"), _tool("c2", _fail_term())],
        )
        d = build_digest(tmp_path, window_days=7)
        rate = d["signals"]["tool_failure_rates"]["terminal"]
        assert rate == round(2 / 2, 4)  # 1.0, not 2

    def test_emits_per_session_fields(self, tmp_path):
        _session(
            tmp_path,
            "s1",
            [_asst("terminal", "c1"), _tool("c1", _fail_term())],
        )
        d = build_digest(tmp_path, window_days=7)
        sig = d["signals"]
        assert "timeouts_per_session" in sig
        assert "refusals_per_session" in sig
        assert "repeated_tool_runs_normalized" in sig

    def test_zero_sessions_no_division_error(self, tmp_path):
        # Empty dir — no sessions scanned. Must not crash.
        d = build_digest(tmp_path, window_days=7)
        assert d["sessions_scanned"] == 0
        assert d["signals"]["tool_failure_rates"] == {}
        assert d["signals"]["timeouts_per_session"] == 0.0


# ── evolution_realized_impact: baseline + comparison ─────────────────────────


class TestRecordMergeBaseline:
    """#1324 — record_merge stores baseline_failure_rate at merge time."""

    def test_stores_baseline_when_provided(self, tmp_path):
        f = tmp_path / "ledger.jsonl"
        record_merge(
            f,
            issue=100,
            merged_at="2026-07-26",
            predicted_impact=0.9,
            target="fix terminal failures",
            baseline_failure_rate={"terminal": 0.12, "read_file": 0.05},
        )
        recs = load_ledger(f)
        assert recs[0]["baseline_failure_rate"] == {
            "terminal": 0.12,
            "read_file": 0.05,
        }

    def test_omits_baseline_when_not_provided(self, tmp_path):
        f = tmp_path / "ledger.jsonl"
        record_merge(
            f,
            issue=100,
            merged_at="2026-07-26",
            predicted_impact=0.9,
            target="fix X",
        )
        recs = load_ledger(f)
        assert "baseline_failure_rate" not in recs[0]


class TestCompareFailureRate:
    """#1324 — verdicts use the normalized rate, not raw count."""

    def test_improved_when_rate_drops(self):
        result = compare_failure_rate(
            baseline={"terminal": 8.0},
            current={"terminal": 5.0},
        )
        assert result["per_tool"]["terminal"]["verdict"] == "improved"
        assert result["any_regressed"] is False

    def test_flat_when_rate_within_threshold(self):
        # delta = (8.8 - 8.0) / 8.0 = 0.10 < 0.20 threshold → flat
        result = compare_failure_rate(
            baseline={"terminal": 8.0},
            current={"terminal": 8.8},
        )
        assert result["per_tool"]["terminal"]["verdict"] == "flat"

    def test_regressed_when_rate_exceeds_threshold(self):
        # delta = (10.0 - 8.0) / 8.0 = 0.25 > 0.20 threshold → regressed
        result = compare_failure_rate(
            baseline={"terminal": 8.0},
            current={"terminal": 10.0},
        )
        assert result["per_tool"]["terminal"]["verdict"] == "regressed"
        assert result["any_regressed"] is True

    def test_false_regression_eliminated(self):
        """The exact case from #1324: raw count rose but rate is flat/improved.

        168 failures / 21 sessions = 8.0/session (baseline)
        298 failures / 42 sessions = 7.095/session (current — slightly improved)

        Old logic: 298 > 168 → regressed (WRONG).
        New logic:  7.095 < 8.0  → flat or improved (CORRECT — NOT regressed).

        The key point: even though the raw count nearly doubled, the
        per-session rate actually dropped slightly, so it must NEVER be
        classified as "regressed".
        """
        result = compare_failure_rate(
            baseline={"tool_call": round(168 / 21, 4)},
            current={"tool_call": round(298 / 42, 4)},
        )
        verdict = result["per_tool"]["tool_call"]["verdict"]
        assert verdict in ("flat", "improved"), (
            f"expected flat or improved, got {verdict} — the false-regression "
            "bug (#1324) would classify this as 'regressed'"
        )
        assert result["any_regressed"] is False

    def test_no_baseline_fallback(self):
        result = compare_failure_rate(
            baseline=None,
            current={"terminal": 0.1},
        )
        assert result["per_tool"]["terminal"]["verdict"] == "no-baseline"
        assert result["any_regressed"] is False

    def test_empty_baseline(self):
        result = compare_failure_rate(
            baseline={},
            current={"terminal": 0.1},
        )
        assert result["per_tool"]["terminal"]["verdict"] == "no-baseline"

    def test_new_tool_not_in_baseline(self):
        result = compare_failure_rate(
            baseline={"terminal": 0.1},
            current={"terminal": 0.1, "new_tool": 0.5},
        )
        assert result["per_tool"]["new_tool"]["verdict"] == "new"

    def test_custom_threshold(self):
        # With threshold 0.5, a 25% increase is flat, not regressed.
        result = compare_failure_rate(
            baseline={"terminal": 8.0},
            current={"terminal": 10.0},
            threshold=0.5,
        )
        assert result["per_tool"]["terminal"]["verdict"] == "flat"

    def test_zero_baseline_rate(self):
        result = compare_failure_rate(
            baseline={"terminal": 0.0},
            current={"terminal": 0.5},
        )
        # Zero baseline → can't compute delta → "improved-or-new"
        assert result["per_tool"]["terminal"]["verdict"] == "improved-or-new"

    def test_multiple_tools_mixed(self):
        result = compare_failure_rate(
            baseline={"terminal": 10.0, "read_file": 5.0, "patch": 3.0},
            current={"terminal": 7.0, "read_file": 6.0, "patch": 3.2},
        )
        assert result["per_tool"]["terminal"]["verdict"] == "improved"
        assert result["per_tool"]["read_file"]["verdict"] == "flat"
        assert result["per_tool"]["patch"]["verdict"] == "flat"
        assert result["any_regressed"] is False
