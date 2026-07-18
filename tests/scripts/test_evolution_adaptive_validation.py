# -*- coding: utf-8 -*-
"""Tests for scripts/evolution_adaptive_validation.py (#1164)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import evolution_adaptive_validation as av  # noqa: E402


def _spec():
    return av.ValidationSpec(
        behavior="function f returns the doubled input",
        recipes=("f(2) == 4", "f(0) == 0"),
        change_class="pure-function",
    )


def test_generate_tests_one_per_recipe_and_injects_context():
    seen = []

    def llm(prompt):
        seen.append(prompt)
        return "def test():\n    assert real"

    tests = av.generate_tests(_spec(), "def f(x): return x*2", llm)
    assert len(tests) == 2
    # solution + behavior are woven into the prompt for interface adaptation
    assert "returns the doubled input" in seen[0]
    assert "def f(x): return x*2" in seen[0]


def test_generate_tests_skips_empty_llm_reply():
    tests = av.generate_tests(_spec(), "sol", lambda p: "")
    assert tests == []


def test_generate_tests_handles_llm_error():
    def boom(p):
        raise RuntimeError("model down")

    assert av.generate_tests(_spec(), "sol", boom) == []


def test_discriminating_test_passes_solution_fails_reference_and_empty():
    tests = [av.GeneratedTest("f(2)==4", "assert f(2)==4")]

    def runner(source, state):
        # passes only against the real solution
        return state == "solution"

    verdict = av.apply_safeguards(tests, runner)
    assert verdict.passed is True
    assert len(verdict.discriminating_results) == 1
    assert verdict.discarded_count == 0


def test_trivially_satisfied_test_is_discarded():
    tests = [av.GeneratedTest("trivial", "assert True")]

    def runner(source, state):
        return True  # passes everywhere -> not discriminating

    verdict = av.apply_safeguards(tests, runner)
    assert verdict.discarded_count == 1
    assert verdict.discriminating_results == []
    assert verdict.passed is False  # no discriminating test -> not validated


def test_test_that_passes_on_reference_is_discarded():
    tests = [av.GeneratedTest("weak", "assert something")]

    def runner(source, state):
        return state in ("solution", "reference")  # not fixed by the change

    verdict = av.apply_safeguards(tests, runner)
    assert verdict.discriminating_results == []


def test_runner_error_counts_as_fail():
    tests = [av.GeneratedTest("r", "src")]

    def runner(source, state):
        if state == "solution":
            return True
        raise RuntimeError("sandbox error")

    verdict = av.apply_safeguards(tests, runner)
    # reference/empty errored -> treated as fail -> the test is discriminating
    assert verdict.discriminating_results and verdict.passed is True


def test_validate_end_to_end():
    def llm(p):
        return "assert f(2)==4"

    def runner(source, state):
        return state == "solution"

    verdict = av.validate(_spec(), "def f(x): return x*2", llm, runner)
    assert verdict.passed is True


def test_spec_roundtrip():
    d = _spec().to_dict()
    spec2 = av.ValidationSpec.from_dict(d)
    assert spec2.behavior == _spec().behavior
    assert spec2.recipes == _spec().recipes
