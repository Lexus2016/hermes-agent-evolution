"""Tests for the MCP stdout JSONRPC-parse noise suppression (#1298).

The MCP SDK's ``stdout_reader`` logs ``logger.exception("Failed to parse
JSONRPC message from server")`` for every stdout line that is not valid
JSON-RPC, emitting a full pydantic ``ValidationError`` traceback at ERROR
level — ~300 occurrences/scan in production. The filter installed at import
time of ``tools.mcp_tool`` demotes that specific message to DEBUG and strips
the traceback. Genuine ERROR logs from the same logger pass through unchanged.
"""

from __future__ import annotations

import logging

import pytest

import tools.mcp_tool as mcp_tool  # noqa: F401  — import installs the filter

_MCP_STDIO_LOGGER = "mcp.client.stdio"
_MCP_JSONRPC_PARSE_MSG = "Failed to parse JSONRPC message from server"


def _has_filter(logger: logging.Logger) -> bool:
    return any(
        isinstance(f, mcp_tool._SuppressNonJsonStdoutTraceback) for f in logger.filters
    )


class TestFilterInstalled:
    def test_filter_attached_on_import(self):
        """Importing tools.mcp_tool must attach the suppression filter."""
        mcp_logger = logging.getLogger(_MCP_STDIO_LOGGER)
        assert _has_filter(mcp_logger)

    def test_install_is_idempotent(self):
        """Calling install twice must not stack duplicate filters."""
        before = len([
            f
            for f in logging.getLogger(_MCP_STDIO_LOGGER).filters
            if isinstance(f, mcp_tool._SuppressNonJsonStdoutTraceback)
        ])
        mcp_tool._install_mcp_stdout_noise_filter()
        mcp_tool._install_mcp_stdout_noise_filter()
        after = len([
            f
            for f in logging.getLogger(_MCP_STDIO_LOGGER).filters
            if isinstance(f, mcp_tool._SuppressNonJsonStdoutTraceback)
        ])
        assert before == 1
        assert after == 1


class TestSuppression:
    @pytest.fixture
    def capture(self, caplog):
        """caplog at DEBUG on the mcp.client.stdio logger."""
        caplog.set_level(logging.DEBUG, logger=_MCP_STDIO_LOGGER)
        return caplog

    def test_parse_error_demoted_to_debug_and_traceback_stripped(self, capture):
        """The known-noisy parse message drops to DEBUG and loses exc_info."""
        logger = logging.getLogger(_MCP_STDIO_LOGGER)
        try:
            raise ValueError("simulated pydantic validation noise")
        except ValueError:
            logger.exception(_MCP_JSONRPC_PARSE_MSG)

        matched = [r for r in capture.records if r.name == _MCP_STDIO_LOGGER]
        assert matched, "expected at least one log record"
        rec = matched[-1]
        assert rec.levelno == logging.DEBUG
        assert rec.levelname == "DEBUG"
        assert rec.exc_info is None
        assert rec.exc_text is None

    def test_unrelated_error_from_same_logger_passes_through(self, capture):
        """A genuine, unrelated ERROR log must keep its level and traceback."""
        logger = logging.getLogger(_MCP_STDIO_LOGGER)
        try:
            raise RuntimeError("a real transport failure")
        except RuntimeError:
            logger.exception("Transport closed unexpectedly")

        matched = [
            r
            for r in capture.records
            if r.name == _MCP_STDIO_LOGGER
            and r.getMessage() == "Transport closed unexpectedly"
        ]
        assert matched, "expected the unrelated error record"
        rec = matched[-1]
        assert rec.levelno == logging.ERROR
        assert rec.exc_info is not None

    def test_non_exception_log_of_parse_msg_also_demoted(self, capture):
        """Even a plain logger.error (no exc_info) of the message is demoted."""
        logger = logging.getLogger(_MCP_STDIO_LOGGER)
        logger.error(_MCP_JSONRPC_PARSE_MSG)

        matched = [
            r
            for r in capture.records
            if r.name == _MCP_STDIO_LOGGER and r.getMessage() == _MCP_JSONRPC_PARSE_MSG
        ]
        assert matched
        assert matched[-1].levelno == logging.DEBUG
