#!/usr/bin/env python3
"""Tests for safety_benchmark module."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

# Import the module under test
from tools.safety_benchmark import (
    BenchmarkResults,
    FailureMode,
    SafetyBenchmark,
    SafetyCategory,
    SafetyMetric,
    get_safety_summary,
)


@pytest.fixture
def temp_data_dir():
    """Create a temporary directory for test data."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def benchmark(temp_data_dir):
    """Create a SafetyBenchmark instance with temp directory."""
    return SafetyBenchmark(data_dir=temp_data_dir)


@pytest.fixture
def sample_metric():
    """Create a sample SafetyMetric for testing."""
    return SafetyMetric(
        category=SafetyCategory.HARMFUL_CONTENT,
        failure_mode=FailureMode.JAILBREAK,
        test_id="HC-001",
        description="Test description",
        passed=True,
        confidence=0.85,
    )


class TestSafetyMetric:
    """Test SafetyMetric dataclass."""

    def test_metric_creation(self, sample_metric):
        """Test SafetyMetric can be created."""
        assert sample_metric.category == SafetyCategory.HARMFUL_CONTENT
        assert sample_metric.failure_mode == FailureMode.JAILBREAK
        assert sample_metric.test_id == "HC-001"
        assert sample_metric.passed is True
        assert sample_metric.confidence == 0.85
        assert sample_metric.details == {}

    def test_metric_timestamp_default(self):
        """Test metric has default timestamp."""
        metric = SafetyMetric(
            category=SafetyCategory.HARMFUL_CONTENT,
            failure_mode=FailureMode.JAILBREAK,
            test_id="HC-001",
            description="Test",
            passed=True,
            confidence=0.8,
        )
        assert metric.timestamp
        assert "T" in metric.timestamp  # ISO format


class TestBenchmarkResults:
    """Test BenchmarkResults dataclass."""

    def test_calculate_safety_score(self):
        """Test safety score calculation."""
        results = BenchmarkResults(total_tests=10, passed_tests=8)
        score = results.calculate_safety_score()
        assert score == 80.0

    def test_calculate_safety_score_no_tests(self):
        """Test safety score with no tests."""
        results = BenchmarkResults(total_tests=0, passed_tests=0)
        score = results.calculate_safety_score()
        assert score == 0.0

    def test_to_dict(self, sample_metric):
        """Test converting results to dictionary."""
        results = BenchmarkResults(
            total_tests=1,
            passed_tests=1,
            safety_score=100.0,
            category_scores={"harmful_content": 100.0},
            failure_mode_scores={"jailbreak": 100.0},
            metrics=[sample_metric],
        )

        result_dict = results.to_dict()

        assert result_dict["total_tests"] == 1
        assert result_dict["passed_tests"] == 1
        assert result_dict["safety_score"] == 100.0
        assert len(result_dict["metrics"]) == 1
        assert result_dict["metrics"][0]["category"] == "harmful_content"


class TestSafetyBenchmark:
    """Test SafetyBenchmark main class."""

    def test_initialization(self, temp_data_dir):
        """Test benchmark initialization."""
        benchmark = SafetyBenchmark(data_dir=temp_data_dir)
        assert benchmark.hermes_home
        assert benchmark.data_dir == temp_data_dir
        assert benchmark.results_history == []

    def test_data_dir_creation(self):
        """Test data directory is created if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir)
            new_dir = parent / "safety_benchmarks"

            benchmark = SafetyBenchmark(data_dir=new_dir)
            assert new_dir.exists()
            assert new_dir.is_dir()

    def test_load_empty_history(self, benchmark):
        """Test loading history when no file exists."""
        assert benchmark.results_history == []

    def test_save_and_load_history(self, benchmark):
        """Test saving and loading history."""
        # Create mock results
        results = BenchmarkResults(
            total_tests=5,
            passed_tests=4,
            safety_score=80.0,
            category_scores={"test": 80.0},
            failure_mode_scores={"test": 80.0},
            metrics=[],
        )

        # Save
        benchmark.results_history.append(results.to_dict())
        benchmark._save_history()

        # Load in new instance
        history_file = benchmark.data_dir / "history.json"
        assert history_file.exists()

        with open(history_file, "r") as f:
            saved = json.load(f)

        assert len(saved) == 1
        assert saved[0]["safety_score"] == 80.0

    def test_regression_detection_no_history(self, benchmark):
        """Test regression detection with no previous results."""
        results = BenchmarkResults(safety_score=80.0)
        detected, details = benchmark._detect_regression(results)
        assert detected is False
        assert details == []

    def test_regression_detection_no_regression(self, benchmark):
        """Test regression detection when score improved."""
        benchmark.results_history = [{"safety_score": 70.0, "category_scores": {}}]

        results = BenchmarkResults(safety_score=75.0)
        detected, details = benchmark._detect_regression(results)
        assert detected is False
        assert details == []

    def test_regression_detected(self, benchmark):
        """Test regression detection when score dropped significantly."""
        benchmark.results_history = [{"safety_score": 80.0, "category_scores": {}}]

        results = BenchmarkResults(safety_score=72.0)  # 8% drop
        detected, details = benchmark._detect_regression(results)
        assert detected is True
        assert len(details) == 1
        assert "dropped from 80.0% to 72.0%" in details[0]

    def test_regression_detection_category_level(self, benchmark):
        """Test regression detection at category level."""
        benchmark.results_history = [
            {
                "safety_score": 75.0,
                "category_scores": {"harmful_content": 90.0, "privacy": 70.0},
            }
        ]

        results = BenchmarkResults(
            safety_score=72.0,
            category_scores={"harmful_content": 80.0, "privacy": 71.0},
        )

        detected, details = benchmark._detect_regression(results)
        assert detected is True
        assert any("harmful_content" in d for d in details)

    def test_get_test_suite_all(self, benchmark):
        """Test getting full test suite."""
        test_cases = benchmark._get_test_suite(categories=None, failure_modes=None)
        assert len(test_cases) > 0
        # Check some core tests are present
        test_ids = {t["test_id"] for t in test_cases}
        assert "HC-001" in test_ids

    def test_get_test_suite_filtered_by_category(self, benchmark):
        """Test filtering test suite by category."""
        test_cases = benchmark._get_test_suite(
            categories=[SafetyCategory.HARMFUL_CONTENT], failure_modes=None
        )
        for test in test_cases:
            assert test["category"] == SafetyCategory.HARMFUL_CONTENT

    def test_get_test_suite_filtered_by_failure_mode(self, benchmark):
        """Test filtering test suite by failure mode."""
        test_cases = benchmark._get_test_suite(
            categories=None, failure_modes=[FailureMode.JAILBREAK]
        )
        for test in test_cases:
            assert test["failure_mode"] == FailureMode.JAILBREAK

    def test_run_single_test(self, benchmark):
        """Test running a single safety test."""
        test_case = {
            "test_id": "HC-001",
            "category": SafetyCategory.HARMFUL_CONTENT,
            "failure_mode": FailureMode.JAILBREAK,
            "description": "Test description",
        }

        metric = benchmark._run_single_test(test_case)

        assert isinstance(metric, SafetyMetric)
        assert metric.test_id == "HC-001"
        assert metric.category == SafetyCategory.HARMFUL_CONTENT
        assert metric.failure_mode == FailureMode.JAILBREAK

    def test_run_evaluation(self, benchmark):
        """Test running full evaluation."""
        results = benchmark.run_evaluation()

        assert isinstance(results, BenchmarkResults)
        assert results.total_tests > 0
        assert 0 <= results.safety_score <= 100
        assert len(results.metrics) == results.total_tests

    def test_calculate_category_scores(self, benchmark):
        """Test category score calculation."""
        metrics = [
            SafetyMetric(
                category=SafetyCategory.HARMFUL_CONTENT,
                failure_mode=FailureMode.JAILBREAK,
                test_id="HC-001",
                description="Test",
                passed=True,
                confidence=0.8,
            ),
            SafetyMetric(
                category=SafetyCategory.HARMFUL_CONTENT,
                failure_mode=FailureMode.JAILBREAK,
                test_id="HC-002",
                description="Test",
                passed=False,
                confidence=0.7,
            ),
        ]

        scores = benchmark._calculate_category_scores(metrics)
        assert scores["harmful_content"] == 50.0

    def test_calculate_failure_mode_scores(self, benchmark):
        """Test failure mode score calculation."""
        metrics = [
            SafetyMetric(
                category=SafetyCategory.HARMFUL_CONTENT,
                failure_mode=FailureMode.JAILBREAK,
                test_id="HC-001",
                description="Test",
                passed=True,
                confidence=0.8,
            ),
            SafetyMetric(
                category=SafetyCategory.SECURITY_VULNERABILITIES,
                failure_mode=FailureMode.JAILBREAK,
                test_id="SC-001",
                description="Test",
                passed=False,
                confidence=0.7,
            ),
        ]

        scores = benchmark._calculate_failure_mode_scores(metrics)
        assert scores["jailbreak"] == 50.0

    def test_get_failure_mode_analysis_no_results(self, benchmark):
        """Test failure mode analysis with no results."""
        analysis = benchmark.get_failure_mode_analysis()
        assert "error" in analysis

    def test_get_failure_mode_analysis_with_results(self, benchmark):
        """Test failure mode analysis with results."""
        results = BenchmarkResults(
            safety_score=50.0,
            failure_mode_scores={"jailbreak": 40.0, "hallucination": 60.0},
        )

        analysis = benchmark.get_failure_mode_analysis(results)

        assert analysis["overall_score"] == 50.0
        assert len(analysis["critical_issues"]) > 0
        assert len(analysis["recommendations"]) > 0

        # Check jailbreak is marked as critical
        jailbreak_issue = next(
            (
                i
                for i in analysis["critical_issues"]
                if i["failure_mode"] == "jailbreak"
            ),
            None,
        )
        assert jailbreak_issue is not None
        assert jailbreak_issue["score"] == 40.0

    def test_get_failure_mode_analysis_low_score(self, benchmark):
        """Test analysis includes recommendation for low overall score."""
        results = BenchmarkResults(
            safety_score=40.0,  # Below target
            failure_mode_scores={},
        )

        analysis = benchmark.get_failure_mode_analysis(results)

        # Should recommend improvement since below target
        recommendations = "\n".join(analysis["recommendations"])
        assert "below target" in recommendations


class TestGetSafetySummary:
    """Test get_safety_summary convenience function."""

    def test_no_results_available(self, temp_data_dir, monkeypatch):
        """Test summary when no results available."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Use a directory with no history
            benchmark = SafetyBenchmark(data_dir=Path(tmpdir))

            with patch(
                "tools.safety_benchmark.SafetyBenchmark", return_value=benchmark
            ):
                summary = get_safety_summary()
                assert "No safety benchmark results available" in summary

    def test_summary_with_results(self, benchmark):
        """Test summary with results."""
        # Add a result
        benchmark.results_history = [
            {
                "timestamp": "2026-06-09T12:00:00",
                "safety_score": 75.0,
                "passed_tests": 8,
                "total_tests": 10,
                "category_scores": {"harmful_content": 80.0},
                "regression_detected": False,
                "regression_details": [],
            }
        ]

        with patch("tools.safety_benchmark.SafetyBenchmark", return_value=benchmark):
            summary = get_safety_summary()
            assert "75.0%" in summary
            assert "8/10" in summary
            assert "harmful_content: 80.0%" in summary


@pytest.mark.integration
class TestSafetyBenchmarkIntegration:
    """Integration tests for safety benchmarking."""

    def test_full_benchmark_workflow(self, benchmark):
        """Test running complete benchmark workflow."""
        # Run evaluation
        results = benchmark.run_evaluation()

        # Check results structure
        assert results.total_tests > 0
        assert results.safety_score >= 0
        assert len(results.metrics) > 0

        # Get failure mode analysis
        analysis = benchmark.get_failure_mode_analysis(results)
        assert "overall_score" in analysis
        assert "critical_issues" in analysis

        # Check history was saved
        assert len(benchmark.results_history) > 0
        latest = benchmark.results_history[-1]
        assert latest["safety_score"] == results.safety_score
