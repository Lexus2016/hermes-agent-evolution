"""Tests for agent.context_load_instrument (issue #2442, Slice A)."""

import pytest

import agent.context_load_instrument as inst
from agent.context_load_instrument import (
    ContextLoadRecorder,
    LoadEvent,
    disable_recording,
    drain_events,
    enable_recording,
    is_recording,
    loaded_paths,
    record_context_load,
    snapshot_events,
)


@pytest.fixture(autouse=True)
def _clean_recorder():
    disable_recording()
    yield
    disable_recording()


def test_record_is_noop_when_disabled():
    record_context_load("/x/AGENTS.md", inst.KIND_AGENTS, chars=10)
    assert snapshot_events() == []
    assert not is_recording()


def test_record_and_drain():
    enable_recording()
    record_context_load("/x/AGENTS.md", inst.KIND_AGENTS, chars=10)
    events = drain_events()
    assert len(events) == 1
    assert events[0].path == "/x/AGENTS.md"
    assert events[0].kind == inst.KIND_AGENTS
    assert events[0].chars == 10
    # drain clears
    assert drain_events() == []


def test_snapshot_does_not_clear():
    enable_recording()
    record_context_load("/x/CLAUDE.md", inst.KIND_CLAUDE, chars=5)
    assert len(snapshot_events()) == 1
    assert len(snapshot_events()) == 1  # still there


def test_unknown_kind_is_preserved_as_unknown():
    enable_recording()
    record_context_load("/x/foo", "bogus-kind", chars=1)
    assert snapshot_events()[0].kind == "unknown"


def test_loaded_paths_filters_by_kind_and_skips_unloaded():
    enable_recording()
    record_context_load("/a/AGENTS.md", inst.KIND_AGENTS, chars=1)
    record_context_load("/b/CLAUDE.md", inst.KIND_CLAUDE, chars=1)
    record_context_load("/c/AGENTS.md", inst.KIND_AGENTS, loaded=False)
    # loaded_paths excludes un-loaded and skipped, de-dups, and filters by kind
    assert loaded_paths() == ["/a/AGENTS.md", "/b/CLAUDE.md"]
    assert loaded_paths(inst.KIND_AGENTS) == ["/a/AGENTS.md"]


def test_recorder_is_bounded():
    r = ContextLoadRecorder()
    for i in range(r._MAX_EVENTS + 100):
        r.record(LoadEvent(path=f"/x/{i}", kind=inst.KIND_AGENTS, chars=1))
    assert len(r.snapshot()) == r._MAX_EVENTS


def test_disable_clears_buffer():
    enable_recording()
    record_context_load("/x/AGENTS.md", inst.KIND_AGENTS, chars=1)
    disable_recording()
    assert snapshot_events() == []
    assert not is_recording()


def test_record_never_raises_on_bad_input():
    enable_recording()
    # chars may be non-numeric in the wild; the recorder coerces, never raises.
    record_context_load(None, inst.KIND_AGENTS, chars="not-a-number")
    ev = snapshot_events()[0]
    assert ev.chars == 0
    assert ev.path == "None"
