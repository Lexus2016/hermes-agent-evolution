"""Tests for scripts/evolution_harness_code_diff.py (#2613, parent #2525)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_harness_code_diff import RETRY_SPIRAL_CAP, build_retry_policy_diff  # noqa: E402

SURFACE = {"retry_count": 10,
           "backoff": {"base_delay_sec": 1.0, "multiplier": 2.0, "max_delay_sec": 60.0},
           "guard_conditions": []}


def _spiral(n=15):
    return {"kind": "retry_spiral", "tool": "browser_navigate",
            "max_consecutive": n, "occurrences": n, "severity": n}


def _provider_error():
    return {"kind": "provider_error", "signature": "429:rate_limit",
            "occurrences": 60, "severity": 60}


def test_spiral_caps_and_guards():
    d = build_retry_policy_diff(_spiral(), SURFACE)
    fields = {c["field"]: c for c in d["changes"]}
    assert fields["retry_count"]["before"] == 10
    assert fields["retry_count"]["after"] == RETRY_SPIRAL_CAP
    assert "browser_navigate" in fields["guard_conditions"]["after"][0]


def test_provider_error_widens_backoff():
    d = build_retry_policy_diff(_provider_error(), SURFACE)
    fields = {c["field"]: c for c in d["changes"]}
    assert fields["backoff"]["after"]["base_delay_sec"] == 2.0
    assert "non-retryable error class: 429:rate_limit" in fields["guard_conditions"]["after"]


def test_diff_schema_and_human_gating():
    w = _spiral()
    w["raw_trace"] = "secret"
    d = build_retry_policy_diff(w, SURFACE)
    for key in ("surface", "changes", "unified_diff", "evidence", "source",
                "status", "requires_human_review", "auto_apply"):
        assert key in d
    assert d["unified_diff"].startswith("--- retry_policy (current)")
    assert "-retry_count: 10" in d["unified_diff"]
    assert d["status"] == "proposed" and d["requires_human_review"] is True
    assert d["auto_apply"] is False
    assert "raw_trace" not in d["evidence"] and "raw_trace" not in json.dumps(d)


def test_dropped_noop_and_default_surface():
    assert build_retry_policy_diff({"kind": "tool_failure"}) is None
    assert build_retry_policy_diff("nope") is None
    s = {"retry_count": RETRY_SPIRAL_CAP,
         "backoff": {"base_delay_sec": 1.0, "multiplier": 2.0, "max_delay_sec": 60.0},
         "guard_conditions": ["non-retryable after 3 consecutive attempts "
                              "for `browser_navigate`"]}
    assert build_retry_policy_diff(_spiral(), s) is None
    d = build_retry_policy_diff(_provider_error())
    assert d["surface"]["retry_count"] == 3 and d["surface"]["backoff"]["multiplier"] == 2.0
