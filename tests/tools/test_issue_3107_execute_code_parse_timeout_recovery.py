"""Tests for Issue #3107: execute_code parse-error validation and timeout/exception recovery."""

import json
import pytest

from tools.code_execution_tool import (
    execute_code,
    _classify_execution_failure,
)


class TestPreExecuteParseValidation:
    """Test AST parse validation before invoking runner."""

    def test_syntax_error_rejected_pre_execution(self):
        malformed_code = "def foo(\n    print('unclosed def')"
        res_raw = execute_code(malformed_code)
        res = json.loads(res_raw)

        assert res["status"] == "error"
        assert res["classification"] == "parse-error"
        assert "SyntaxError" in res["error"]
        assert "syntax_error" in res
        assert res["syntax_error"]["lineno"] in (1, 2)
        assert "Fix the syntax error" in res["suggestion"]
        assert "valid Python syntax" in res["suggestion"]
        assert res["tool_calls_made"] == 0

    def test_indentation_error_rejected_pre_execution(self):
        bad_indent = "def foo():\nprint('bad indent')"
        res_raw = execute_code(bad_indent)
        res = json.loads(res_raw)

        assert res["status"] == "error"
        assert res["classification"] == "parse-error"
        assert "SyntaxError" in res["error"] or "IndentationError" in res["error"]
        assert "syntax_error" in res


class TestEnhancedFailureClassification:
    """Test structured exception and signal classification."""

    def test_timeout_classification_and_suggestions(self):
        diag = _classify_execution_failure(
            exit_code=-1,
            stderr_text="",
            status="timeout",
            timeout_value=45,
        )
        assert diag["status"] == "timeout"
        assert diag["classification"] == "timeout"
        assert "45s" in diag["error"]
        assert "smaller chunked steps" in diag["suggestion"]
        assert "code_execution.timeout" in diag["suggestion"]

    def test_sigkill_oom_classification(self):
        diag = _classify_execution_failure(
            exit_code=137,
            stderr_text="",
            status="error",
            timeout_value=30,
        )
        assert diag["classification"] == "killed_oom"
        assert "SIGKILL" in diag["error"]
        assert "smaller batches" in diag["suggestion"]

    def test_sigsegv_classification(self):
        diag = _classify_execution_failure(
            exit_code=139,
            stderr_text="",
            status="error",
            timeout_value=30,
        )
        assert diag["classification"] == "segmentation_fault"
        assert "Segmentation Fault" in diag["error"]

    def test_sigabrt_classification(self):
        diag = _classify_execution_failure(
            exit_code=134,
            stderr_text="",
            status="error",
            timeout_value=30,
        )
        assert diag["classification"] == "aborted"
        assert "SIGABRT" in diag["error"]

    @pytest.mark.parametrize(
        "exc_name,stderr,expected_class",
        [
            ("NameError", "Traceback (most recent call last):\n  File \"run.py\", line 1\nNameError: name 'undefined_var' is not defined", "nameerror"),
            ("KeyError", "Traceback (most recent call last):\n  File \"run.py\", line 1\nKeyError: 'missing_key'", "keyerror"),
            ("IndexError", "Traceback (most recent call last):\n  File \"run.py\", line 1\nIndexError: list index out of range", "indexerror"),
            ("TypeError", "Traceback (most recent call last):\n  File \"run.py\", line 1\nTypeError: unsupported operand type(s) for +: 'int' and 'str'", "typeerror"),
            ("ValueError", "Traceback (most recent call last):\n  File \"run.py\", line 1\nValueError: invalid literal for int() with base 10: 'abc'", "valueerror"),
            ("AttributeError", "Traceback (most recent call last):\n  File \"run.py\", line 1\nAttributeError: 'NoneType' object has no attribute 'split'", "attributeerror"),
            ("ZeroDivisionError", "Traceback (most recent call last):\n  File \"run.py\", line 1\nZeroDivisionError: division by zero", "zerodivisionerror"),
            ("FileNotFoundError", "Traceback (most recent call last):\n  File \"run.py\", line 1\nFileNotFoundError: [Errno 2] No such file or directory: 'data.csv'", "filenotfounderror"),
            ("PermissionError", "Traceback (most recent call last):\n  File \"run.py\", line 1\nPermissionError: [Errno 13] Permission denied: '/root/secret'", "permissionerror"),
            ("RecursionError", "Traceback (most recent call last):\n  File \"run.py\", line 1\nRecursionError: maximum recursion depth exceeded", "recursionerror"),
            ("MemoryError", "Traceback (most recent call last):\n  File \"run.py\", line 1\nMemoryError: unable to allocate array", "memoryerror"),
            ("JSONDecodeError", "Traceback (most recent call last):\n  File \"run.py\", line 1\njson.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)", "jsondecodeerror"),
        ],
    )
    def test_specific_exception_extraction(self, exc_name, stderr, expected_class):
        diag = _classify_execution_failure(
            exit_code=1,
            stderr_text=stderr,
            status="error",
            timeout_value=30,
        )
        assert diag["classification"] == expected_class
        assert exc_name in diag["error"]
        assert len(diag["suggestion"]) > 10
