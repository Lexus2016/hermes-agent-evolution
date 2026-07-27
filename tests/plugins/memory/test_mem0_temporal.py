"""Tests for temporal memory metadata (issue #1289).

Validates the behaviors the slice ships:
  1. Every write stamps ``valid_from``.
  2. ``mem0_search`` filters out superseded (``valid_until`` elapsed) entries.
  3. ``mem0_update`` marks the old row superseded AND appends a fresh entry
     carrying a ``supersedes`` provenance pointer.
  4. ``as_of`` parameter performs point-in-time retrieval.
  5. Legacy entries (no temporal metadata) pass through unchanged.
  6. ``_parse_iso_epoch`` is timezone-correct (the #1289 rework regression —
     ``time.mktime`` was used instead of ``calendar.timegm``, silently no-oping
     the feature on every non-UTC host).
"""

import json
import time

import pytest

from plugins.memory.mem0 import (
    Mem0MemoryProvider,
    _is_current,
    _now_iso,
    _parse_iso_epoch,
    _temporal_metadata,
    _META_VALID_FROM,
    _META_VALID_UNTIL,
    _META_SUPERSEDES,
)
from tests.plugins.memory.test_mem0_v3 import FakeBackend


class TestTemporalHelpers:
    """Unit tests for the pure temporal helper functions."""

    def test_now_iso_format(self):
        ts = _now_iso()
        assert ts.endswith("Z")
        # Round-trips through the parser.
        assert _parse_iso_epoch(ts) is not None

    def test_temporal_metadata_adds_valid_from(self):
        meta = _temporal_metadata({"channel": "telegram"})
        assert meta["channel"] == "telegram"
        assert _META_VALID_FROM in meta
        assert _META_SUPERSEDES not in meta

    def test_temporal_metadata_supersedes_pointer(self):
        meta = _temporal_metadata(supersedes="old-id-123")
        assert meta[_META_SUPERSEDES] == "old-id-123"

    def test_temporal_metadata_empty_existing(self):
        meta = _temporal_metadata(None)
        assert _META_VALID_FROM in meta

    def test_is_current_legacy_entry_no_metadata(self):
        # Entries predating #1289 carry no temporal fields → still current.
        assert _is_current({"id": "x", "memory": "foo"}) is True

    def test_is_current_legacy_entry_empty_metadata(self):
        assert _is_current({"id": "x", "memory": "foo", "metadata": {}}) is True

    def test_is_current_open_validity(self):
        entry = {"metadata": {_META_VALID_FROM: _now_iso()}}
        assert _is_current(entry) is True

    def test_is_current_expired_valid_until_excludes_now(self):
        # valid_until in the past → not current.
        past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
        entry = {"metadata": {_META_VALID_FROM: past, _META_VALID_UNTIL: past}}
        assert _is_current(entry) is False

    def test_is_current_future_valid_until_includes_now(self):
        future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
        entry = {"metadata": {_META_VALID_FROM: _now_iso(), _META_VALID_UNTIL: future}}
        assert _is_current(entry) is True

    def test_is_current_as_of_before_valid_from(self):
        # Entry that becomes valid in the future, queried as-of now.
        future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
        entry = {"metadata": {_META_VALID_FROM: future}}
        now = time.time()
        assert _is_current(entry, as_of=now) is False

    def test_is_current_as_of_in_window(self):
        # Entry valid in the past hour, queried as-of 30 minutes ago.
        now = time.time()
        valid_from = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600))
        entry = {"metadata": {_META_VALID_FROM: valid_from}}
        assert _is_current(entry, as_of=now - 1800) is True

    def test_is_current_malformed_timestamp_fails_open(self):
        entry = {"metadata": {_META_VALID_UNTIL: "not-a-date"}}
        # Malformed → fail open (treat as current).
        assert _is_current(entry) is True

    def test_parse_iso_epoch_returns_none_on_garbage(self):
        assert _parse_iso_epoch("") is None
        assert _parse_iso_epoch("garbage") is None


class TestParseIsoEpochTimezone:
    """Regression tests for the #1289 rework: _parse_iso_epoch must be
    timezone-correct. The original implementation used ``time.mktime`` which
    interprets the struct_time in LOCAL time; but ``_now_iso`` writes UTC via
    ``time.gmtime``. On a non-UTC host the epoch was off by the UTC offset,
    silently no-oping the feature (superseded entries not filtered) or
    wrongly excluding current entries. The fix uses ``calendar.timegm``
    which treats the struct as UTC. These tests force non-UTC TZs so the bug
    can never regress — under the buggy code, the round-trip epoch would be
    off by hours, not seconds.
    """

    @pytest.mark.parametrize("tz", ["UTC", "America/New_York", "Asia/Tokyo"])
    def test_roundtrip_within_2s_of_time_time(self, tz, monkeypatch):
        # _now_iso writes UTC; parsing it back must land within ~2s of the
        # true UTC epoch regardless of host TZ.
        monkeypatch.setenv("TZ", tz)
        try:
            time.tzset()
        except AttributeError:
            pytest.skip("time.tzset not available on this platform")
        before = time.time()
        iso = _now_iso()
        after = time.time()
        parsed = _parse_iso_epoch(iso)
        assert parsed is not None
        # parsed must fall in the [before, after] window (both are UTC epochs).
        assert before - 2 <= parsed <= after + 2, (
            f"TZ={tz}: parsed epoch {parsed} outside [{before}, {after}] — "
            f"TZ-correctness bug in _parse_iso_epoch"
        )

    @pytest.mark.parametrize("tz", ["America/New_York", "Asia/Tokyo"])
    def test_expired_entry_filtered_under_nonutc_tz(self, tz, monkeypatch):
        # The core symptom: a superseded entry (valid_until in the past) must
        # be filtered out even on a non-UTC host. Under the buggy mktime code,
        # the offset could flip the comparison so the entry was NOT filtered.
        monkeypatch.setenv("TZ", tz)
        try:
            time.tzset()
        except AttributeError:
            pytest.skip("time.tzset not available on this platform")
        now = time.time()
        past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600))
        entry = {"metadata": {_META_VALID_FROM: past, _META_VALID_UNTIL: past}}
        assert _is_current(entry) is False, (
            f"TZ={tz}: expired entry not filtered — _parse_iso_epoch TZ bug"
        )


class TestSearchAsOfFilter:
    """mem0_search filters by temporal validity."""

    def _make_provider(self, backend):
        provider = Mem0MemoryProvider()
        provider._user_id = "u123"
        provider._agent_id = "hermes"
        provider._backend = backend
        return provider

    def test_search_filters_expired_entry(self):
        past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
        results = [
            {
                "id": "current",
                "memory": "fresh fact",
                "score": 0.9,
                "metadata": {_META_VALID_FROM: _now_iso()},
            },
            {
                "id": "stale",
                "memory": "old fact",
                "score": 0.8,
                "metadata": {_META_VALID_FROM: past, _META_VALID_UNTIL: past},
            },
        ]
        backend = FakeBackend(search_results=results)
        provider = self._make_provider(backend)
        out = json.loads(provider.handle_tool_call("mem0_search", {"query": "x"}))
        ids = [r["id"] for r in out["results"]]
        assert "current" in ids
        assert "stale" not in ids

    def test_search_legacy_entries_pass_through(self):
        results = [
            {"id": "legacy", "memory": "no metadata", "score": 0.9},
        ]
        backend = FakeBackend(search_results=results)
        provider = self._make_provider(backend)
        out = json.loads(provider.handle_tool_call("mem0_search", {"query": "x"}))
        assert len(out["results"]) == 1
        assert out["results"][0]["id"] == "legacy"

    def test_search_returns_temporal_fields_in_response(self):
        vf = _now_iso()
        results = [
            {
                "id": "m1",
                "memory": "fact",
                "score": 0.9,
                "metadata": {_META_VALID_FROM: vf},
            },
        ]
        backend = FakeBackend(search_results=results)
        provider = self._make_provider(backend)
        out = json.loads(provider.handle_tool_call("mem0_search", {"query": "x"}))
        assert out["results"][0]["valid_from"] == vf

    def test_search_as_of_parameter_filters_future_entries(self):
        future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
        results = [
            {
                "id": "future",
                "memory": "not yet valid",
                "score": 0.9,
                "metadata": {_META_VALID_FROM: future},
            },
        ]
        backend = FakeBackend(search_results=results)
        provider = self._make_provider(backend)
        out = json.loads(
            provider.handle_tool_call(
                "mem0_search", {"query": "x", "as_of": time.time()}
            )
        )
        assert out["count"] == 0


class TestUpdateSupersession:
    """mem0_update stamps valid_until on the old row + appends a fresh entry."""

    def _make_provider(self, backend):
        provider = Mem0MemoryProvider()
        provider._user_id = "u123"
        provider._agent_id = "hermes"
        provider._backend = backend
        return provider

    def test_update_stamps_valid_until_on_old_row(self):
        backend = FakeBackend()
        provider = self._make_provider(backend)
        provider.handle_tool_call(
            "mem0_update", {"memory_id": "mem-1", "text": "new fact"}
        )
        update_call = backend.captured[0]
        assert update_call[0] == "update"
        assert _META_VALID_UNTIL in update_call[3]["metadata"]

    def test_update_appends_superseding_entry(self):
        backend = FakeBackend()
        provider = self._make_provider(backend)
        provider.handle_tool_call(
            "mem0_update", {"memory_id": "mem-1", "text": "new fact"}
        )
        add_call = backend.captured[1]
        assert add_call[0] == "add"
        assert add_call[2]["metadata"][_META_SUPERSEDES] == "mem-1"
        assert _META_VALID_FROM in add_call[2]["metadata"]
