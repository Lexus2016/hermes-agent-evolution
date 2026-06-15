"""Tests for execute_code structured failure diagnostics (evolution #215)."""

import json
from unittest.mock import patch

from tools.code_execution_tool import _classify_execution_failure


class TestClassifyExecutionFailure:
    def test_timeout(self):
        result = _classify_execution_failure(
            exit_code=-1,
            stderr_text="",
            status="timeout",
            timeout_value=10,
        )
        assert result["status"] == "timeout"
        assert result["classification"] == "timeout"
        assert "10s" in result["error"]
        assert "timeout" in result["suggestion"].lower()

    def test_missing_package(self):
        result = _classify_execution_failure(
            exit_code=1,
            stderr_text="Traceback: ModuleNotFoundError: No module named 'pandas'",
            status="error",
            timeout_value=30,
        )
        assert result["status"] == "error"
        assert result["classification"] == "missing_package"
        assert result["missing_package"] == "pandas"
        assert "pandas" in result["error"]
        assert "Install" in result["suggestion"]

    def test_syntax_error(self):
        result = _classify_execution_failure(
            exit_code=1,
            stderr_text="  File \"script.py\", line 1\n    if x\n        ^\nSyntaxError: expected ':'",
            status="error",
            timeout_value=30,
        )
        assert result["classification"] == "syntax_error"
        assert "syntax" in result["suggestion"].lower()

    def test_nonzero_exit(self):
        result = _classify_execution_failure(
            exit_code=42,
            stderr_text="",
            status="error",
            timeout_value=30,
        )
        assert result["classification"] == "nonzero_exit"
        assert result["exit_code"] == 42
        assert "42" in result["error"]

    def test_nonzero_exit_prefers_stderr(self):
        result = _classify_execution_failure(
            exit_code=1,
            stderr_text="Something went wrong",
            status="error",
            timeout_value=30,
        )
        assert result["classification"] == "nonzero_exit"
        assert result["error"] == "Something went wrong"
        assert result["stderr"] == "Something went wrong"
