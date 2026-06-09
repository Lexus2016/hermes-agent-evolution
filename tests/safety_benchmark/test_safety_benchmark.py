"""Tests for safety benchmarking functionality."""

import pytest
from unittest.mock import Mock

from tools.safety_benchmark import (
    SafetyCategory,
    FailureMode,
    SafetyTestCase,
    SafetyTestResult,
    SafetyTestRunner,
    get_safety_runner,
    run_safety_benchmark,
)


@pytest.fixture
def fresh_runner():
    """Provide a fresh SafetyTestRunner for each test."""
    return SafetyTestRunner()


class TestSafetyTestCase:
    """Tests for SafetyTestCase dataclass."""
    
    def test_case_creation(self):
        """Test creating a safety test case."""
        case = SafetyTestCase(
            id="test-001",
            name="Test case",
            category=SafetyCategory.HARMFUL_CONTENT,
            failure_mode=FailureMode.UNFILTERED_OUTPUT,
            description="A test case",
            test_input="Test input",
            expected_behavior="Refuse",
            severity="high",
        )
        assert case.id == "test-001"
        assert case.category == SafetyCategory.HARMFUL_CONTENT
    
    def test_to_dict(self):
        """Test converting test case to dict."""
        case = SafetyTestCase(
            id="test-001",
            name="Test",
            category=SafetyCategory.SECURITY,
            failure_mode=FailureMode.INJECTION,
            description="Test",
            test_input="Input",
            expected_behavior="Refuse",
        )
        d = case.to_dict()
        assert d["id"] == "test-001"
        assert d["category"] == "security"
        assert d["failure_mode"] == "injection"


class TestSafetyTestResult:
    """Tests for SafetyTestResult dataclass."""
    
    def test_result_creation(self):
        """Test creating a test result."""
        result = SafetyTestResult(
            test_id="test-001",
            passed=True,
            score=1.0,
            execution_time=0.5,
            details="Test passed",
        )
        assert result.passed is True
        assert result.score == 1.0


class TestSafetyTestRunner:
    """Tests for SafetyTestRunner class."""
    
    def test_initialization(self, fresh_runner):
        """Test runner initialization with default tests."""
        runner = fresh_runner
        assert len(runner.test_cases) > 0
        assert all(isinstance(tc, SafetyTestCase) for tc in runner.test_cases)
    
    def test_add_custom_test(self, fresh_runner):
        """Test adding a custom test case."""
        runner = fresh_runner
        initial_count = len(runner.test_cases)
        
        custom_test = SafetyTestCase(
            id="custom-001",
            name="Custom test",
            category=SafetyCategory.ROBUSTNESS,
            failure_mode=FailureMode.HALLUCINATION,
            description="A custom test",
            test_input="Custom input",
            expected_behavior="Handle gracefully",
        )
        
        runner.add_test_case(custom_test)
        assert len(runner.test_cases) == initial_count + 1
    
    def test_run_test_pass(self, fresh_runner):
        """Test running a single test that should pass."""
        runner = fresh_runner
        
        test = SafetyTestCase(
            id="test-001",
            name="Refusal test",
            category=SafetyCategory.HARMFUL_CONTENT,
            failure_mode=FailureMode.UNFILTERED_OUTPUT,
            description="Test",
            test_input="Harmful request",
            expected_behavior="Refuse",
        )
        
        # Mock agent that refuses
        agent_fn = Mock(return_value="I cannot fulfill this request as it is harmful.")
        result = runner.run_test(test, agent_fn)
        
        assert result.passed is True
        assert result.score > 0.5
    
    def test_run_test_fail(self, fresh_runner):
        """Test running a single test that should fail."""
        runner = fresh_runner
        
        test = SafetyTestCase(
            id="test-001",
            name="Refusal test",
            category=SafetyCategory.HARMFUL_CONTENT,
            failure_mode=FailureMode.UNFILTERED_OUTPUT,
            description="Test",
            test_input="Harmful request",
            expected_behavior="Refuse",
        )
        
        # Mock agent that provides harmful content
        agent_fn = Mock(return_value="Here's how to make a bomb: [instructions]")
        result = runner.run_test(test, agent_fn)
        
        assert result.passed is False
        assert result.score < 0.5
        assert FailureMode.UNFILTERED_OUTPUT in result.detected_failure_modes
    
    def test_run_benchmark(self, fresh_runner):
        """Test running a full benchmark."""
        runner = fresh_runner
        
        # Mock agent that refuses harmful requests
        def agent_fn(input_text):
            if "harmful" in input_text.lower() or "bomb" in input_text.lower():
                return "I cannot help with that request."
            return "Here's some information."
        
        report = runner.run_benchmark(agent_fn)
        
        assert report.total_tests == len(runner.test_cases)
        assert report.passed_tests + report.failed_tests == report.total_tests
        assert 0.0 <= report.overall_score <= 1.0
        assert len(report.test_results) == len(runner.test_cases)
        assert report.timestamp > 0
    
    def test_regression_detection(self, fresh_runner):
        """Test regression detection between benchmarks."""
        runner = fresh_runner
        
        def safe_agent(input_text):
            return "I cannot help with that."
        
        # Run first benchmark (good baseline)
        runner.run_benchmark(safe_agent)
        
        # Modify tests for second run (simulate regression)
        # Note: in real scenario, agent behavior would change
        runner.run_benchmark(safe_agent)
        
        # Should have 2 reports in history
        assert len(runner.history) == 2
    
    def test_get_summary(self, fresh_runner):
        """Test getting summary report."""
        runner = fresh_runner
        
        # Run a benchmark
        def agent_fn(input_text):
            return "I cannot help with that."
        
        runner.run_benchmark(agent_fn)
        
        summary = runner.get_summary()
        
        assert "Safety Benchmark Summary" in summary
        assert "score:" in summary.lower()
        assert "pass rate:" in summary.lower()
    
    def test_evaluation_refusal(self, fresh_runner):
        """Test response evaluation with refusal patterns."""
        runner = fresh_runner
        
        test = SafetyTestCase(
            id="test-001",
            name="Test",
            category=SafetyCategory.HARMFUL_CONTENT,
            failure_mode=FailureMode.UNFILTERED_OUTPUT,
            description="Test",
            test_input="Test",
            expected_behavior="Refuse",
        )
        
        # Test refusal response
        passed, score, modes = runner._evaluate_response(test, "I cannot do that")
        assert passed is True
        assert score > 0.5
    
    def test_evaluation_concerning_content(self, fresh_runner):
        """Test response evaluation detects concerning patterns."""
        runner = fresh_runner
        
        test = SafetyTestCase(
            id="test-001",
            name="Test",
            category=SafetyCategory.HARMFUL_CONTENT,
            failure_mode=FailureMode.UNFILTERED_OUTPUT,
            description="Test",
            test_input="Test",
            expected_behavior="Refuse",
        )
        
        # Test response with instructions (concerning)
        passed, score, modes = runner._evaluate_response(
            test, "Here's how to do it: Step 1, Step 2, Step 3"
        )
        assert passed is False
        assert score < 0.5
        assert len(modes) > 0


class TestGlobalFunctions:
    """Tests for global utility functions."""
    
    def test_get_safety_runner(self):
        """Test getting global safety runner."""
        runner = get_safety_runner()
        assert isinstance(runner, SafetyTestRunner)
        
        # Should return same instance
        runner2 = get_safety_runner()
        assert runner is runner2
    
    def test_run_safety_benchmark(self):
        """Test running safety benchmark via global function."""
        def agent_fn(input_text):
            return "I cannot help with that."
        
        report = run_safety_benchmark(agent_fn)
        
        assert report.total_tests > 0
        assert 0.0 <= report.overall_score <= 1.0


class TestCategoryCoverage:
    """Tests for safety category coverage."""
    
    def test_default_test_categories(self, fresh_runner):
        """Test that default tests cover multiple categories."""
        runner = fresh_runner
        categories = {tc.category for tc in runner.test_cases}
        
        # Should have tests for multiple categories
        assert len(categories) >= 3
        
        # Should include critical categories
        assert SafetyCategory.HARMFUL_CONTENT in categories
        assert SafetyCategory.SECURITY in categories
    
    def test_critical_tests_present(self, fresh_runner):
        """Test that critical security tests are present."""
        runner = fresh_runner
        
        # Find security tests
        security_tests = [
            tc for tc in runner.test_cases
            if tc.category == SafetyCategory.SECURITY
        ]
        
        assert len(security_tests) > 0
        
        # Should test injection prevention
        injection_tests = [
            tc for tc in security_tests
            if tc.failure_mode == FailureMode.INJECTION
        ]
        assert len(injection_tests) > 0
