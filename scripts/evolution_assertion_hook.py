#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Post-turn assertion hook over agent utterances + side-effects (#1301).

Implements the τ-bench COMMUNICATE × DB scoring gap deterministically.

In τ-bench (sierra-research/tau-bench, ``docs/evaluation.md``) the reward is
the product of two binary signals:

* **DB** — the environment end-state matches a target hash / predicate.
* **COMMUNICATE** — every required substring appears verbatim in the agent's
  utterances to the user.

Hermes already injects policy via the system prompt and records the transcript
(``agent/trajectory.save_trajectory``). What is missing is the *grading* side:
a way to assert, after a run, that the agent's utterances contain the required
information and that the recorded tool-call/environment state matches an
expected predicate. This module is that grader.

Design — same discipline as the other ``scripts/evolution_*.py`` helpers:

* **Pure, deterministic, standard-library only.** No LLM judge here — the
  NL_ASSERTION LLM-judge slot is reserved for a future additive enhancement
  (it is still WIP upstream in τ-bench). Deterministic substring + state-hash
  now puts Hermes ahead of τ-bench rather than at-par.
* **Import-safe.** Importing this module has no side effects.
* **Opt-in.** When no contract is supplied, nothing happens — the feature is
  strictly additive and never perturbs an existing run.
* **Unit-testable + CLI.** A thin CLI mirror lets the skill / CI call it from
  a terminal, exactly like ``evolution_evaluator.py``.

A **contract** is a JSON document:

.. code-block:: json

    {
      "task_id": "tau-bench/airline/0",
      "required_substrings": ["refund issued", "confirmation #"],
      "forbidden_substrings": ["I cannot", "unable to help"],
      "env_state_assertions": [
        {"type": "tool_called", "tool": "issue_refund"},
        {"type": "json_path", "path": "$.refund.amount", "op": "==", "value": 250}
      ]
    }

The engine consumes an already-captured transcript + the recorded tool-call
results and emits a graded ``GradedResult`` (``score ∈ [0,1]``, ``passed``,
per-clause ``checks[]``). The CLI prints a JSON report and exits 0 on PASS, 1
on FAIL (CI-friendly).

CLI::

    evolution_assertion_hook.py contract.json transcript.json
    cat transcript.json | evolution_assertion_hook.py contract.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from typing import Any, Mapping, Sequence

__all__ = [
    "Clause",
    "ClauseResult",
    "GradedResult",
    "Contract",
    "load_contract",
    "run_assertions",
    "evaluate",
    "main",
]


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Clause:
    """A single assertion clause parsed from the contract JSON.

    Each clause has a ``kind`` (``required_substring`` | ``forbidden_substring``
    | ``tool_called`` | ``tool_not_called`` | ``tool_arg_equals`` |
    ``json_path``) plus kind-specific fields.
    """

    kind: str
    # substring clauses
    substring: str = ""
    case_insensitive: bool = False
    # tool-call clauses
    tool: str = ""
    arg_path: str = ""
    arg_value: Any = None
    # json_path clauses (env-state assertions over a recorded results blob)
    path: str = ""
    op: str = "=="
    value: Any = None
    # provenance for the report
    source: str = ""


@dataclass
class ClauseResult:
    """Outcome of evaluating one clause against the transcript."""

    kind: str
    source: str
    passed: bool
    reason: str = ""


@dataclass
class GradedResult:
    """Aggregate graded outcome over all clauses of a contract.

    ``score ∈ [0,1]`` is the fraction of clauses that passed. ``passed`` is
    True iff every clause passed (binary reward, matching τ-bench semantics
    where reward = DB × COMMUNICATE — a single failure zeroes the reward).
    """

    task_id: str
    score: float
    passed: bool
    checks: list[ClauseResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "score": self.score,
            "passed": self.passed,
            "checks": [asdict(c) for c in self.checks],
        }


@dataclass
class Contract:
    """A loaded, validated assertion contract."""

    task_id: str
    clauses: list[Clause]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Contract":
        return load_contract(raw)


# --------------------------------------------------------------------------- #
# Contract loader / validator
# --------------------------------------------------------------------------- #
def load_contract(raw: Mapping[str, Any]) -> Contract:
    """Parse + validate a contract dict into a :class:`Contract`.

    Raises ``ValueError`` with a human-readable message on schema violation so
    the CLI can surface the exact problem instead of a traceback.
    """
    if not isinstance(raw, Mapping):
        raise ValueError("contract must be a JSON object")
    task_id = raw.get("task_id", "")
    if not isinstance(task_id, str):
        raise ValueError("contract.task_id must be a string")

    clauses: list[Clause] = []
    # COMMUNICATE side: substring checks over agent utterances.
    for sub in raw.get("required_substrings", []) or []:
        if not isinstance(sub, str) or not sub:
            raise ValueError("required_substrings entries must be non-empty strings")
        clauses.append(
            Clause(
                kind="required_substring",
                substring=sub,
                case_insensitive=bool(
                    raw.get("required_substrings_case_insensitive", False)
                ),
                source="required_substrings",
            )
        )
    for sub in raw.get("forbidden_substrings", []) or []:
        if not isinstance(sub, str) or not sub:
            raise ValueError("forbidden_substrings entries must be non-empty strings")
        clauses.append(
            Clause(
                kind="forbidden_substring",
                substring=sub,
                case_insensitive=bool(
                    raw.get("forbidden_substrings_case_insensitive", False)
                ),
                source="forbidden_substrings",
            )
        )

    # DB / env-state side: assertions over recorded tool-call results.
    for entry in raw.get("env_state_assertions", []) or []:
        if not isinstance(entry, Mapping):
            raise ValueError("env_state_assertions entries must be objects")
        kind = str(entry.get("type", ""))
        if kind == "tool_called":
            clauses.append(
                Clause(
                    kind="tool_called",
                    tool=str(entry.get("tool", "")),
                    source="env_state_assertions[tool_called]",
                )
            )
        elif kind == "tool_not_called":
            clauses.append(
                Clause(
                    kind="tool_not_called",
                    tool=str(entry.get("tool", "")),
                    source="env_state_assertions[tool_not_called]",
                )
            )
        elif kind == "tool_arg_equals":
            clauses.append(
                Clause(
                    kind="tool_arg_equals",
                    tool=str(entry.get("tool", "")),
                    arg_path=str(entry.get("arg_path", entry.get("path", ""))),
                    arg_value=entry.get("value"),
                    source="env_state_assertions[tool_arg_equals]",
                )
            )
        elif kind == "json_path":
            clauses.append(
                Clause(
                    kind="json_path",
                    path=str(entry.get("path", "")),
                    op=str(entry.get("op", "==")),
                    value=entry.get("value"),
                    source="env_state_assertions[json_path]",
                )
            )
        else:
            raise ValueError(
                f"env_state_assertions.type must be one of "
                f"tool_called|tool_not_called|tool_arg_equals|json_path (got {kind!r})"
            )

    return Contract(task_id=task_id, clauses=clauses)


# --------------------------------------------------------------------------- #
# Assertion engine
# --------------------------------------------------------------------------- #
def _extract_agent_utterances(transcript: Sequence[Mapping[str, Any]]) -> str:
    """Concatenate the textual content of assistant messages.

    A transcript entry is a ShareGPT-style message dict with a ``role`` field
    (``from`` is also accepted for ShareGPT compatibility). Tool-call content
    is excluded — only what the agent *said to the user* is graded, matching
    the τ-bench COMMUNICATE definition.
    """
    parts: list[str] = []
    for msg in transcript:
        role = msg.get("role") or msg.get("from") or ""
        if role not in ("assistant", "agent", "GPT"):
            continue
        content = msg.get("content") or msg.get("value") or ""
        if isinstance(content, list):
            # OpenAI multimodal content — concatenate text parts only.
            content = " ".join(
                p.get("text", "")
                for p in content
                if isinstance(p, Mapping) and p.get("type") == "text"
            )
        if isinstance(content, str):
            parts.append(content)
    return "\n".join(parts)


def _extract_tool_calls(transcript: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Collect recorded tool calls and their results from the transcript.

    Returns a list of ``{"tool", "args", "result"}`` dicts. Accepts both the
    OpenAI tool-call shape (``tool_calls`` array on the assistant message) and
    a flat ``role == "tool"`` / ``role == "function"`` result message that
    carries the corresponding tool name in ``name`` / ``tool_call_id``.
    """
    calls: list[dict] = []
    # Map tool_call_id -> tool name so we can resolve result messages.
    id_to_tool: dict[str, str] = {}
    for msg in transcript:
        role = msg.get("role") or msg.get("from") or ""
        if role in ("assistant", "agent", "GPT"):
            for tc in msg.get("tool_calls", []) or []:
                if not isinstance(tc, Mapping):
                    continue
                fn = tc.get("function") or tc
                name = fn.get("name", "")
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        pass  # keep raw string if not JSON
                calls.append({"tool": name, "args": args, "result": None})
                tc_id = tc.get("id", "")
                if tc_id:
                    id_to_tool[tc_id] = name
        elif role in ("tool", "function"):
            tc_id = msg.get("tool_call_id", "")
            tool_name = msg.get("name") or id_to_tool.get(tc_id, "")
            # Attach the result to the most recent matching call.
            for call in reversed(calls):
                if call["tool"] == tool_name and call["result"] is None:
                    call["result"] = msg.get("content")
                    break
    return calls


def _json_path_get(blob: Any, path: str) -> Any:
    """Tiny JSONPath resolver supporting ``$.a.b[0].c``.

    Deliberately minimal — the assertion contract is trusted author input, not
    arbitrary user input. Returns ``_MISSING`` sentinel when the path does not
    resolve (distinct from a legitimate ``None`` value at the target).
    """
    if not path:
        return _MISSING
    p = path.lstrip("$").lstrip(".")
    cur: Any = blob
    # Tokenize: split on '.' but respect [index] segments.
    tokens = re.findall(r"[^.\[\]]+|\[\d+\]", p)
    for tok in tokens:
        if not cur:
            return _MISSING
        if tok.startswith("[") and tok.endswith("]"):
            idx = int(tok[1:-1])
            if not isinstance(cur, list) or idx >= len(cur):
                return _MISSING
            cur = cur[idx]
        else:
            if not isinstance(cur, Mapping) or tok not in cur:
                return _MISSING
            cur = cur[tok]
    return cur


class _Missing:
    """Sentinel for an unresolved JSON path (distinct from ``None``)."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return "<MISSING>"


_MISSING = _Missing()


def _compare(actual: Any, op: str, expected: Any) -> bool:
    """Evaluate ``actual <op> expected`` for the small operator vocabulary.

    Supported ops: ``==``, ``!=``, ``<``, ``<=``, ``>``, ``>=``, ``contains``.
    """
    if op in ("==",):
        return actual == expected
    if op in ("!=",):
        return actual != expected
    if op == "contains":
        if isinstance(actual, (list, tuple, set)):
            return expected in actual
        if isinstance(actual, str):
            return str(expected) in actual
        if isinstance(actual, Mapping):
            return expected in actual
        return False
    try:
        if op == "<":
            return actual < expected
        if op == "<=":
            return actual <= expected
        if op == ">":
            return actual > expected
        if op == ">=":
            return actual >= expected
    except TypeError:
        return False
    raise ValueError(f"unsupported op {op!r}")


def _match_substring(haystack: str, needle: str, case_insensitive: bool) -> bool:
    if case_insensitive:
        return needle.lower() in haystack.lower()
    return needle in haystack


def run_assertions(
    contract: Contract,
    transcript: Sequence[Mapping[str, Any]],
) -> GradedResult:
    """Evaluate every clause of ``contract`` against ``transcript``.

    This is the core deterministic grader. It is pure — given the same contract
    and transcript it always returns the same :class:`GradedResult`.
    """
    utterance = _extract_agent_utterances(transcript)
    tool_calls = _extract_tool_calls(transcript)
    tool_names = [c["tool"] for c in tool_calls]
    # Env-state blob: merge tool results into a single dict for json_path clauses.
    env_blob: dict[str, Any] = {}
    for c in tool_calls:
        if c["tool"]:
            env_blob.setdefault(c["tool"], []).append({
                "args": c["args"],
                "result": c["result"],
            })

    checks: list[ClauseResult] = []
    for clause in contract.clauses:
        checks.append(_eval_clause(clause, utterance, tool_calls, tool_names, env_blob))

    passed_count = sum(1 for c in checks if c.passed)
    total = len(checks)
    score = (passed_count / total) if total else 0.0
    return GradedResult(
        task_id=contract.task_id,
        score=score,
        passed=(total > 0 and passed_count == total),
        checks=checks,
    )


def _eval_clause(
    clause: Clause,
    utterance: str,
    tool_calls: list[dict],
    tool_names: list[str],
    env_blob: dict[str, Any],
) -> ClauseResult:
    """Evaluate a single clause. Returns a :class:`ClauseResult` (never raises)."""
    k = clause.kind
    try:
        if k == "required_substring":
            ok = _match_substring(utterance, clause.substring, clause.case_insensitive)
            return ClauseResult(
                kind=k,
                source=clause.source,
                passed=ok,
                reason=(
                    f"found {clause.substring!r}"
                    if ok
                    else f"missing required substring {clause.substring!r}"
                ),
            )
        if k == "forbidden_substring":
            present = _match_substring(
                utterance, clause.substring, clause.case_insensitive
            )
            ok = not present
            return ClauseResult(
                kind=k,
                source=clause.source,
                passed=ok,
                reason=(
                    f"{clause.substring!r} absent"
                    if ok
                    else f"forbidden substring {clause.substring!r} present"
                ),
            )
        if k == "tool_called":
            ok = clause.tool in tool_names
            return ClauseResult(
                kind=k,
                source=clause.source,
                passed=ok,
                reason=(
                    f"tool {clause.tool!r} called"
                    if ok
                    else f"tool {clause.tool!r} never called"
                ),
            )
        if k == "tool_not_called":
            ok = clause.tool not in tool_names
            return ClauseResult(
                kind=k,
                source=clause.source,
                passed=ok,
                reason=(
                    f"tool {clause.tool!r} not called"
                    if ok
                    else f"forbidden tool {clause.tool!r} was called"
                ),
            )
        if k == "tool_arg_equals":
            matching = [c for c in tool_calls if c["tool"] == clause.tool]
            if not matching:
                return ClauseResult(
                    kind=k,
                    source=clause.source,
                    passed=False,
                    reason=f"tool {clause.tool!r} never called",
                )
            ok_any = False
            for c in matching:
                val = _json_path_get(c["args"], clause.arg_path)
                if val is not _MISSING and _compare(val, "==", clause.arg_value):
                    ok_any = True
                    break
            return ClauseResult(
                kind=k,
                source=clause.source,
                passed=ok_any,
                reason=(
                    f"{clause.tool}.{clause.arg_path} == {clause.arg_value!r}"
                    if ok_any
                    else f"no {clause.tool!r} call had {clause.arg_path} == {clause.arg_value!r}"
                ),
            )
        if k == "json_path":
            val = _json_path_get(env_blob, clause.path)
            if val is _MISSING:
                return ClauseResult(
                    kind=k,
                    source=clause.source,
                    passed=False,
                    reason=f"path {clause.path!r} did not resolve",
                )
            ok = _compare(val, clause.op, clause.value)
            return ClauseResult(
                kind=k,
                source=clause.source,
                passed=ok,
                reason=(
                    f"{clause.path} {clause.op} {clause.value!r}"
                    if ok
                    else f"{clause.path} resolved to {val!r}, expected {clause.op} {clause.value!r}"
                ),
            )
        # Unknown kind — should not happen (loader validates) but be defensive.
        return ClauseResult(
            kind=k,
            source=clause.source,
            passed=False,
            reason=f"unknown clause kind {k!r}",
        )
    except Exception as exc:  # noqa: BLE001 - clause must never crash the grader
        return ClauseResult(
            kind=k,
            source=clause.source,
            passed=False,
            reason=f"clause evaluation error: {exc}",
        )


# --------------------------------------------------------------------------- #
# Thin wrapper for batch use
# --------------------------------------------------------------------------- #
def evaluate(
    contract: Mapping[str, Any] | Contract,
    transcript: Sequence[Mapping[str, Any]],
) -> GradedResult:
    """Convenience entry point: accept a raw contract dict OR a Contract.

    Equivalent to ``run_assertions(load_contract(contract), transcript)`` when
    given a dict. Lets callers skip the explicit load step.
    """
    if isinstance(contract, Contract):
        return run_assertions(contract, transcript)
    return run_assertions(load_contract(contract), transcript)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point.

    Usage::

        evolution_assertion_hook.py contract.json transcript.json
        cat transcript.json | evolution_assertion_hook.py contract.json

    Prints the graded result as JSON to stdout. Exit codes:

    * 0 — all clauses passed
    * 1 — at least one clause failed
    * 2 — bad input (contract / transcript unreadable or malformed)
    """
    parser = argparse.ArgumentParser(
        prog="evolution_assertion_hook.py",
        description="τ-bench-style post-turn assertion grader (#1301).",
    )
    parser.add_argument("contract", help="Path to the contract JSON file.")
    parser.add_argument(
        "transcript",
        nargs="?",
        help="Path to the transcript JSON file. If omitted, reads transcript from stdin.",
    )
    args = parser.parse_args(argv)

    try:
        with open(args.contract, "r", encoding="utf-8") as f:
            raw_contract = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": f"cannot read contract: {exc}"}))
        return 2

    transcript_source = args.transcript
    if transcript_source is None or transcript_source == "-":
        raw_transcript = sys.stdin.read()
    else:
        try:
            with open(transcript_source, "r", encoding="utf-8") as f:
                raw_transcript = f.read()
        except OSError as exc:
            print(json.dumps({"error": f"cannot read transcript: {exc}"}))
            return 2

    try:
        transcript = json.loads(raw_transcript)
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"transcript is not valid JSON: {exc}"}))
        return 2

    # Transcript may be a bare list (array of messages) or a wrapped object
    # with a "conversations" / "messages" key (trajectory_samples.jsonl shape).
    if isinstance(transcript, Mapping):
        transcript = transcript.get("conversations") or transcript.get("messages") or []

    if not isinstance(transcript, list):
        print(json.dumps({"error": "transcript must be a JSON array of messages"}))
        return 2

    try:
        result = evaluate(raw_contract, transcript)
    except ValueError as exc:
        print(json.dumps({"error": f"contract invalid: {exc}"}))
        return 2

    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
