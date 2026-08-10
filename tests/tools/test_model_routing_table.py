"""Tests for model_routing_table (issue #2257, Slice A)."""

import random

import pytest

from tools.model_routing_table import (
    DEFAULT_EPSILON,
    RoutingTable,
    classify_task,
)


def test_classify_task_explicit_dimension():
    assert classify_task({"dimension": "coding"}) == "coding"
    assert classify_task({"dimension": "reasoning"}) == "reasoning"


def test_classify_task_heuristic():
    assert classify_task({"type": "fix a python bug"}) == "coding"
    assert classify_task({"tags": ["math", "proof"]}) == "reasoning"
    assert classify_task({"type": "write a poem"}) == "creative"
    assert classify_task({"tags": ["shell", "terminal"]}) == "tool-use"
    assert classify_task({"type": "something else"}) == "general"


def test_record_outcome_tracks_attempts_and_successes():
    table = RoutingTable(models=["model-a", "model-b"])
    table.record_outcome("model-a", "coding", True)
    table.record_outcome("model-a", "coding", False)
    table.record_outcome("model-a", "coding", True)
    rec = table._records["model-a::coding"]
    assert rec.attempts == 3
    assert rec.successes == 2
    assert rec.success_rate == pytest.approx(2 / 3)


def test_best_model_prefers_higher_success_rate():
    table = RoutingTable(models=["model-a", "model-b"])
    table.record_outcome("model-a", "coding", True)
    table.record_outcome("model-b", "coding", False)
    assert table.best_model("coding") == "model-a"


def test_best_model_returns_none_without_signal():
    table = RoutingTable(models=["model-a"])
    assert table.best_model("coding") is None


def test_select_model_exploits_best_with_zero_epsilon():
    table = RoutingTable(models=["model-a", "model-b"], epsilon=0.0)
    table.record_outcome("model-a", "coding", True)
    table.record_outcome("model-b", "coding", False)
    assert table.select_model({"type": "fix a bug"}) == "model-a"


def test_select_model_explores_with_epsilon_one():
    # epsilon=1.0 -> always random. With a seeded rng, verify it picks from
    # the model set (and is not always the best).
    table = RoutingTable(models=["model-a", "model-b"], epsilon=1.0, rng=random.Random(42))
    table.record_outcome("model-a", "coding", True)
    table.record_outcome("model-b", "coding", False)
    picks = {table.select_model({"type": "fix a bug"}) for _ in range(50)}
    assert picks <= {"model-a", "model-b"}
    assert len(picks) > 1  # exploration actually varies


def test_select_model_cold_start_explores():
    # No signal on a dimension -> picks a model (exploration).
    table = RoutingTable(models=["model-a", "model-b"], epsilon=0.0)
    assert table.select_model({"type": "brand new task"}) in {"model-a", "model-b"}


def test_select_model_returns_none_with_no_models():
    table = RoutingTable(models=[])
    assert table.select_model({"type": "anything"}) is None


def test_roundtrip_serialization():
    table = RoutingTable(models=["model-a", "model-b"], epsilon=0.2)
    table.record_outcome("model-a", "coding", True)
    table.record_outcome("model-b", "reasoning", False)
    restored = RoutingTable.from_dict(table.to_dict())
    assert restored.models == table.models
    assert restored.epsilon == pytest.approx(0.2)
    assert restored._records["model-a::coding"].successes == 1
    assert restored._records["model-b::reasoning"].attempts == 1
