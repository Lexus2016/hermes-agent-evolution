# -*- coding: utf-8 -*-
"""Comprehensive pytest suite for ``recheck_classifier`` (issue #1038).

Covers:
* RecheckVerdict enum values, from_value, round-trip
* RecheckContext construction, defaults, helpers, serialization
* RecheckResult construction, defaults, serialization
* RecheckClassifier basic cases, repeated-call detection, UNCERTAIN paths
* Recency factor computation (now, old, edge deltas)
* Repetition factor computation (0..4+, saturation)
* Confidence scoring boundaries
* Edge cases: empty context, None values, missing keys
* RecheckSuppressionPolicy thresholds, reasons, custom thresholds
* Serialization round-trips for context and result
* Stats tracking and reset

Run::

    cd /Users/admin/.hermes/profiles/user1/evolution && \
    python -m pytest tests/test_recheck_classifier.py --tb=short -q
"""

from __future__ import annotations

import json
import time

import pytest

from lib.recheck_classifier import (
    RecheckVerdict,
    RecheckContext,
    RecheckResult,
    RecheckClassifier,
    RecheckSuppressionPolicy,
    __version__ as module_version,
)


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

class TestVersion:
    def test_version_is_string(self):
        assert isinstance(module_version, str)

    def test_version_format(self):
        parts = module_version.split(".")
        assert len(parts) == 3
        for p in parts:
            assert p.isdigit()


# ---------------------------------------------------------------------------
# RecheckVerdict enum
# ---------------------------------------------------------------------------

class TestRecheckVerdict:
    def test_rethink_value(self):
        assert RecheckVerdict.RETHINK.value == "rethink"

    def test_recheck_value(self):
        assert RecheckVerdict.RECHECK.value == "recheck"

    def test_uncertain_value(self):
        assert RecheckVerdict.UNCERTAIN.value == "uncertain"

    def test_three_members(self):
        assert len(list(RecheckVerdict)) == 3

    def test_from_value_member(self):
        assert RecheckVerdict.from_value(RecheckVerdict.RETHINK) is RecheckVerdict.RETHINK

    def test_from_value_string(self):
        assert RecheckVerdict.from_value("rethink") is RecheckVerdict.RETHINK

    def test_from_value_uppercase(self):
        assert RecheckVerdict.from_value("RECHECK") is RecheckVerdict.RECHECK

    def test_from_value_unknown_raises(self):
        with pytest.raises(ValueError):
            RecheckVerdict.from_value("bogus")

    def test_from_value_int_raises(self):
        with pytest.raises(ValueError):
            RecheckVerdict.from_value(42)

    def test_from_value_none_raises(self):
        with pytest.raises(ValueError):
            RecheckVerdict.from_value(None)


# ---------------------------------------------------------------------------
# RecheckContext
# ---------------------------------------------------------------------------

class TestRecheckContext:
    def test_defaults(self):
        ctx = RecheckContext()
        assert ctx.tool_name == ""
        assert ctx.action == ""
        assert ctx.recent_calls == []
        assert ctx.target_description == ""
        assert ctx.call_count is None

    def test_construction(self):
        ctx = RecheckContext(
            tool_name="read_file",
            action="read /app/main.py",
            recent_calls=[{"tool_name": "read_file", "action": "read /app/main.py", "timestamp": 1000.0}],
            target_description="Check main module",
            call_count=1,
        )
        assert ctx.tool_name == "read_file"
        assert ctx.action == "read /app/main.py"
        assert len(ctx.recent_calls) == 1
        assert ctx.call_count == 1

    def test_matching_calls_finds_identical(self):
        ctx = RecheckContext(
            tool_name="read_file",
            action="read /a.py",
            recent_calls=[
                {"tool_name": "read_file", "action": "read /a.py", "timestamp": 1.0},
                {"tool_name": "terminal", "action": "ls", "timestamp": 2.0},
                {"tool_name": "read_file", "action": "read /a.py", "timestamp": 3.0},
            ],
        )
        matches = ctx.matching_calls()
        assert len(matches) == 2

    def test_matching_calls_empty_when_none(self):
        ctx = RecheckContext(tool_name="read_file", action="read /a.py",
                             recent_calls=[{"tool_name": "terminal", "action": "ls", "timestamp": 1.0}])
        assert ctx.matching_calls() == []

    def test_last_matching_timestamp(self):
        ctx = RecheckContext(
            tool_name="read_file",
            action="read /a.py",
            recent_calls=[
                {"tool_name": "read_file", "action": "read /a.py", "timestamp": 1.0},
                {"tool_name": "read_file", "action": "read /a.py", "timestamp": 5.0},
            ],
        )
        assert ctx.last_matching_timestamp() == 5.0

    def test_last_matching_timestamp_none(self):
        ctx = RecheckContext(tool_name="read_file", action="read /a.py", recent_calls=[])
        assert ctx.last_matching_timestamp() is None

    def test_resolved_call_count_explicit(self):
        ctx = RecheckContext(tool_name="x", action="y", call_count=5)
        assert ctx.resolved_call_count() == 5

    def test_resolved_call_count_inferred(self):
        ctx = RecheckContext(
            tool_name="read_file",
            action="read /a.py",
            recent_calls=[
                {"tool_name": "read_file", "action": "read /a.py", "timestamp": 1.0},
                {"tool_name": "read_file", "action": "read /a.py", "timestamp": 2.0},
            ],
        )
        assert ctx.resolved_call_count() == 2

    def test_has_intervening_true(self):
        ctx = RecheckContext(
            tool_name="read_file",
            action="read /a.py",
            recent_calls=[
                {"tool_name": "read_file", "action": "read /a.py", "timestamp": 1.0},
                {"tool_name": "patch", "action": "edit /a.py", "timestamp": 2.0},
            ],
        )
        assert ctx.has_intervening_different_action() is True

    def test_has_intervening_false(self):
        ctx = RecheckContext(
            tool_name="read_file",
            action="read /a.py",
            recent_calls=[
                {"tool_name": "read_file", "action": "read /a.py", "timestamp": 1.0},
                {"tool_name": "read_file", "action": "read /a.py", "timestamp": 2.0},
            ],
        )
        assert ctx.has_intervening_different_action() is False

    def test_has_intervening_false_when_only_non_matching(self):
        ctx = RecheckContext(
            tool_name="read_file",
            action="read /a.py",
            recent_calls=[{"tool_name": "terminal", "action": "ls", "timestamp": 1.0}],
        )
        assert ctx.has_intervening_different_action() is False

    def test_to_dict(self):
        ctx = RecheckContext(tool_name="t", action="a", target_description="desc", call_count=2)
        d = ctx.to_dict()
        assert d["tool_name"] == "t"
        assert d["action"] == "a"
        assert d["target_description"] == "desc"
        assert d["call_count"] == 2
        assert d["recent_calls"] == []

    def test_from_dict_roundtrip(self):
        original = RecheckContext(
            tool_name="read_file",
            action="read /a.py",
            recent_calls=[{"tool_name": "read_file", "action": "read /a.py", "timestamp": 1.0}],
            target_description="check",
            call_count=1,
        )
        d = original.to_dict()
        restored = RecheckContext.from_dict(d)
        assert restored.tool_name == original.tool_name
        assert restored.action == original.action
        assert restored.recent_calls == original.recent_calls
        assert restored.target_description == original.target_description
        assert restored.call_count == original.call_count

    def test_from_dict_empty(self):
        ctx = RecheckContext.from_dict({})
        assert ctx.tool_name == ""
        assert ctx.action == ""
        assert ctx.recent_calls == []
        assert ctx.call_count is None

    def test_from_dict_none_recent_calls(self):
        ctx = RecheckContext.from_dict({"recent_calls": None})
        assert ctx.recent_calls == []


# ---------------------------------------------------------------------------
# RecheckResult
# ---------------------------------------------------------------------------

class TestRecheckResult:
    def test_defaults(self):
        r = RecheckResult()
        assert r.verdict is RecheckVerdict.UNCERTAIN
        assert r.confidence == 0.0
        assert r.reason == ""
        assert r.context is None

    def test_construction(self):
        ctx = RecheckContext(tool_name="t", action="a")
        r = RecheckResult(verdict=RecheckVerdict.RECHECK, confidence=0.9, reason="dup", context=ctx)
        assert r.verdict is RecheckVerdict.RECHECK
        assert r.confidence == 0.9
        assert r.reason == "dup"
        assert r.context is ctx

    def test_to_dict(self):
        ctx = RecheckContext(tool_name="t", action="a")
        r = RecheckResult(verdict=RecheckVerdict.RETHINK, confidence=0.5, reason="ok", context=ctx)
        d = r.to_dict()
        assert d["verdict"] == "rethink"
        assert d["confidence"] == 0.5
        assert d["reason"] == "ok"
        assert d["context"] is not None

    def test_to_dict_no_context(self):
        r = RecheckResult(verdict=RecheckVerdict.UNCERTAIN, confidence=0.0)
        d = r.to_dict()
        assert d["context"] is None

    def test_from_dict_roundtrip(self):
        ctx = RecheckContext(tool_name="t", action="a", call_count=2)
        original = RecheckResult(verdict=RecheckVerdict.RECHECK, confidence=0.85, reason="dup", context=ctx)
        restored = RecheckResult.from_dict(original.to_dict())
        assert restored.verdict is RecheckVerdict.RECHECK
        assert restored.confidence == 0.85
        assert restored.reason == "dup"
        assert restored.context is not None
        assert restored.context.tool_name == "t"

    def test_from_dict_no_context(self):
        r = RecheckResult.from_dict({"verdict": "rethink", "confidence": 0.3, "reason": "x"})
        assert r.verdict is RecheckVerdict.RETHINK
        assert r.context is None

    def test_json_serializable(self):
        ctx = RecheckContext(tool_name="t", action="a", call_count=2)
        r = RecheckResult(verdict=RecheckVerdict.RECHECK, confidence=0.8, reason="dup", context=ctx)
        s = json.dumps(r.to_dict())
        assert "recheck" in s


# ---------------------------------------------------------------------------
# RecheckClassifier — basic cases
# ---------------------------------------------------------------------------

class TestClassifierBasics:
    def test_uncertain_when_no_history(self):
        clf = RecheckClassifier()
        ctx = RecheckContext(tool_name="read_file", action="read /a.py", recent_calls=[])
        result = clf.classify(ctx)
        assert result.verdict is RecheckVerdict.UNCERTAIN
        assert "No recent call history" in result.reason

    def test_uncertain_when_no_tool_or_action(self):
        clf = RecheckClassifier()
        ctx = RecheckContext()
        result = clf.classify(ctx)
        assert result.verdict is RecheckVerdict.UNCERTAIN
        assert result.confidence == 0.0

    def test_rethink_on_single_prior_call(self):
        clf = RecheckClassifier()
        ctx = RecheckContext(
            tool_name="read_file",
            action="read /a.py",
            recent_calls=[{"tool_name": "read_file", "action": "read /a.py", "timestamp": time.time()}],
        )
        result = clf.classify(ctx)
        assert result.verdict is RecheckVerdict.RETHINK

    def test_recheck_on_repeated_calls_no_intervening(self):
        now = time.time()
        clf = RecheckClassifier()
        ctx = RecheckContext(
            tool_name="read_file",
            action="read /a.py",
            recent_calls=[
                {"tool_name": "read_file", "action": "read /a.py", "timestamp": now - 2},
                {"tool_name": "read_file", "action": "read /a.py", "timestamp": now - 1},
            ],
        )
        result = clf.classify(ctx)
        assert result.verdict is RecheckVerdict.RECHECK

    def test_rethink_when_intervening_action(self):
        now = time.time()
        clf = RecheckClassifier()
        ctx = RecheckContext(
            tool_name="read_file",
            action="read /a.py",
            recent_calls=[
                {"tool_name": "read_file", "action": "read /a.py", "timestamp": now - 3},
                {"tool_name": "patch", "action": "edit /a.py", "timestamp": now - 2},
                {"tool_name": "read_file", "action": "read /a.py", "timestamp": now - 1},
            ],
        )
        result = clf.classify(ctx)
        assert result.verdict is RecheckVerdict.RETHINK
        assert "justified" in result.reason


# ---------------------------------------------------------------------------
# RecheckClassifier — repeated-call detection
# ---------------------------------------------------------------------------

class TestRepeatedCallDetection:
    def test_two_identical_calls_flagged_recheck(self):
        now = time.time()
        clf = RecheckClassifier()
        ctx = RecheckContext(
            tool_name="t",
            action="a",
            recent_calls=[
                {"tool_name": "t", "action": "a", "timestamp": now - 1},
                {"tool_name": "t", "action": "a", "timestamp": now},
            ],
        )
        assert clf.classify(ctx).verdict is RecheckVerdict.RECHECK

    def test_three_identical_calls_higher_confidence(self):
        now = time.time()
        clf = RecheckClassifier()
        ctx2 = RecheckContext(
            tool_name="t",
            action="a",
            recent_calls=[
                {"tool_name": "t", "action": "a", "timestamp": now - 2},
                {"tool_name": "t", "action": "a", "timestamp": now - 1},
            ],
        )
        ctx3 = RecheckContext(
            tool_name="t",
            action="a",
            recent_calls=[
                {"tool_name": "t", "action": "a", "timestamp": now - 3},
                {"tool_name": "t", "action": "a", "timestamp": now - 2},
                {"tool_name": "t", "action": "a", "timestamp": now - 1},
            ],
        )
        r2 = clf.classify(ctx2)
        clf.reset_stats()
        r3 = clf.classify(ctx3)
        assert r3.confidence >= r2.confidence

    def test_different_action_not_flagged(self):
        now = time.time()
        clf = RecheckClassifier()
        ctx = RecheckContext(
            tool_name="read_file",
            action="read /a.py",
            recent_calls=[
                {"tool_name": "read_file", "action": "read /b.py", "timestamp": now},
                {"tool_name": "read_file", "action": "read /b.py", "timestamp": now + 1},
            ],
        )
        result = clf.classify(ctx)
        assert result.verdict is not RecheckVerdict.RECHECK

    def test_different_tool_not_flagged(self):
        now = time.time()
        clf = RecheckClassifier()
        ctx = RecheckContext(
            tool_name="read_file",
            action="a",
            recent_calls=[
                {"tool_name": "terminal", "action": "a", "timestamp": now},
                {"tool_name": "terminal", "action": "a", "timestamp": now + 1},
            ],
        )
        result = clf.classify(ctx)
        # No matching calls but history exists → count 0 → RETHINK (not RECHECK)
        assert result.verdict is RecheckVerdict.RETHINK

    def test_explicit_call_count_override(self):
        now = time.time()
        clf = RecheckClassifier()
        ctx = RecheckContext(
            tool_name="read_file",
            action="read /a.py",
            recent_calls=[
                {"tool_name": "read_file", "action": "read /a.py", "timestamp": now},
            ],
            call_count=3,
        )
        result = clf.classify(ctx)
        assert result.verdict is RecheckVerdict.RECHECK


# ---------------------------------------------------------------------------
# Recency factor
# ---------------------------------------------------------------------------

class TestRecencyFactor:
    def test_zero_when_no_prior_call(self):
        clf = RecheckClassifier()
        ctx = RecheckContext(tool_name="t", action="a", recent_calls=[])
        assert clf._compute_recency_factor(ctx) == 0.0

    def test_one_when_just_now(self):
        clf = RecheckClassifier()
        ctx = RecheckContext(
            tool_name="t",
            action="a",
            recent_calls=[{"tool_name": "t", "action": "a", "timestamp": time.time()}],
        )
        assert clf._compute_recency_factor(ctx) == pytest.approx(1.0)

    def test_zero_when_beyond_window(self):
        clf = RecheckClassifier(recency_window=10.0)
        ctx = RecheckContext(
            tool_name="t",
            action="a",
            recent_calls=[{"tool_name": "t", "action": "a", "timestamp": time.time() - 100}],
        )
        assert clf._compute_recency_factor(ctx) == 0.0

    def test_midpoint(self):
        clf = RecheckClassifier(recency_window=100.0)
        ctx = RecheckContext(
            tool_name="t",
            action="a",
            recent_calls=[{"tool_name": "t", "action": "a", "timestamp": time.time() - 50}],
        )
        assert clf._compute_recency_factor(ctx) == pytest.approx(0.5, abs=0.05)

    def test_future_timestamp_clamps_to_one(self):
        clf = RecheckClassifier()
        ctx = RecheckContext(
            tool_name="t",
            action="a",
            recent_calls=[{"tool_name": "t", "action": "a", "timestamp": time.time() + 999}],
        )
        assert clf._compute_recency_factor(ctx) == 1.0

    def test_custom_recency_window(self):
        clf = RecheckClassifier(recency_window=5.0)
        ctx = RecheckContext(
            tool_name="t",
            action="a",
            recent_calls=[{"tool_name": "t", "action": "a", "timestamp": time.time() - 3}],
        )
        factor = clf._compute_recency_factor(ctx)
        assert 0.0 < factor < 1.0


# ---------------------------------------------------------------------------
# Repetition factor
# ---------------------------------------------------------------------------

class TestRepetitionFactor:
    def test_zero_when_no_calls(self):
        ctx = RecheckContext(tool_name="t", action="a")
        assert RecheckClassifier._compute_repetition_factor(ctx) == 0.0

    def test_one_call(self):
        ctx = RecheckContext(tool_name="t", action="a", call_count=1)
        assert RecheckClassifier._compute_repetition_factor(ctx) == pytest.approx(0.25)

    def test_two_calls(self):
        ctx = RecheckContext(tool_name="t", action="a", call_count=2)
        assert RecheckClassifier._compute_repetition_factor(ctx) == pytest.approx(0.5)

    def test_four_calls_saturates(self):
        ctx = RecheckContext(tool_name="t", action="a", call_count=4)
        assert RecheckClassifier._compute_repetition_factor(ctx) == pytest.approx(1.0)

    def test_eight_calls_clamped(self):
        ctx = RecheckContext(tool_name="t", action="a", call_count=8)
        assert RecheckClassifier._compute_repetition_factor(ctx) == 1.0

    def test_inferred_from_recent_calls(self):
        ctx = RecheckContext(
            tool_name="t",
            action="a",
            recent_calls=[
                {"tool_name": "t", "action": "a", "timestamp": 1.0},
                {"tool_name": "t", "action": "a", "timestamp": 2.0},
            ],
        )
        assert RecheckClassifier._compute_repetition_factor(ctx) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------

class TestConfidenceScoring:
    def test_confidence_in_range_recheck(self):
        now = time.time()
        clf = RecheckClassifier()
        ctx = RecheckContext(
            tool_name="t",
            action="a",
            recent_calls=[
                {"tool_name": "t", "action": "a", "timestamp": now - 1},
                {"tool_name": "t", "action": "a", "timestamp": now},
            ],
        )
        c = clf.classify(ctx).confidence
        assert 0.0 <= c <= 1.0

    def test_confidence_in_range_rethink(self):
        clf = RecheckClassifier()
        ctx = RecheckContext(
            tool_name="t",
            action="a",
            recent_calls=[{"tool_name": "t", "action": "a", "timestamp": time.time()}],
        )
        c = clf.classify(ctx).confidence
        assert 0.0 <= c <= 1.0

    def test_confidence_in_range_uncertain(self):
        clf = RecheckClassifier()
        ctx = RecheckContext(tool_name="t", action="a", recent_calls=[])
        c = clf.classify(ctx).confidence
        assert 0.0 <= c <= 1.0

    def test_more_repetition_raises_confidence(self):
        now = time.time()
        clf = RecheckClassifier()
        low = clf.classify(RecheckContext(
            tool_name="t", action="a",
            recent_calls=[
                {"tool_name": "t", "action": "a", "timestamp": now - 1},
                {"tool_name": "t", "action": "a", "timestamp": now},
            ],
        ))
        clf.reset_stats()
        high = clf.classify(RecheckContext(
            tool_name="t", action="a",
            recent_calls=[
                {"tool_name": "t", "action": "a", "timestamp": now - 3},
                {"tool_name": "t", "action": "a", "timestamp": now - 2},
                {"tool_name": "t", "action": "a", "timestamp": now - 1},
                {"tool_name": "t", "action": "a", "timestamp": now},
            ],
        ))
        assert high.confidence >= low.confidence

    def test_rounded_to_four_decimals(self):
        now = time.time()
        clf = RecheckClassifier()
        ctx = RecheckContext(
            tool_name="t", action="a",
            recent_calls=[
                {"tool_name": "t", "action": "a", "timestamp": now - 1},
                {"tool_name": "t", "action": "a", "timestamp": now},
            ],
        )
        c = clf.classify(ctx).confidence
        assert round(c, 4) == c


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_context(self):
        clf = RecheckClassifier()
        ctx = RecheckContext()
        result = clf.classify(ctx)
        assert result.verdict is RecheckVerdict.UNCERTAIN

    def test_none_call_count_uses_inference(self):
        now = time.time()
        clf = RecheckClassifier()
        ctx = RecheckContext(
            tool_name="t", action="a", call_count=None,
            recent_calls=[
                {"tool_name": "t", "action": "a", "timestamp": now - 1},
                {"tool_name": "t", "action": "a", "timestamp": now},
            ],
        )
        assert clf.classify(ctx).verdict is RecheckVerdict.RECHECK

    def test_missing_timestamp_in_call(self):
        clf = RecheckClassifier()
        ctx = RecheckContext(
            tool_name="t", action="a",
            recent_calls=[
                {"tool_name": "t", "action": "a"},
                {"tool_name": "t", "action": "a"},
            ],
        )
        # Should still classify as RECHECK based on count; recency factor just 0
        result = clf.classify(ctx)
        assert result.verdict is RecheckVerdict.RECHECK

    def test_empty_action_string_with_calls(self):
        now = time.time()
        clf = RecheckClassifier()
        ctx = RecheckContext(
            tool_name="t", action="",
            recent_calls=[{"tool_name": "t", "action": "", "timestamp": now}],
        )
        result = clf.classify(ctx)
        # Single call → RETHINK
        assert result.verdict is RecheckVerdict.RETHINK

    def test_only_tool_name_no_action(self):
        clf = RecheckClassifier()
        ctx = RecheckContext(tool_name="t", action="", recent_calls=[])
        result = clf.classify(ctx)
        assert result.verdict is RecheckVerdict.UNCERTAIN

    def test_recent_calls_missing_action_key(self):
        now = time.time()
        clf = RecheckClassifier()
        ctx = RecheckContext(
            tool_name="t", action="a",
            recent_calls=[{"tool_name": "t", "timestamp": now}],
        )
        # No match (action missing in history entry) → not RECHECK
        result = clf.classify(ctx)
        assert result.verdict is not RecheckVerdict.RECHECK

    def test_recent_calls_missing_tool_key(self):
        now = time.time()
        clf = RecheckClassifier()
        ctx = RecheckContext(
            tool_name="t", action="a",
            recent_calls=[{"action": "a", "timestamp": now}],
        )
        result = clf.classify(ctx)
        assert result.verdict is not RecheckVerdict.RECHECK


# ---------------------------------------------------------------------------
# RecheckSuppressionPolicy
# ---------------------------------------------------------------------------

class TestSuppressionPolicy:
    def test_default_threshold(self):
        pol = RecheckSuppressionPolicy()
        assert pol.threshold == 0.7

    def test_custom_threshold(self):
        pol = RecheckSuppressionPolicy(threshold=0.9)
        assert pol.threshold == 0.9

    def test_suppress_high_confidence_recheck(self):
        pol = RecheckSuppressionPolicy(threshold=0.5)
        r = RecheckResult(verdict=RecheckVerdict.RECHECK, confidence=0.9)
        assert pol.should_suppress(r) is True

    def test_no_suppress_low_confidence_recheck(self):
        pol = RecheckSuppressionPolicy(threshold=0.8)
        r = RecheckResult(verdict=RecheckVerdict.RECHECK, confidence=0.5)
        assert pol.should_suppress(r) is False

    def test_no_suppress_rethink(self):
        pol = RecheckSuppressionPolicy()
        r = RecheckResult(verdict=RecheckVerdict.RETHINK, confidence=0.99)
        assert pol.should_suppress(r) is False

    def test_no_suppress_uncertain(self):
        pol = RecheckSuppressionPolicy()
        r = RecheckResult(verdict=RecheckVerdict.UNCERTAIN, confidence=0.99)
        assert pol.should_suppress(r) is False

    def test_threshold_boundary_equal(self):
        pol = RecheckSuppressionPolicy(threshold=0.7)
        r = RecheckResult(verdict=RecheckVerdict.RECHECK, confidence=0.7)
        assert pol.should_suppress(r) is True

    def test_threshold_just_below(self):
        pol = RecheckSuppressionPolicy(threshold=0.7)
        r = RecheckResult(verdict=RecheckVerdict.RECHECK, confidence=0.69)
        assert pol.should_suppress(r) is False

    def test_per_call_threshold_override(self):
        pol = RecheckSuppressionPolicy(threshold=0.9)
        r = RecheckResult(verdict=RecheckVerdict.RECHECK, confidence=0.6)
        assert pol.should_suppress(r, threshold=0.5) is True

    def test_get_suppression_reason_suppressed(self):
        pol = RecheckSuppressionPolicy(threshold=0.5)
        r = RecheckResult(verdict=RecheckVerdict.RECHECK, confidence=0.8, reason="dup call")
        reason = pol.get_suppression_reason(r)
        assert "Suppressing" in reason

    def test_get_suppression_reason_low_confidence(self):
        pol = RecheckSuppressionPolicy(threshold=0.9)
        r = RecheckResult(verdict=RecheckVerdict.RECHECK, confidence=0.5)
        reason = pol.get_suppression_reason(r)
        assert "confidence" in reason.lower()

    def test_get_suppression_reason_rethink(self):
        pol = RecheckSuppressionPolicy()
        r = RecheckResult(verdict=RecheckVerdict.RETHINK)
        assert "RETHINK" in pol.get_suppression_reason(r)

    def test_get_suppression_reason_uncertain(self):
        pol = RecheckSuppressionPolicy()
        r = RecheckResult(verdict=RecheckVerdict.UNCERTAIN)
        assert "UNCERTAIN" in pol.get_suppression_reason(r)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

class TestSerialization:
    def test_context_roundtrip_json(self):
        original = RecheckContext(
            tool_name="read_file",
            action="read /a.py",
            recent_calls=[{"tool_name": "read_file", "action": "read /a.py", "timestamp": 1.5}],
            target_description="check",
            call_count=2,
        )
        s = json.dumps(original.to_dict())
        restored = RecheckContext.from_dict(json.loads(s))
        assert restored.tool_name == "read_file"
        assert restored.call_count == 2
        assert restored.recent_calls[0]["timestamp"] == 1.5

    def test_result_roundtrip_json(self):
        ctx = RecheckContext(tool_name="t", action="a", call_count=3)
        original = RecheckResult(verdict=RecheckVerdict.RECHECK, confidence=0.88, reason="dup", context=ctx)
        s = json.dumps(original.to_dict())
        restored = RecheckResult.from_dict(json.loads(s))
        assert restored.verdict is RecheckVerdict.RECHECK
        assert restored.confidence == 0.88
        assert restored.context.tool_name == "t"

    def test_result_no_context_roundtrip(self):
        original = RecheckResult(verdict=RecheckVerdict.UNCERTAIN, confidence=0.0)
        restored = RecheckResult.from_dict(original.to_dict())
        assert restored.context is None
        assert restored.verdict is RecheckVerdict.UNCERTAIN

    def test_nested_serialization(self):
        """Full classify → to_dict → from_dict preserves verdict."""
        now = time.time()
        clf = RecheckClassifier()
        ctx = RecheckContext(
            tool_name="t", action="a",
            recent_calls=[
                {"tool_name": "t", "action": "a", "timestamp": now - 1},
                {"tool_name": "t", "action": "a", "timestamp": now},
            ],
        )
        result = clf.classify(ctx)
        d = result.to_dict()
        restored = RecheckResult.from_dict(d)
        assert restored.verdict is result.verdict
        assert restored.confidence == result.confidence


# ---------------------------------------------------------------------------
# Stats tracking
# ---------------------------------------------------------------------------

class TestStats:
    def test_initial_stats(self):
        clf = RecheckClassifier()
        stats = clf.get_stats()
        assert stats["total"] == 0
        assert stats["recheck"] == 0
        assert stats["rethink"] == 0
        assert stats["uncertain"] == 0

    def test_stats_after_classifications(self):
        now = time.time()
        clf = RecheckClassifier()
        # RETHINK
        clf.classify(RecheckContext(
            tool_name="t", action="a",
            recent_calls=[{"tool_name": "t", "action": "a", "timestamp": now}],
        ))
        # RECHECK
        clf.classify(RecheckContext(
            tool_name="t", action="a",
            recent_calls=[
                {"tool_name": "t", "action": "a", "timestamp": now - 1},
                {"tool_name": "t", "action": "a", "timestamp": now},
            ],
        ))
        # UNCERTAIN
        clf.classify(RecheckContext(tool_name="t", action="a", recent_calls=[]))
        stats = clf.get_stats()
        assert stats["total"] == 3
        assert stats["rethink"] == 1
        assert stats["recheck"] == 1
        assert stats["uncertain"] == 1

    def test_reset_stats(self):
        now = time.time()
        clf = RecheckClassifier()
        clf.classify(RecheckContext(
            tool_name="t", action="a",
            recent_calls=[{"tool_name": "t", "action": "a", "timestamp": now}],
        ))
        clf.reset_stats()
        stats = clf.get_stats()
        assert stats["total"] == 0

    def test_uncertain_derived_correctly(self):
        clf = RecheckClassifier()
        clf.classify(RecheckContext())  # UNCERTAIN
        clf.classify(RecheckContext())  # UNCERTAIN
        stats = clf.get_stats()
        assert stats["uncertain"] == 2
        assert stats["total"] == 2


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

class TestIntegration:
    def test_full_workflow_suppress(self):
        """Repeated reads with no intervening action → suppress."""
        now = time.time()
        clf = RecheckClassifier()
        pol = RecheckSuppressionPolicy(threshold=0.5)
        ctx = RecheckContext(
            tool_name="read_file", action="read /app/main.py",
            recent_calls=[
                {"tool_name": "read_file", "action": "read /app/main.py", "timestamp": now - 2},
                {"tool_name": "read_file", "action": "read /app/main.py", "timestamp": now - 1},
            ],
            target_description="verify main module",
        )
        result = clf.classify(ctx)
        assert pol.should_suppress(result) is True

    def test_full_workflow_allow(self):
        """Read after a patch → allow."""
        now = time.time()
        clf = RecheckClassifier()
        pol = RecheckSuppressionPolicy()
        ctx = RecheckContext(
            tool_name="read_file", action="read /app/main.py",
            recent_calls=[
                {"tool_name": "read_file", "action": "read /app/main.py", "timestamp": now - 3},
                {"tool_name": "patch", "action": "edit /app/main.py", "timestamp": now - 2},
                {"tool_name": "read_file", "action": "read /app/main.py", "timestamp": now - 1},
            ],
        )
        result = clf.classify(ctx)
        assert pol.should_suppress(result) is False

    def test_first_ever_call_allowed(self):
        """No history at all → UNCERTAIN, not suppressed."""
        clf = RecheckClassifier()
        pol = RecheckSuppressionPolicy()
        ctx = RecheckContext(tool_name="read_file", action="read /x.py", recent_calls=[])
        result = clf.classify(ctx)
        assert pol.should_suppress(result) is False
