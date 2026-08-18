# -*- coding: utf-8 -*-
"""Unit tests for SkillProx Slice 1 — re-execution verify primitive (#2777)."""

from evolution.lib.skill_prox import ReExecutionVerdict, verify_skill_edit


def _runner(body: str, inp: int) -> bool:
    """A runner that passes when the body contains the input as a digit."""
    return str(inp) in body


def test_passes_when_edit_reruns_clean_on_all_inputs():
    body = "handle 1"
    edit = lambda b: b + " and 2"  # noqa: E731
    verdict = verify_skill_edit(body, edit, [1, 2], _runner)
    assert verdict.passed is True
    assert verdict.per_input == {"0": True, "1": True}
    assert "and 2" in verdict.edited_body


def test_fails_when_any_input_fails_on_rerun():
    body = "handle 1"
    edit = lambda b: b + " and 2"  # noqa: E731
    verdict = verify_skill_edit(body, edit, [1, 2, 3], _runner)
    assert verdict.passed is False
    assert verdict.per_input["2"] is False  # input 3 not handled


def test_edit_that_raises_is_a_failure_not_a_crash():
    def bad_edit(b: str) -> str:
        raise RuntimeError("boom")

    verdict = verify_skill_edit("x", bad_edit, [1], _runner)
    assert verdict.passed is False
    assert "boom" in (verdict.error or "")


def test_runner_that_raises_is_a_per_input_failure():
    def flaky_runner(body: str, inp: int) -> bool:
        if inp == 2:
            raise ValueError("nope")
        return True

    verdict = verify_skill_edit("x", lambda b: b, [1, 2], flaky_runner)
    assert verdict.passed is False
    assert verdict.per_input == {"0": True, "1": False}


def test_empty_batch_is_not_a_pass():
    verdict = verify_skill_edit("x", lambda b: b, [], _runner)
    assert verdict.passed is False


def test_input_keys_are_used_when_provided():
    verdict = verify_skill_edit("1", lambda b: b, [1], _runner, input_keys=["a"])
    assert verdict.per_input == {"a": True}


def test_verdict_is_a_dataclass():
    v = ReExecutionVerdict(passed=True)
    assert v.per_input == {} and v.edited_body == "" and v.error is None
