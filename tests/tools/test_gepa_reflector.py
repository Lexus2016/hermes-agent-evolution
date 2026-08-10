"""Tests for gepa_reflector (issue #2231, Slice A)."""

import json

import pytest

from tools.gepa_reflector import (
    Critique,
    VariantResult,
    reflect,
    reflect_to_json,
    store_critiques,
)


def _mixed_results():
    return [
        VariantResult(variant="v1", task="t1", passed=True, task_data={"n": 1}),
        VariantResult(variant="v2", task="t1", passed=False, task_data={"n": 2}),
        VariantResult(variant="v1", task="t2", passed=False, task_data={}),
    ]


def test_reflect_produces_nonempty_critiques_for_mixed_set():
    critiques = reflect(_mixed_results())
    assert len(critiques) == 3
    for c in critiques:
        assert isinstance(c, Critique)
        assert c.critique.strip()  # non-empty
        assert c.signals  # has at least one signal


def test_reflect_preserves_order_and_pass_fail():
    critiques = reflect(_mixed_results())
    assert [c.passed for c in critiques] == [True, False, False]
    assert [c.variant for c in critiques] == ["v1", "v2", "v1"]
    assert [c.task for c in critiques] == ["t1", "t1", "t2"]


def test_reflect_success_critique_mentions_success():
    critiques = reflect([VariantResult(variant="v1", task="t1", passed=True)])
    assert "succeeded" in critiques[0].critique
    assert "success" in critiques[0].signals


def test_reflect_failure_critique_mentions_failure():
    critiques = reflect([VariantResult(variant="v1", task="t1", passed=False)])
    assert "failed" in critiques[0].critique
    assert "failure" in critiques[0].signals


def test_reflect_with_llm_callback():
    def fake_llm(results):
        return [
            Critique(
                variant=r.variant,
                task=r.task,
                passed=r.passed,
                critique=f"LLM critique for {r.variant}/{r.task}",
                signals=["llm"],
            )
            for r in results
        ]

    critiques = reflect(_mixed_results(), llm=fake_llm)
    assert critiques[0].critique == "LLM critique for v1/t1"
    assert critiques[0].signals == ["llm"]


def test_reflect_llm_failure_falls_back_to_deterministic():
    def broken_llm(results):
        raise RuntimeError("LLM down")

    critiques = reflect(_mixed_results(), llm=broken_llm)
    assert len(critiques) == 3
    assert all(c.critique.strip() for c in critiques)


def test_reflect_llm_wrong_count_falls_back():
    def short_llm(results):
        return [Critique(variant="x", task="y", passed=True, critique="only one")]

    critiques = reflect(_mixed_results(), llm=short_llm)
    assert len(critiques) == 3  # fell back to deterministic


def test_reflect_to_json_serializes():
    data = json.loads(reflect_to_json(_mixed_results()))
    assert len(data) == 3
    assert data[0]["variant"] == "v1"
    assert "critique" in data[0]


def test_store_critiques_appends_jsonl(tmp_path):
    out = tmp_path / "critiques.jsonl"
    store_critiques(reflect(_mixed_results()), str(out))
    lines = out.read_text().strip().splitlines()
    assert len(lines) == 3
    first = json.loads(lines[0])
    assert first["variant"] == "v1"
    assert "critique" in first
