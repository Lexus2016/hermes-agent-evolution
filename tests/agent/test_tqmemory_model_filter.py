"""Tests for model-identity metadata, outcome tags, and utility-weighted retrieval (#2234, #3199, #3200, #3201)."""

import json

from agent.tqmemory_model_filter import (
    stamp_model_metadata,
    stamp_outcome_metadata,
    compute_note_utility,
    filter_by_model_family,
    rank_by_utility_and_family,
    record_outcome_feedback,
    is_tqmemory_write,
    is_tqmemory_read,
)


def test_stamp_adds_identity_and_family():
    result = stamp_model_metadata({"title": "t", "content": "c"}, "claude-sonnet-4-6")
    m = result["metadata"]
    assert m["model_identity"] == "claude-sonnet-4-6"
    assert m["model_family"] == "anthropic"


def test_stamp_preserves_existing_metadata():
    result = stamp_model_metadata({"metadata": {"custom": "val"}}, "gpt-5.5")
    assert result["metadata"]["custom"] == "val"
    assert result["metadata"]["model_family"] == "openai"


def test_stamp_no_model_is_unknown():
    result = stamp_model_metadata({"content": "x"}, None)
    assert result["metadata"]["model_family"] == "unknown"


def test_stamp_outcome_valid():
    result = stamp_outcome_metadata({"title": "t"}, "helped")
    assert result["metadata"]["outcome"] == "helped"
    assert result["metadata"]["utility"] == 1.25

    result_misled = stamp_outcome_metadata({"outcome": "misled"})
    assert result_misled["metadata"]["outcome"] == "misled"
    assert result_misled["metadata"]["utility"] == 0.2


def test_stamp_outcome_invalid_ignored():
    result = stamp_outcome_metadata({"title": "t"}, "invalid_tag")
    assert "outcome" not in result["metadata"]


def test_compute_note_utility():
    assert compute_note_utility({"outcome": "helped"}) == 1.25
    assert compute_note_utility({"outcome": "neutral"}) == 1.0
    assert compute_note_utility({"outcome": "misled"}) == 0.2
    assert compute_note_utility({"utility": 0.8}) == 0.8
    assert compute_note_utility({}) == 1.0
    assert compute_note_utility(None) == 1.0


def test_filter_downweights_cross_family():
    entries = [
        {"id": 1, "relevance": 1.0, "metadata": {"model_family": "openai"}},
        {"id": 2, "relevance": 1.0, "metadata": {"model_family": "anthropic"}},
    ]
    result = filter_by_model_family(json.dumps({"notes": entries}), "claude-sonnet-4-6")
    notes = json.loads(result)["notes"]
    assert notes[0]["id"] == 2 and notes[0]["relevance"] == 1.0  # same family first
    assert notes[1]["id"] == 1 and notes[1]["relevance"] == 0.5  # cross-family penalized


def test_rank_by_utility_and_family():
    entries = [
        {"id": 1, "relevance": 1.0, "metadata": {"outcome": "misled"}},
        {"id": 2, "relevance": 1.0, "metadata": {"outcome": "helped"}},
        {"id": 3, "relevance": 1.0, "metadata": {"outcome": "neutral"}},
    ]
    result = rank_by_utility_and_family(json.dumps({"notes": entries}))
    notes = json.loads(result)["notes"]
    # helped (1.25) > neutral (1.0) > misled (0.2)
    assert [n["id"] for n in notes] == [2, 3, 1]
    assert notes[0]["relevance"] == 1.25
    assert notes[1]["relevance"] == 1.0
    assert notes[2]["relevance"] == 0.2


def test_record_outcome_feedback():
    payload = record_outcome_feedback("note-123", "helped", {"session": "s1"})
    assert payload["id"] == "note-123"
    assert payload["outcome"] == "helped"
    assert payload["metadata"]["utility"] == 1.25
    assert payload["metadata"]["session"] == "s1"

    payload_invalid = record_outcome_feedback("note-456", "bad-value")
    assert payload_invalid["outcome"] == "neutral"


def test_filter_passthrough_unknown_metadata():
    entries = [{"id": 1, "relevance": 1.0, "metadata": {}}]
    result = filter_by_model_family(json.dumps({"notes": entries}), "gpt-5.5")
    assert json.loads(result)["notes"][0]["relevance"] == 1.0


def test_filter_malformed_json_is_noop():
    assert filter_by_model_family("not json {{{", "gpt-5.5") == "not json {{{"


def test_filter_no_consumer_model_is_noop():
    entries = [{"id": 1, "relevance": 1.0, "metadata": {"model_family": "openai"}}]
    result = filter_by_model_family(json.dumps({"notes": entries}), None)
    assert json.loads(result)["notes"][0]["relevance"] == 1.0


def test_filter_no_entries_key_is_noop():
    result = filter_by_model_family(json.dumps({"status": "ok"}), "gpt-5.5")
    assert json.loads(result) == {"status": "ok"}


def test_tool_name_detection():
    assert is_tqmemory_write("mcp_tqmemory_remember_note")
    assert not is_tqmemory_write("mcp_tqmemory_recent_context")
    assert is_tqmemory_read("mcp_tqmemory_recent_context")
    assert not is_tqmemory_read("mcp_tqmemory_remember_note")

