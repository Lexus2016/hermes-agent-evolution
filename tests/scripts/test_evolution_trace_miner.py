"""Tests for scripts/evolution_trace_miner.py (#248 first increment)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_trace_miner import format_weaknesses, mine_weaknesses  # noqa: E402


def _digest(*, tool_failures=None, provider_errors=None, repeated=None, window=7, scanned=10):
    return {
        "window_days": window,
        "sessions_scanned": scanned,
        "signals": {
            "tool_failures": tool_failures or {},
            "provider_errors": provider_errors or {},
            "repeated_tool_runs": repeated or {},
        },
    }


class TestMineWeaknesses:
    def test_threshold_filters_low_counts(self):
        d = _digest(tool_failures={"terminal": 6, "read_file": 2})
        recs = mine_weaknesses(d, min_count=5)
        assert len(recs) == 1
        assert recs[0]["tool"] == "terminal" and recs[0]["occurrences"] == 6

    def test_provider_errors_keyed_by_class(self):
        d = _digest(provider_errors={"429:rate_limit": 60, "400:format_error": 1})
        recs = mine_weaknesses(d, min_count=5)
        assert len(recs) == 1
        r = recs[0]
        assert r["kind"] == "provider_error" and r["signature"] == "429:rate_limit"
        assert r["occurrences"] == 60

    def test_retry_spiral_from_repeated_runs(self):
        d = _digest(repeated={"browser_navigate": {"max_consecutive": 15, "sessions": 3}})
        recs = mine_weaknesses(d, min_count=5)
        assert len(recs) == 1
        r = recs[0]
        assert r["kind"] == "retry_spiral" and r["max_consecutive"] == 15 and r["sessions"] == 3

    def test_sorted_by_severity_desc(self):
        d = _digest(
            tool_failures={"terminal": 6},
            provider_errors={"429:rate_limit": 60},
            repeated={"browser_navigate": {"max_consecutive": 15, "sessions": 3}},
        )
        recs = mine_weaknesses(d, min_count=5)
        sev = [r["severity"] for r in recs]
        assert sev == sorted(sev, reverse=True)
        assert recs[0]["severity"] == 60  # the 429 cluster is worst

    def test_empty_digest_no_weaknesses(self):
        assert mine_weaknesses(_digest(), min_count=5) == []
        assert mine_weaknesses({}, min_count=5) == []

    def test_malformed_repeated_entry_skipped(self):
        d = _digest(repeated={"x": "not-a-dict", "browser": {"max_consecutive": 9, "sessions": 1}})
        recs = mine_weaknesses(d, min_count=5)
        assert len(recs) == 1 and recs[0]["tool"] == "browser"

    def test_no_raw_content_only_counts_and_labels(self):
        # mined records carry only tool names, counts, and our labels.
        d = _digest(tool_failures={"terminal": 9}, provider_errors={"429:rate_limit": 9})
        blob = json.dumps(mine_weaknesses(d, min_count=5))
        assert "rate_limit" in blob  # the class label is fine
        # no free-form/raw fields leak in
        for r in mine_weaknesses(d, min_count=5):
            assert set(r).issubset(
                {"kind", "tool", "occurrences", "severity", "label", "signature",
                 "max_consecutive", "sessions"}
            )


class TestFormat:
    def test_empty(self):
        assert "no recurring weaknesses" in format_weaknesses([], window_days=7)

    def test_lists_records(self):
        recs = mine_weaknesses(_digest(tool_failures={"terminal": 8}), min_count=5)
        out = format_weaknesses(recs, window_days=7)
        assert "1 weakness cluster" in out and "terminal" in out
