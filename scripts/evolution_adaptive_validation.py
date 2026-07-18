#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Adaptive validation agent for the evolution verification stage (issue #1164).

Senior SWE-Bench (Snorkel AI + Princeton + UW–Madison, 2026) moved verification
from pre-written checks to an **adaptive validation agent**: given a submitted
patch + an abstract *validation specification* (testing recipes, not over-specified
steps), an LLM writes test scripts adapted to the submitted solution's actual
interfaces, runs them across cases, and a judge can request a revision round.
Because newer models exhibit benchmark-awareness/reward-hacking, two safeguards
are mandatory: **run-vs-reference** (the test must fail on the pre-change state)
and **empty-solution** (the test must fail on a trivial/empty solution) — any
result that is trivially satisfied is discarded.

This complements (does not replace) the existing pre-written shard runner
(``evolution_pre_pr_test_runner`` #580): fast deterministic checks stay; this
handles the long tail where the change's interface differs from what a pre-written
check assumed.

Design: the LLM step is an **injectable** ``llm_call`` (stub in tests, model in
production) and the execution step is an injectable ``runner`` — so the module is
fully testable without a live model or a real sandbox. The safeguard/verdict logic
is pure and deterministic. No side effects on import.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    "ValidationSpec",
    "GeneratedTest",
    "TestResult",
    "ValidationVerdict",
    "generate_tests",
    "apply_safeguards",
    "validate",
    "main",
]

# runner: (test_source, solution_state) -> passed?  solution_state is one of
# "solution" | "reference" | "empty" so the runner can execute the test against
# the right code state.
Runner = Callable[[str, str], bool]
LLMCall = Callable[[str], str]


@dataclass(frozen=True)
class ValidationSpec:
    """Abstract testing recipes for a change-class (human-auditable, versioned)."""

    behavior: str  # what must hold, in plain language
    recipes: tuple[str, ...] = ()  # representative testing recipes / cases
    change_class: str = "default"

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ValidationSpec":
        return cls(
            behavior=str(d.get("behavior", "")),
            recipes=tuple(str(r) for r in d.get("recipes", [])),
            change_class=str(d.get("change_class", "default")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"behavior": self.behavior, "recipes": list(self.recipes), "change_class": self.change_class}


@dataclass(frozen=True)
class GeneratedTest:
    recipe: str
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {"recipe": self.recipe, "source": self.source}


@dataclass(frozen=True)
class TestResult:
    recipe: str
    passed_on_solution: bool
    passed_on_reference: bool
    passed_on_empty: bool

    @property
    def discriminating(self) -> bool:
        """A test is only meaningful if it PASSES on the solution and FAILS on
        both the pre-change reference and an empty/trivial solution."""
        return self.passed_on_solution and not self.passed_on_reference and not self.passed_on_empty

    def to_dict(self) -> dict[str, Any]:
        return {
            "recipe": self.recipe,
            "passed_on_solution": self.passed_on_solution,
            "passed_on_reference": self.passed_on_reference,
            "passed_on_empty": self.passed_on_empty,
            "discriminating": self.discriminating,
        }


@dataclass(frozen=True)
class ValidationVerdict:
    results: tuple[TestResult, ...] = ()

    @property
    def discriminating_results(self) -> list[TestResult]:
        return [r for r in self.results if r.discriminating]

    @property
    def discarded_count(self) -> int:
        return len(self.results) - len(self.discriminating_results)

    @property
    def passed(self) -> bool:
        """The change validates only if there is at least one discriminating test
        and ALL discriminating tests pass on the solution."""
        disc = self.discriminating_results
        return bool(disc) and all(r.passed_on_solution for r in disc)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "discriminating": len(self.discriminating_results),
            "discarded_trivial": self.discarded_count,
            "results": [r.to_dict() for r in self.results],
        }


def _build_prompt(spec: ValidationSpec, solution: str, recipe: str) -> str:
    return (
        "You are a validation agent. Write ONE self-contained test script that "
        "verifies the behavior below, adapted to the SUBMITTED solution's actual "
        "interfaces (not a fixed pre-written interface). The test must fail if the "
        "behavior does not hold.\n\n"
        f"Behavior that must hold: {spec.behavior}\n"
        f"Testing recipe: {recipe}\n\n"
        f"Submitted solution (for interface adaptation):\n{solution[:4000]}\n"
    )


def generate_tests(spec: ValidationSpec, solution: str, llm_call: LLMCall) -> list[GeneratedTest]:
    """Generate one adapted test per recipe via the injected ``llm_call``.

    Never raises — a failed/empty model reply for a recipe is skipped.
    """
    recipes = spec.recipes or (spec.behavior,)
    tests: list[GeneratedTest] = []
    for recipe in recipes:
        try:
            source = llm_call(_build_prompt(spec, solution, recipe)) or ""
        except Exception:
            source = ""
        if source.strip():
            tests.append(GeneratedTest(recipe=recipe, source=source))
    return tests


def apply_safeguards(tests: Sequence[GeneratedTest], runner: Runner) -> ValidationVerdict:
    """Run each test against solution / reference / empty and keep only the
    discriminating ones (fail on reference AND empty, pass on solution)."""
    results: list[TestResult] = []
    for t in tests:
        def _run(state: str) -> bool:
            try:
                return bool(runner(t.source, state))
            except Exception:
                return False

        results.append(
            TestResult(
                recipe=t.recipe,
                passed_on_solution=_run("solution"),
                passed_on_reference=_run("reference"),
                passed_on_empty=_run("empty"),
            )
        )
    return ValidationVerdict(results=tuple(results))


def validate(spec: ValidationSpec, solution: str, llm_call: LLMCall, runner: Runner) -> ValidationVerdict:
    """End-to-end: generate adapted tests, run them, apply safeguards, return verdict."""
    tests = generate_tests(spec, solution, llm_call)
    return apply_safeguards(tests, runner)


def main(argv: list[str] | None = None) -> int:
    # CLI is a thin harness: it cannot run real LLM/sandbox here, so it validates
    # a spec file's shape and prints it (the executable path is used from Python
    # with injected llm_call/runner). This keeps the module importable + testable.
    parser = argparse.ArgumentParser(description="Adaptive validation spec inspector (#1164)")
    parser.add_argument("--spec", required=True, help="path to a validation_spec JSON")
    args = parser.parse_args(argv)
    with open(args.spec, encoding="utf-8") as fh:
        spec = ValidationSpec.from_dict(json.load(fh))
    print(json.dumps(spec.to_dict(), ensure_ascii=False, indent=2))
    return 0 if spec.behavior.strip() else 2


if __name__ == "__main__":
    sys.exit(main())
