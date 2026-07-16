# -*- coding: utf-8 -*-
"""Comprehensive pytest suite for :mod:`root_cause_diagnosis`.

Covers:

* ``FailureCategory`` enum — all values, ``from_string`` parsing.
* ``Diagnosis`` dataclass — construction, JSON round-trip, defaults.
* ``ErrorClassifier`` — every category's patterns, priority ordering,
  custom patterns, ``count_matches``, empty/None inputs.
* ``RootCauseAnalyzer`` — full flow for each category, root-cause text,
  contributing factors, fix generation per category, confidence scoring,
  context enrichment, metadata.
* ``DiagnosisHistory`` — add/get_recent/get_recurring/clear/stats,
  serialization round-trip, thread safety.
* Edge cases — empty error message, None values, missing context,
  unknown errors, multiple matches.

Run::

    python -m pytest tests/test_root_cause_diagnosis.py -v
"""

from __future__ import annotations

import json
import threading

import pytest

from root_cause_diagnosis import (
    Diagnosis,
    DiagnosisHistory,
    ErrorClassifier,
    FailureCategory,
    RootCauseAnalyzer,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def classifier() -> ErrorClassifier:
    return ErrorClassifier()


@pytest.fixture
def analyzer() -> RootCauseAnalyzer:
    return RootCauseAnalyzer()


@pytest.fixture
def history() -> DiagnosisHistory:
    return DiagnosisHistory()


# ---------------------------------------------------------------------------
# FailureCategory tests
# ---------------------------------------------------------------------------

class TestFailureCategory:
    """Tests for the FailureCategory enum."""

    def test_has_all_expected_values(self):
        values = {fc.value for fc in FailureCategory}
        assert values == {
            "network", "permission", "not_found", "validation",
            "timeout", "resource_limit", "syntax_error", "unknown",
        }

    def test_total_categories(self):
        assert len(list(FailureCategory)) == 8

    def test_from_string_valid(self):
        assert FailureCategory.from_string("network") == FailureCategory.NETWORK
        assert FailureCategory.from_string("TIMEOUT") == FailureCategory.TIMEOUT
        assert FailureCategory.from_string("  permission ") == FailureCategory.PERMISSION

    def test_from_string_all_values(self):
        for fc in FailureCategory:
            assert FailureCategory.from_string(fc.value) == fc

    def test_from_string_invalid_raises(self):
        with pytest.raises(ValueError):
            FailureCategory.from_string("nonexistent")

    def test_from_string_case_insensitive(self):
        assert FailureCategory.from_string("not_found") == FailureCategory.NOT_FOUND
        assert FailureCategory.from_string("NOT_FOUND") == FailureCategory.NOT_FOUND
        assert FailureCategory.from_string("RESOURCE_LIMIT") == FailureCategory.RESOURCE_LIMIT

    def test_unknown_default(self):
        assert FailureCategory.UNKNOWN.value == "unknown"


# ---------------------------------------------------------------------------
# Diagnosis dataclass tests
# ---------------------------------------------------------------------------

class TestDiagnosis:
    """Tests for the Diagnosis dataclass."""

    def test_default_values(self):
        d = Diagnosis()
        assert d.category == FailureCategory.UNKNOWN
        assert d.root_cause == ""
        assert d.contributing_factors == []
        assert d.suggested_fixes == []
        assert d.confidence == 0.0
        assert d.metadata == {}

    def test_to_dict_keys(self):
        d = Diagnosis(
            category=FailureCategory.NETWORK,
            root_cause="network down",
            contributing_factors=["dns"],
            suggested_fixes=["retry"],
            confidence=0.8,
            metadata={"tool": "search"},
        )
        result = d.to_dict()
        assert set(result.keys()) == {
            "category", "root_cause", "contributing_factors",
            "suggested_fixes", "confidence", "metadata",
        }

    def test_to_dict_category_is_string(self):
        d = Diagnosis(category=FailureCategory.TIMEOUT)
        assert d.to_dict()["category"] == "timeout"

    def test_from_dict_round_trip(self):
        d = Diagnosis(
            category=FailureCategory.PERMISSION,
            root_cause="denied",
            contributing_factors=["expired token"],
            suggested_fixes=["refresh"],
            confidence=0.75,
            metadata={"tool": "tool_a"},
        )
        result = Diagnosis.from_dict(d.to_dict())
        assert result == d

    def test_from_dict_missing_fields(self):
        d = Diagnosis.from_dict({"category": "network"})
        assert d.category == FailureCategory.NETWORK
        assert d.root_cause == ""
        assert d.confidence == 0.0

    def test_json_serialization(self):
        d = Diagnosis(
            category=FailureCategory.VALIDATION,
            root_cause="bad input",
            contributing_factors=["missing arg"],
            suggested_fixes=["add arg"],
            confidence=0.9,
            metadata={"key": "value"},
        )
        s = json.dumps(d.to_dict())
        restored = Diagnosis.from_dict(json.loads(s))
        assert restored == d

    def test_lists_are_independent(self):
        d1 = Diagnosis()
        d2 = Diagnosis()
        d1.contributing_factors.append("factor")
        assert d2.contributing_factors == []

    def test_metadata_is_independent(self):
        d1 = Diagnosis()
        d2 = Diagnosis()
        d1.metadata["k"] = "v"
        assert d2.metadata == {}


# ---------------------------------------------------------------------------
# ErrorClassifier tests
# ---------------------------------------------------------------------------

class TestErrorClassifier:
    """Tests for the ErrorClassifier."""

    # -- Timeout ------------------------------------------------------

    def test_classify_timeout(self, classifier):
        assert classifier.classify("Connection timeout") == FailureCategory.TIMEOUT

    def test_classify_timeout_typed_out(self, classifier):
        assert classifier.classify("The operation timed out") == FailureCategory.TIMEOUT

    def test_classify_timeout_deadline(self, classifier):
        assert classifier.classify("deadline exceeded") == FailureCategory.TIMEOUT

    def test_classify_timeout_via_error_type(self, classifier):
        assert classifier.classify("", "TimeoutError") == FailureCategory.TIMEOUT

    # -- Permission ---------------------------------------------------

    def test_classify_permission_denied(self, classifier):
        assert classifier.classify("Permission denied") == FailureCategory.PERMISSION

    def test_classify_permission_403(self, classifier):
        assert classifier.classify("HTTP 403 Forbidden") == FailureCategory.PERMISSION

    def test_classify_permission_unauthorized(self, classifier):
        assert classifier.classify("Unauthorized access") == FailureCategory.PERMISSION

    def test_classify_permission_access_denied(self, classifier):
        assert classifier.classify("Access denied to resource") == FailureCategory.PERMISSION

    def test_classify_permission_forbidden(self, classifier):
        assert classifier.classify("Forbidden") == FailureCategory.PERMISSION

    # -- Not Found ----------------------------------------------------

    def test_classify_not_found(self, classifier):
        assert classifier.classify("Resource not found") == FailureCategory.NOT_FOUND

    def test_classify_not_found_404(self, classifier):
        assert classifier.classify("Error 404 page") == FailureCategory.NOT_FOUND

    def test_classify_not_found_no_such_file(self, classifier):
        assert classifier.classify("No such file or directory") == FailureCategory.NOT_FOUND

    def test_classify_not_found_does_not_exist(self, classifier):
        assert classifier.classify("File does not exist") == FailureCategory.NOT_FOUND

    # -- Resource Limit -----------------------------------------------

    def test_classify_resource_rate_limit(self, classifier):
        assert classifier.classify("Rate limit exceeded") == FailureCategory.RESOURCE_LIMIT

    def test_classify_resource_429(self, classifier):
        assert classifier.classify("HTTP 429 Too Many Requests") == FailureCategory.RESOURCE_LIMIT

    def test_classify_status_code_not_matched_when_embedded(self, classifier):
        # Regression: a status-code pattern must not fire when embedded inside
        # a larger number ("400" inside "4001").
        assert classifier.classify("listening on port 4001") != FailureCategory.VALIDATION

    def test_classify_host_not_matched_in_ghost(self, classifier):
        # Regression: the short "host" network pattern must not fire inside
        # "ghost".
        assert classifier.classify("ghost process terminated") == FailureCategory.UNKNOWN

    def test_classify_resource_quota(self, classifier):
        assert classifier.classify("Quota exceeded for today") == FailureCategory.RESOURCE_LIMIT

    def test_classify_resource_oom(self, classifier):
        assert classifier.classify("Out of memory") == FailureCategory.RESOURCE_LIMIT

    def test_classify_resource_exhausted(self, classifier):
        assert classifier.classify("Resource exhausted") == FailureCategory.RESOURCE_LIMIT

    # -- Network ------------------------------------------------------

    def test_classify_network_connection(self, classifier):
        assert classifier.classify("Connection refused") == FailureCategory.NETWORK

    def test_classify_network_network(self, classifier):
        assert classifier.classify("Network is unreachable") == FailureCategory.NETWORK

    def test_classify_network_dns(self, classifier):
        assert classifier.classify("DNS resolution failed") == FailureCategory.NETWORK

    def test_classify_network_reset(self, classifier):
        assert classifier.classify("Connection reset by peer") == FailureCategory.NETWORK

    # -- Validation ---------------------------------------------------

    def test_classify_validation(self, classifier):
        assert classifier.classify("Validation failed for field") == FailureCategory.VALIDATION

    def test_classify_validation_invalid(self, classifier):
        assert classifier.classify("Invalid argument provided") == FailureCategory.VALIDATION

    def test_classify_validation_schema(self, classifier):
        assert classifier.classify("Schema mismatch") == FailureCategory.VALIDATION

    def test_classify_validation_bad_request(self, classifier):
        assert classifier.classify("400 Bad Request") == FailureCategory.VALIDATION

    def test_classify_validation_unexpected_arg(self, classifier):
        assert classifier.classify("unexpected keyword argument 'foo'") == FailureCategory.VALIDATION

    # -- Syntax Error -------------------------------------------------

    def test_classify_syntax(self, classifier):
        assert classifier.classify("SyntaxError at line 5") == FailureCategory.SYNTAX_ERROR

    def test_classify_syntax_parse_error(self, classifier):
        assert classifier.classify("Parse error in input") == FailureCategory.SYNTAX_ERROR

    def test_classify_syntax_unexpected_token(self, classifier):
        assert classifier.classify("Unexpected token '}'") == FailureCategory.SYNTAX_ERROR

    def test_classify_syntax_indentation(self, classifier):
        assert classifier.classify("IndentationError") == FailureCategory.SYNTAX_ERROR

    # -- Unknown ------------------------------------------------------

    def test_classify_unknown(self, classifier):
        assert classifier.classify("Something completely bizarre happened") == FailureCategory.UNKNOWN

    def test_classify_empty_message(self, classifier):
        assert classifier.classify("") == FailureCategory.UNKNOWN

    def test_classify_empty_both(self, classifier):
        assert classifier.classify("", "") == FailureCategory.UNKNOWN

    def test_classify_none_message(self, classifier):
        assert classifier.classify(None) == FailureCategory.UNKNOWN  # type: ignore[arg-type]

    # -- Priority ordering --------------------------------------------

    def test_timeout_beats_connection(self, classifier):
        """'timeout' should win over generic 'connection'."""
        msg = "Connection timeout"
        assert classifier.classify(msg) == FailureCategory.TIMEOUT

    def test_rate_limit_beats_connection(self, classifier):
        """'429' should win over generic 'connection'."""
        msg = "Connection refused with 429"
        assert classifier.classify(msg) == FailureCategory.RESOURCE_LIMIT

    def test_permission_beats_network(self, classifier):
        """'403' should win over generic 'connection'."""
        msg = "Connection failed: 403 Forbidden"
        assert classifier.classify(msg) == FailureCategory.PERMISSION

    # -- Custom patterns ----------------------------------------------

    def test_custom_patterns_init(self):
        c = ErrorClassifier(
            patterns={FailureCategory.TIMEOUT: ["custom_to"]}
        )
        assert c.classify("custom_to happened") == FailureCategory.TIMEOUT

    def test_add_pattern(self, classifier):
        classifier.add_pattern(FailureCategory.UNKNOWN, "blargh")
        assert classifier.classify("blargh error") == FailureCategory.UNKNOWN

    def test_add_pattern_lowercased(self, classifier):
        classifier.add_pattern(FailureCategory.NETWORK, "MyPattern")
        assert classifier.classify("mypattern detected") == FailureCategory.NETWORK

    def test_add_pattern_no_duplicate(self, classifier):
        before = classifier.get_category_patterns()
        classifier.add_pattern(FailureCategory.TIMEOUT, "timeout")  # already exists
        after = classifier.get_category_patterns()
        assert before[FailureCategory.TIMEOUT] == after[FailureCategory.TIMEOUT]

    # -- get_category_patterns ----------------------------------------

    def test_get_category_patterns_returns_all(self, classifier):
        patterns = classifier.get_category_patterns()
        for cat in FailureCategory:
            if cat != FailureCategory.UNKNOWN:
                assert cat in patterns

    def test_get_category_patterns_unknown_not_in_defaults(self, classifier):
        patterns = classifier.get_category_patterns()
        assert FailureCategory.UNKNOWN not in patterns

    def test_get_category_patterns_returns_copy(self, classifier):
        p1 = classifier.get_category_patterns()
        p1[FailureCategory.TIMEOUT].append("injected")
        p2 = classifier.get_category_patterns()
        assert "injected" not in p2[FailureCategory.TIMEOUT]

    # -- count_matches ------------------------------------------------

    def test_count_matches_single(self, classifier):
        counts = classifier.count_matches("timeout occurred")
        assert counts.get(FailureCategory.TIMEOUT) == 1

    def test_count_matches_multiple_same_category(self, classifier):
        counts = classifier.count_matches("timeout: operation timed out")
        assert counts[FailureCategory.TIMEOUT] == 2

    def test_count_matches_multiple_categories(self, classifier):
        counts = classifier.count_matches("Connection refused and timeout")
        assert FailureCategory.NETWORK in counts
        assert FailureCategory.TIMEOUT in counts

    def test_count_matches_empty(self, classifier):
        assert classifier.count_matches("") == {}

    def test_count_matches_no_match(self, classifier):
        assert classifier.count_matches("totally random text") == {}


# ---------------------------------------------------------------------------
# RootCauseAnalyzer tests
# ---------------------------------------------------------------------------

class TestRootCauseAnalyzer:
    """Tests for the RootCauseAnalyzer."""

    # -- Full flow per category ---------------------------------------

    def test_analyze_network(self, analyzer):
        d = analyzer.analyze("search", "Connection refused", "ConnectionError", {})
        assert d.category == FailureCategory.NETWORK
        assert "network" in d.root_cause.lower()
        assert len(d.suggested_fixes) > 0
        assert d.metadata["tool_name"] == "search"

    def test_analyze_permission(self, analyzer):
        d = analyzer.analyze("read_file", "Permission denied", "PermissionError", {})
        assert d.category == FailureCategory.PERMISSION
        assert "permission" in d.root_cause.lower() or "access" in d.root_cause.lower()

    def test_analyze_not_found(self, analyzer):
        d = analyzer.analyze("read_file", "No such file", "FileNotFoundError", {})
        assert d.category == FailureCategory.NOT_FOUND
        assert "exist" in d.root_cause.lower()

    def test_analyze_validation(self, analyzer):
        d = analyzer.analyze("search", "Invalid argument", "ValueError", {})
        assert d.category == FailureCategory.VALIDATION
        assert "validation" in d.root_cause.lower() or "schema" in d.root_cause.lower()

    def test_analyze_timeout(self, analyzer):
        d = analyzer.analyze("search", "Connection timeout", "TimeoutError", {})
        assert d.category == FailureCategory.TIMEOUT
        assert "time" in d.root_cause.lower()

    def test_analyze_resource_limit(self, analyzer):
        d = analyzer.analyze("search", "Rate limit exceeded", "", {})
        assert d.category == FailureCategory.RESOURCE_LIMIT
        assert "limit" in d.root_cause.lower() or "resource" in d.root_cause.lower()

    def test_analyze_syntax_error(self, analyzer):
        d = analyzer.analyze("terminal", "SyntaxError at line 5", "SyntaxError", {})
        assert d.category == FailureCategory.SYNTAX_ERROR
        assert "syntax" in d.root_cause.lower() or "parse" in d.root_cause.lower()

    def test_analyze_unknown(self, analyzer):
        d = analyzer.analyze("search", "weird unknown issue", "", {})
        assert d.category == FailureCategory.UNKNOWN
        assert "unclassified" in d.root_cause.lower()

    # -- Metadata -----------------------------------------------------

    def test_analyze_metadata_has_tool_name(self, analyzer):
        d = analyzer.analyze("patch", "timeout", "", {})
        assert d.metadata["tool_name"] == "patch"

    def test_analyze_metadata_has_error_type(self, analyzer):
        d = analyzer.analyze("patch", "timeout", "TimeoutError", {})
        assert d.metadata["error_type"] == "TimeoutError"

    def test_analyze_metadata_has_context(self, analyzer):
        ctx = {"retry_count": 2, "endpoint": "http://api"}
        d = analyzer.analyze("search", "timeout", "", ctx)
        assert d.metadata["context"] == ctx

    def test_analyze_metadata_empty_context(self, analyzer):
        d = analyzer.analyze("search", "timeout", "")
        assert d.metadata["context"] == {}

    def test_analyze_metadata_context_not_mutated(self, analyzer):
        ctx = {"retry_count": 1}
        d = analyzer.analyze("search", "timeout", "", ctx)
        d.metadata["context"]["injected"] = True
        assert "injected" not in ctx

    def test_analyze_none_context(self, analyzer):
        d = analyzer.analyze("search", "timeout", "", None)
        assert d.metadata["context"] == {}

    # -- Root cause determination -------------------------------------

    def test_root_cause_includes_tool_name(self, analyzer):
        d = analyzer.analyze("my_tool", "timeout", "", {})
        assert "my_tool" in d.root_cause

    def test_root_cause_retry_enrichment(self, analyzer):
        d = analyzer.analyze("search", "timeout", "", {"retry_count": 3})
        assert "3" in d.root_cause
        assert "non-transient" in d.root_cause.lower()

    def test_root_cause_no_retry_enrichment_for_validation(self, analyzer):
        d = analyzer.analyze("search", "invalid arg", "", {"retry_count": 5})
        assert "non-transient" not in d.root_cause.lower()

    # -- Contributing factors -----------------------------------------

    def test_contributing_factors_timeout(self, analyzer):
        d = analyzer.analyze("search", "timeout", "", {})
        assert len(d.contributing_factors) >= 2

    def test_contributing_factors_network_endpoint(self, analyzer):
        d = analyzer.analyze(
            "search", "Connection refused", "", {"endpoint": "http://api"}
        )
        assert any("http://api" in f for f in d.contributing_factors)

    def test_contributing_factors_with_args(self, analyzer):
        d = analyzer.analyze(
            "search", "timeout", "", {"args": {"q": "test"}}
        )
        assert any("test" in f for f in d.contributing_factors)

    def test_contributing_factors_not_found_path(self, analyzer):
        d = analyzer.analyze(
            "read_file", "No such file", "", {"path": "/foo/bar"}
        )
        assert any("/foo/bar" in f for f in d.contributing_factors)

    def test_contributing_factors_resource_retry_after(self, analyzer):
        d = analyzer.analyze(
            "search", "429 rate limit", "", {"retry_after": 30}
        )
        assert any("30" in f for f in d.contributing_factors)

    def test_contributing_factors_validation_schema(self, analyzer):
        d = analyzer.analyze(
            "search", "validation failed", "", {"schema_errors": ["missing field"]}
        )
        assert any("missing field" in f for f in d.contributing_factors)

    def test_contributing_factors_syntax_line(self, analyzer):
        d = analyzer.analyze(
            "terminal", "SyntaxError", "", {"line_number": 42}
        )
        assert any("42" in f for f in d.contributing_factors)

    # -- Fix generation per category ----------------------------------

    def test_fixes_network_nonempty(self, analyzer):
        d = analyzer.analyze("search", "Connection refused", "", {})
        assert len(d.suggested_fixes) >= 3

    def test_fixes_permission_nonempty(self, analyzer):
        d = analyzer.analyze("search", "403 Forbidden", "", {})
        assert len(d.suggested_fixes) >= 3

    def test_fixes_not_found_nonempty(self, analyzer):
        d = analyzer.analyze("search", "not found", "", {})
        assert len(d.suggested_fixes) >= 3

    def test_fixes_validation_nonempty(self, analyzer):
        d = analyzer.analyze("search", "invalid", "", {})
        assert len(d.suggested_fixes) >= 3

    def test_fixes_timeout_nonempty(self, analyzer):
        d = analyzer.analyze("search", "timeout", "", {})
        assert len(d.suggested_fixes) >= 3

    def test_fixes_resource_limit_nonempty(self, analyzer):
        d = analyzer.analyze("search", "429", "", {})
        assert len(d.suggested_fixes) >= 3

    def test_fixes_syntax_error_nonempty(self, analyzer):
        d = analyzer.analyze("search", "SyntaxError", "", {})
        assert len(d.suggested_fixes) >= 3

    def test_fixes_unknown_nonempty(self, analyzer):
        d = analyzer.analyze("search", "bizarre error", "", {})
        assert len(d.suggested_fixes) >= 3

    def test_fixes_include_tool_name(self, analyzer):
        d = analyzer.analyze("my_tool", "timeout", "", {})
        assert any("my_tool" in fix for fix in d.suggested_fixes)

    def test_fixes_unknown_interpolate_tool_name(self, analyzer):
        # Regression: UNKNOWN-category fixes must interpolate the real tool
        # name, never leak a literal "{tool_name}" placeholder (missing
        # f-prefix bug).
        d = analyzer.analyze("weird_tool", "bizarre error", "", {})
        assert d.category == FailureCategory.UNKNOWN
        assert all("{tool_name}" not in fix for fix in d.suggested_fixes)
        assert any("weird_tool" in fix for fix in d.suggested_fixes)

    def test_fixes_returns_copy(self, analyzer):
        d1 = analyzer.analyze("search", "timeout", "", {})
        d2 = analyzer.analyze("search", "timeout", "", {})
        d1.suggested_fixes.append("injected")
        assert "injected" not in d2.suggested_fixes

    # -- Confidence scoring -------------------------------------------

    def test_confidence_range(self, analyzer):
        d = analyzer.analyze("search", "timeout", "", {})
        assert 0.0 <= d.confidence <= 1.0

    def test_confidence_unknown_low(self, analyzer):
        d = analyzer.analyze("search", "bizarre unknown error", "", {})
        assert d.confidence <= 0.2

    def test_confidence_single_match(self, analyzer):
        d = analyzer.analyze("search", "timeout", "", {})
        assert d.confidence >= 0.3

    def test_confidence_multiple_matches_higher(self, analyzer):
        d_single = analyzer.analyze("search", "timeout", "", {})
        d_multi = analyzer.analyze("search", "timeout: deadline exceeded", "", {})
        assert d_multi.confidence >= d_single.confidence

    def test_confidence_dominant_category(self, analyzer):
        # Only one pattern category matches → high dominance
        d = analyzer.analyze("search", "timeout", "", {})
        assert d.confidence >= 0.4

    def test_confidence_unknown_exact(self, analyzer):
        d = analyzer.analyze("search", "bizarre", "", {})
        assert d.confidence == 0.1


# ---------------------------------------------------------------------------
# DiagnosisHistory tests
# ---------------------------------------------------------------------------

class TestDiagnosisHistory:
    """Tests for the DiagnosisHistory tracker."""

    # -- add / get_recent ---------------------------------------------

    def test_add_and_get_recent(self, history):
        d = Diagnosis(category=FailureCategory.TIMEOUT)
        history.add("search", d)
        assert len(history.get_recent("search")) == 1

    def test_get_recent_empty(self, history):
        assert history.get_recent("nonexistent") == []

    def test_get_recent_limit(self, history):
        for i in range(10):
            history.add("search", Diagnosis(category=FailureCategory.TIMEOUT))
        recent = history.get_recent("search", n=3)
        assert len(recent) == 3

    def test_get_recent_more_than_available(self, history):
        history.add("search", Diagnosis(category=FailureCategory.TIMEOUT))
        recent = history.get_recent("search", n=10)
        assert len(recent) == 1

    def test_get_recent_zero(self, history):
        history.add("search", Diagnosis(category=FailureCategory.TIMEOUT))
        assert history.get_recent("search", n=0) == []

    def test_get_recent_default_n(self, history):
        for i in range(7):
            history.add("search", Diagnosis(category=FailureCategory.TIMEOUT))
        recent = history.get_recent("search")
        assert len(recent) == 5

    def test_get_recent_returns_last_n(self, history):
        for i in range(5):
            history.add("search", Diagnosis(category=FailureCategory.TIMEOUT, root_cause=str(i)))
        recent = history.get_recent("search", n=2)
        assert recent[0].root_cause == "3"
        assert recent[1].root_cause == "4"

    def test_separate_tools_isolated(self, history):
        history.add("search", Diagnosis(category=FailureCategory.TIMEOUT))
        history.add("read_file", Diagnosis(category=FailureCategory.NOT_FOUND))
        assert len(history.get_recent("search")) == 1
        assert len(history.get_recent("read_file")) == 1

    # -- get_recurring ------------------------------------------------

    def test_get_recurring_none_empty(self, history):
        assert history.get_recurring("search") is None

    def test_get_recurring_single_entry_none(self, history):
        history.add("search", Diagnosis(category=FailureCategory.TIMEOUT))
        assert history.get_recurring("search") is None

    def test_get_recurring_dominant(self, history):
        history.add("search", Diagnosis(category=FailureCategory.TIMEOUT))
        history.add("search", Diagnosis(category=FailureCategory.TIMEOUT))
        history.add("search", Diagnosis(category=FailureCategory.NETWORK))
        assert history.get_recurring("search") == FailureCategory.TIMEOUT

    def test_get_recurring_tie_break_recent(self, history):
        history.add("search", Diagnosis(category=FailureCategory.TIMEOUT))
        history.add("search", Diagnosis(category=FailureCategory.NETWORK))
        # Tie → most recent wins → NETWORK
        assert history.get_recurring("search") == FailureCategory.NETWORK

    def test_get_recurring_all_same(self, history):
        for _ in range(3):
            history.add("search", Diagnosis(category=FailureCategory.PERMISSION))
        assert history.get_recurring("search") == FailureCategory.PERMISSION

    # -- clear --------------------------------------------------------

    def test_clear_specific_tool(self, history):
        history.add("search", Diagnosis(category=FailureCategory.TIMEOUT))
        history.add("read_file", Diagnosis(category=FailureCategory.NOT_FOUND))
        history.clear("search")
        assert history.get_recent("search") == []
        assert len(history.get_recent("read_file")) == 1

    def test_clear_all(self, history):
        history.add("search", Diagnosis(category=FailureCategory.TIMEOUT))
        history.add("read_file", Diagnosis(category=FailureCategory.NOT_FOUND))
        history.clear()
        assert history.get_recent("search") == []
        assert history.get_recent("read_file") == []

    def test_clear_nonexistent_tool(self, history):
        history.clear("nonexistent")  # should not raise

    # -- stats --------------------------------------------------------

    def test_stats_empty(self, history):
        s = history.stats()
        assert s["total_diagnoses"] == 0
        assert s["per_category"]["timeout"] == 0
        assert s["per_tool"] == {}

    def test_stats_total(self, history):
        history.add("search", Diagnosis(category=FailureCategory.TIMEOUT))
        history.add("search", Diagnosis(category=FailureCategory.NETWORK))
        history.add("read_file", Diagnosis(category=FailureCategory.NOT_FOUND))
        s = history.stats()
        assert s["total_diagnoses"] == 3

    def test_stats_per_category(self, history):
        history.add("search", Diagnosis(category=FailureCategory.TIMEOUT))
        history.add("search", Diagnosis(category=FailureCategory.TIMEOUT))
        history.add("read_file", Diagnosis(category=FailureCategory.NOT_FOUND))
        s = history.stats()
        assert s["per_category"]["timeout"] == 2
        assert s["per_category"]["not_found"] == 1
        assert s["per_category"]["network"] == 0

    def test_stats_per_tool(self, history):
        history.add("search", Diagnosis(category=FailureCategory.TIMEOUT))
        history.add("read_file", Diagnosis(category=FailureCategory.NOT_FOUND))
        history.add("read_file", Diagnosis(category=FailureCategory.NOT_FOUND))
        s = history.stats()
        assert s["per_tool"]["search"] == 1
        assert s["per_tool"]["read_file"] == 2

    def test_stats_all_categories_present(self, history):
        s = history.stats()
        for cat in FailureCategory:
            assert cat.value in s["per_category"]

    # -- Serialization ------------------------------------------------

    def test_to_dict_empty(self, history):
        assert history.to_dict() == {}

    def test_serialization_round_trip(self, history):
        history.add("search", Diagnosis(
            category=FailureCategory.TIMEOUT,
            root_cause="timeout",
            contributing_factors=["slow"],
            suggested_fixes=["retry"],
            confidence=0.8,
            metadata={"k": "v"},
        ))
        history.add("read_file", Diagnosis(
            category=FailureCategory.NOT_FOUND,
            root_cause="missing",
        ))
        d = history.to_dict()
        restored = DiagnosisHistory.from_dict(d)
        assert len(restored.get_recent("search")) == 1
        assert len(restored.get_recent("read_file")) == 1
        assert restored.get_recent("search")[0].category == FailureCategory.TIMEOUT
        assert restored.get_recent("read_file")[0].root_cause == "missing"

    def test_json_serialization(self, history):
        history.add("search", Diagnosis(category=FailureCategory.TIMEOUT, confidence=0.7))
        s = json.dumps(history.to_dict())
        restored = DiagnosisHistory.from_dict(json.loads(s))
        assert len(restored.get_recent("search")) == 1
        assert restored.get_recent("search")[0].confidence == pytest.approx(0.7)

    # -- Thread safety ------------------------------------------------

    def test_thread_safety(self, history):
        errors = []

        def worker():
            try:
                for _ in range(20):
                    history.add("search", Diagnosis(category=FailureCategory.TIMEOUT))
                    history.get_recent("search")
                    history.stats()
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert history.stats()["total_diagnoses"] == 100


# ---------------------------------------------------------------------------
# Integration: analyzer + history
# ---------------------------------------------------------------------------

class TestIntegration:
    """Integration tests combining analyzer and history."""

    def test_full_pipeline(self, analyzer, history):
        d = analyzer.analyze("search", "timeout", "TimeoutError", {"retry_count": 1})
        history.add("search", d)
        assert history.get_recent("search")[0].category == FailureCategory.TIMEOUT
        assert history.get_recurring("search") is None  # single entry

    def test_recurring_after_multiple_failures(self, analyzer, history):
        for i in range(3):
            d = analyzer.analyze("search", "timeout", "", {})
            history.add("search", d)
        assert history.get_recurring("search") == FailureCategory.TIMEOUT
        assert history.stats()["total_diagnoses"] == 3

    def test_custom_classifier_injected(self):
        custom = ErrorClassifier(patterns={FailureCategory.NETWORK: ["foobar"]})
        analyzer = RootCauseAnalyzer(classifier=custom)
        d = analyzer.analyze("search", "foobar error", "", {})
        assert d.category == FailureCategory.NETWORK

    def test_diagnosis_serializes_with_history(self, analyzer, history):
        d = analyzer.analyze("search", "Connection refused", "", {})
        history.add("search", d)
        s = json.dumps(history.to_dict())
        restored = DiagnosisHistory.from_dict(json.loads(s))
        assert restored.get_recent("search")[0].category == FailureCategory.NETWORK
