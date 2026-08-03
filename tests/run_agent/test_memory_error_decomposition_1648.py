"""Tests for issue #1648 (memory 'other' error decomposition).

The memory tool historically returned opaque "other" errors — the dominant
memory failure bucket (98%) that drove blind retry spirals. This asserts that
unexpected exceptions are decomposed into structured ``reason`` categories
with recovery directives so the model can act instead of looping.
"""

import json

from tools.memory_tool import (
    _classify_memory_failure,
    _enrich_memory_error,
    memory_tool,
)


class TestMemoryFailureClassification:
    def test_connection_timeout(self):
        tag = _classify_memory_failure(TimeoutError("file lock timed out"))
        assert tag["reason"] == "connection-timeout"
        assert "retry" in tag["recovery"].lower()

    def test_serialization_error(self):
        tag = _classify_memory_failure(ValueError("cannot serialize JSON"))
        assert tag["reason"] == "serialization-error"

    def test_schema_mismatch(self):
        tag = _classify_memory_failure(TypeError("invalid argument type"))
        assert tag["reason"] == "schema-mismatch"

    def test_capacity_exceeded(self):
        tag = _classify_memory_failure(OSError("memory limit exceeded"))
        assert tag["reason"] == "capacity-exceeded"
        assert "consolidate" in tag["recovery"].lower()

    def test_unknown_falls_back_to_other(self):
        tag = _classify_memory_failure(RuntimeError("some opaque thing"))
        assert tag["reason"] == "other"
        assert "do not blindly retry" in tag["recovery"].lower()

    def test_enrich_memory_error_is_structured(self):
        payload = json.loads(_enrich_memory_error(TimeoutError("timed out")))
        assert payload["success"] is False
        assert payload["reason"] == "connection-timeout"
        assert "Recovery:" in payload["error"]


class TestMemoryToolDispatchEnrichment:
    def test_unexpected_exception_is_decomposed(self):
        """An exception escaping the store dispatch is returned as a
        structured reason instead of crashing or an opaque 'other'."""

        class _BoomStore:
            def add(self, *a, **k):
                raise TimeoutError("connection reset during write")

            def replace(self, *a, **k):  # pragma: no cover
                raise AssertionError

            def remove(self, *a, **k):  # pragma: no cover
                raise AssertionError

        result = memory_tool(
            action="add", content="x", store=_BoomStore(), target="memory"
        )
        payload = json.loads(result)
        assert payload["success"] is False
        assert payload["reason"] == "connection-timeout"
        assert "Recovery:" in payload["error"]
