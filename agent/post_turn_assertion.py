"""Post-turn assertion hook — τ-bench-style outcome-fidelity grading (#1301).

Grades a completed turn against a per-task assertion contract after the turn
ends, producing a machine-checkable verdict (the thing that was missing in the
prior 1016-line attempt — that shipped the grader with NO call site). This
module is the grader; the wiring lives in ``agent/turn_finalizer.py``.

The contract format is deliberately minimal and hidden from the agent process
at runtime (the agent must not ``cat`` the verifier — LHTB finding). A contract
is a JSON file discovered via the ``HERMES_ASSERT_CONTRACT`` env var:

    {
      "task_id": "optional-task-label",
      "communicate": ["required substring in agent reply", ...],
      "db": {
        "path": "/abs/path/to/artifact.txt",
        "sha256": "expected sha256 of the file content"
      }
    }

Either axis is optional — a contract may assert only ``communicate`` substrings,
only ``db`` state, or both (product semantics matching τ-bench: composite score
is 1 only if BOTH axes that are present pass).

The verdict is emitted as a single structured line to ``agent.log`` and, when
``HERMES_ASSERT_RESULT_PATH`` is set, written as JSON to that path so a CI / eval
harness can consume it without parsing logs.

This module contains NO network calls, NO agent-process introspection, and NO
LLM — it is a deterministic post-hoc checker over already-recorded state.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


def _load_contract(path: str) -> Optional[Dict[str, Any]]:
    """Load and minimally validate an assertion contract. Returns None on any
    malformed input — a bad contract must never crash the turn finalizer."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _assistant_text(messages: List[Dict[str, Any]]) -> str:
    """Concatenate all assistant-role textual content for substring matching.
    Tool-call arguments are deliberately excluded — only what the agent SAID to
    the user is checked, matching τ-bench COMMUNICATE semantics."""
    parts: List[str] = []
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        content = m.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(str(part.get("text", "")))
    return "\n".join(parts)


def _check_communicate(
    contract: Dict[str, Any], messages: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """COMMUNICATE axis — every required substring appears verbatim in the
    agent's user-facing text. Returns {pass: bool, missing: [...]}."""
    required = contract.get("communicate") or []
    if not isinstance(required, list) or not required:
        return {"pass": True, "missing": [], "applicable": False}
    haystack = _assistant_text(messages)
    missing = [s for s in required if isinstance(s, str) and s not in haystack]
    return {"pass": not missing, "missing": missing, "applicable": True}


def _check_db(contract: Dict[str, Any]) -> Dict[str, Any]:
    """DB axis — hash a target file and compare to the expected sha256. Returns
    {pass: bool, reason: str, applicable: bool}. 'applicable: False' means no
    db assertion was specified (so it does not gate the composite)."""
    db = contract.get("db")
    if not isinstance(db, dict) or not db.get("path"):
        return {"pass": True, "reason": "no db assertion", "applicable": False}
    target = db.get("path")
    expected = db.get("sha256")
    if not isinstance(target, str) or not isinstance(expected, str):
        return {
            "pass": False,
            "reason": "contract missing path or sha256",
            "applicable": True,
        }
    try:
        with open(target, "rb") as fh:
            actual = hashlib.sha256(fh.read()).hexdigest()
    except OSError as exc:
        return {
            "pass": False,
            "reason": f"unreadable target: {exc}",
            "applicable": True,
        }
    return {
        "pass": actual == expected,
        "reason": "match"
        if actual == expected
        else f"sha256 mismatch (got {actual[:12]}...)",
        "applicable": True,
    }


def evaluate(contract_path: str, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Run the assertion contract against the recorded transcript and return a
    structured verdict. Safe to call from finalize_turn — never raises.

    Returns::

        {
          "task_id": str | None,
          "communicate": {"pass": bool, "missing": [...], "applicable": bool},
          "db": {"pass": bool, "reason": str, "applicable": bool},
          "score": 0 | 1,   # product of applicable axes (τ-bench semantics)
          "contract_path": str
        }
    """
    contract = _load_contract(contract_path)
    if contract is None:
        return {
            "task_id": None,
            "communicate": {"pass": False, "missing": [], "applicable": False},
            "db": {"pass": False, "reason": "contract unreadable", "applicable": False},
            "score": 0,
            "contract_path": contract_path,
            "error": "contract unreadable or malformed",
        }
    comm = _check_communicate(contract, messages)
    db = _check_db(contract)
    # Composite score = product of applicable axes (τ-bench DB × COMMUNICATE).
    axes = [ax["pass"] for ax in (comm, db) if ax.get("applicable")]
    score = 1 if axes and all(axes) else (0 if axes else 1)
    return {
        "task_id": contract.get("task_id"),
        "communicate": comm,
        "db": db,
        "score": score,
        "contract_path": contract_path,
    }


def emit_verdict(verdict: Dict[str, Any]) -> None:
    """Emit the verdict — single structured log line + optional JSON file when
    ``HERMES_ASSERT_RESULT_PATH`` is set. Never raises."""
    try:
        from agent.conversation_loop import logger

        logger.info(
            "post-turn-assertion: task=%s score=%d communicate=%s db=%s",
            verdict.get("task_id"),
            verdict.get("score"),
            verdict.get("communicate", {}).get("pass"),
            verdict.get("db", {}).get("pass"),
        )
    except Exception:
        pass
    out = os.environ.get("HERMES_ASSERT_RESULT_PATH")
    if out:
        try:
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(verdict, fh, indent=2)
        except OSError:
            pass


def run_if_enabled(messages: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Top-level entry point called from ``finalize_turn``. Returns the verdict
    dict when an assertion contract is configured (``HERMES_ASSERT_CONTRACT``),
    otherwise None. This is the ONE call site — keep it thin."""
    contract_path = os.environ.get("HERMES_ASSERT_CONTRACT")
    if not contract_path or not contract_path.strip():
        return None
    verdict = evaluate(contract_path, messages)
    emit_verdict(verdict)
    return verdict
