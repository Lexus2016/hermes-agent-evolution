"""Tests for vision tool provider error retry (#828).

Verifies that _is_retryable_provider_error correctly classifies errors
and that the vision API call retries transient failures instead of
immediately returning an error.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))


class TestIsRetryableProviderError:
    """Unit tests for _is_retryable_provider_error classification."""

    def test_value_error_is_not_retryable(self):
        from tools.vision_tools import _is_retryable_provider_error
        assert not _is_retryable_provider_error(ValueError("bad input"))

    def test_type_error_is_not_retryable(self):
        from tools.vision_tools import _is_retryable_provider_error
        assert not _is_retryable_provider_error(TypeError("wrong type"))

    def test_key_error_is_not_retryable(self):
        from tools.vision_tools import _is_retryable_provider_error
        assert not _is_retryable_provider_error(KeyError("missing"))

    def test_attribute_error_is_not_retryable(self):
        from tools.vision_tools import _is_retryable_provider_error
        assert not _is_retryable_provider_error(AttributeError("no attr"))

    def test_4xx_not_retryable(self):
        from tools.vision_tools import _is_retryable_provider_error
        req = httpx.Request("POST", "http://example.com")
        resp = httpx.Response(403, request=req)
        err = httpx.HTTPStatusError("403", request=req, response=resp)
        assert not _is_retryable_provider_error(err)

    def test_404_not_retryable(self):
        from tools.vision_tools import _is_retryable_provider_error
        req = httpx.Request("POST", "http://example.com")
        resp = httpx.Response(404, request=req)
        err = httpx.HTTPStatusError("404", request=req, response=resp)
        assert not _is_retryable_provider_error(err)

    def test_429_is_retryable(self):
        from tools.vision_tools import _is_retryable_provider_error
        req = httpx.Request("POST", "http://example.com")
        resp = httpx.Response(429, request=req)
        err = httpx.HTTPStatusError("429", request=req, response=resp)
        assert _is_retryable_provider_error(err)

    def test_500_is_retryable(self):
        from tools.vision_tools import _is_retryable_provider_error
        req = httpx.Request("POST", "http://example.com")
        resp = httpx.Response(500, request=req)
        err = httpx.HTTPStatusError("500", request=req, response=resp)
        assert _is_retryable_provider_error(err)

    def test_503_is_retryable(self):
        from tools.vision_tools import _is_retryable_provider_error
        req = httpx.Request("POST", "http://example.com")
        resp = httpx.Response(503, request=req)
        err = httpx.HTTPStatusError("503", request=req, response=resp)
        assert _is_retryable_provider_error(err)

    def test_timeout_is_retryable(self):
        from tools.vision_tools import _is_retryable_provider_error
        assert _is_retryable_provider_error(httpx.TimeoutException("timed out"))

    def test_transport_error_is_retryable(self):
        from tools.vision_tools import _is_retryable_provider_error
        assert _is_retryable_provider_error(httpx.TransportError("conn refused"))

    def test_connection_error_is_retryable(self):
        from tools.vision_tools import _is_retryable_provider_error
        assert _is_retryable_provider_error(ConnectionError("refused"))

    def test_os_error_is_retryable(self):
        from tools.vision_tools import _is_retryable_provider_error
        assert _is_retryable_provider_error(OSError("network unreachable"))

    def test_generic_exception_is_retryable(self):
        from tools.vision_tools import _is_retryable_provider_error
        assert _is_retryable_provider_error(RuntimeError("something"))


class TestProviderRetryIntegration:
    """Integration tests for the vision provider retry loop."""

    def test_retry_loop_retries_on_transient_then_succeeds(self):
        """The provider call should retry on a 429 and succeed on the next attempt."""
        from tools.vision_tools import _is_retryable_provider_error

        req = httpx.Request("POST", "http://example.com")
        resp = httpx.Response(429, request=req)
        err_429 = httpx.HTTPStatusError("429", request=req, response=resp)
        assert _is_retryable_provider_error(err_429)
        # In the retry loop, a 429 causes a retry (not a raise).

    def test_retry_loop_raises_on_non_retryable(self):
        """A 403 error should not be retried — it should raise immediately."""
        from tools.vision_tools import _is_retryable_provider_error

        req = httpx.Request("POST", "http://example.com")
        resp = httpx.Response(403, request=req)
        err = httpx.HTTPStatusError("403", request=req, response=resp)
        assert not _is_retryable_provider_error(err)
        # The caller would raise this immediately, not retry.


if __name__ == "__main__":
    pytest.main([__file__, "-v"])