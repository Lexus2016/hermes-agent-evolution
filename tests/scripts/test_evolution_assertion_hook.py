# -*- coding: utf-8 -*-
"""Tests for scripts/evolution_assertion_hook.py (#1301).

Covers the τ-bench COMMUNICATE × DB deterministic grader: substring checks,
tool-call constraints, json_path env-state assertions, the CLI, and the
τ-bench reward semantics (a single failing clause zeroes the reward).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import evolution_assertion_hook as ah  # noqa: E402


# --------------------------------------------------------------------------- #
# Transcript fixtures (ShareGPT / OpenAI hybrid shapes the engine accepts)
# --------------------------------------------------------------------------- #
def _transcript(*msgs):
    """Build a transcript from (role, content) tuples."""
    return [{"role": r, "content": c} for r, c in msgs]


def _transcript_with_tool_call(tool_name, args=None, result=None, utterance="done"):
    """OpenAI tool-call shape: assistant message with tool_calls + tool result."""
    return [
        {"role": "user", "content": "please refund"},
        {
            "role": "assistant",
            "content": utterance,
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(args or {}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": tool_name,
            "content": json.dumps(result or {}),
        },
        {"role": "assistant", "content": utterance},
    ]


# --------------------------------------------------------------------------- #
# COMMUNICATE side — substring checks
# --------------------------------------------------------------------------- #
def test_required_substring_present_passes():
    contract = ah.Contract.from_dict({
        "task_id": "t1",
        "required_substrings": ["refund issued"],
    })
    transcript = _transcript(
        ("user", "please refund"),
        ("assistant", "Your refund issued. Confirmation #abc"),
    )
    r = ah.run_assertions(contract, transcript)
    assert r.passed is True
    assert r.score == 1.0


def test_required_substring_absent_fails_and_zeroes_reward():
    contract = ah.Contract.from_dict({
        "task_id": "t1",
        "required_substrings": ["refund issued"],
    })
    transcript = _transcript(("assistant", "sorry, cannot help"))
    r = ah.run_assertions(contract, transcript)
    assert r.passed is False
    assert r.score == 0.0
    assert "missing required substring" in r.checks[0].reason


def test_required_substring_case_insensitive():
    contract = ah.Contract.from_dict({
        "task_id": "t1",
        "required_substrings": ["REFUND ISSUED"],
        "required_substrings_case_insensitive": True,
    })
    transcript = _transcript(("assistant", "your refund issued today"))
    r = ah.run_assertions(contract, transcript)
    assert r.passed is True


def test_forbidden_substring_present_fails():
    contract = ah.Contract.from_dict({
        "task_id": "t1",
        "forbidden_substrings": ["I cannot"],
    })
    transcript = _transcript(("assistant", "I cannot help with that"))
    r = ah.run_assertions(contract, transcript)
    assert r.passed is False
    assert "forbidden substring" in r.checks[0].reason


def test_forbidden_substring_absent_passes():
    contract = ah.Contract.from_dict({
        "task_id": "t1",
        "forbidden_substrings": ["I cannot"],
    })
    transcript = _transcript(("assistant", "Done!"))
    r = ah.run_assertions(contract, transcript)
    assert r.passed is True


def test_only_assistant_utterances_are_graded_not_tool_output():
    """Tool-role content must not count toward COMMUNICATE (user-facing only)."""
    contract = ah.Contract.from_dict({
        "task_id": "t1",
        "required_substrings": ["secret"],
    })
    # "secret" appears only in a tool result, never in the assistant utterance.
    transcript = [
        {"role": "assistant", "content": "ok"},
        {"role": "tool", "content": "the secret value is 42"},
    ]
    r = ah.run_assertions(contract, transcript)
    assert r.passed is False


# --------------------------------------------------------------------------- #
# DB / env-state side — tool-call constraints
# --------------------------------------------------------------------------- #
def test_tool_called_present_passes():
    contract = ah.Contract.from_dict({
        "task_id": "t1",
        "env_state_assertions": [{"type": "tool_called", "tool": "issue_refund"}],
    })
    transcript = _transcript_with_tool_call("issue_refund")
    r = ah.run_assertions(contract, transcript)
    assert r.passed is True


def test_tool_called_absent_fails():
    contract = ah.Contract.from_dict({
        "task_id": "t1",
        "env_state_assertions": [{"type": "tool_called", "tool": "issue_refund"}],
    })
    transcript = _transcript(("assistant", "done"))
    r = ah.run_assertions(contract, transcript)
    assert r.passed is False
    assert "never called" in r.checks[0].reason


def test_tool_not_called_constraint():
    contract = ah.Contract.from_dict({
        "task_id": "t1",
        "env_state_assertions": [{"type": "tool_not_called", "tool": "delete_user"}],
    })
    transcript = _transcript_with_tool_call("issue_refund")
    r = ah.run_assertions(contract, transcript)
    assert r.passed is True


def test_tool_arg_equals_matches():
    contract = ah.Contract.from_dict({
        "task_id": "t1",
        "env_state_assertions": [
            {
                "type": "tool_arg_equals",
                "tool": "issue_refund",
                "arg_path": "$.amount",
                "value": 250,
            }
        ],
    })
    transcript = _transcript_with_tool_call("issue_refund", args={"amount": 250})
    r = ah.run_assertions(contract, transcript)
    assert r.passed is True


def test_tool_arg_equals_mismatch_fails():
    contract = ah.Contract.from_dict({
        "task_id": "t1",
        "env_state_assertions": [
            {
                "type": "tool_arg_equals",
                "tool": "issue_refund",
                "arg_path": "$.amount",
                "value": 250,
            }
        ],
    })
    transcript = _transcript_with_tool_call("issue_refund", args={"amount": 100})
    r = ah.run_assertions(contract, transcript)
    assert r.passed is False


# --------------------------------------------------------------------------- #
# json_path env-state assertions over the merged tool-results blob
# --------------------------------------------------------------------------- #
def test_json_path_equals_matches():
    contract = ah.Contract.from_dict({
        "task_id": "t1",
        "env_state_assertions": [
            {
                "type": "json_path",
                "path": "$.issue_refund[0].args.amount",
                "op": "==",
                "value": 250,
            }
        ],
    })
    transcript = _transcript_with_tool_call("issue_refund", args={"amount": 250})
    r = ah.run_assertions(contract, transcript)
    assert r.passed is True


def test_json_path_missing_fails_with_clear_reason():
    contract = ah.Contract.from_dict({
        "task_id": "t1",
        "env_state_assertions": [
            {"type": "json_path", "path": "$.nonexistent.path", "op": "==", "value": 1}
        ],
    })
    transcript = _transcript_with_tool_call("issue_refund")
    r = ah.run_assertions(contract, transcript)
    assert r.passed is False
    assert "did not resolve" in r.checks[0].reason


def test_json_path_ge_operator():
    contract = ah.Contract.from_dict({
        "task_id": "t1",
        "env_state_assertions": [
            {
                "type": "json_path",
                "path": "$.issue_refund[0].args.amount",
                "op": ">=",
                "value": 200,
            }
        ],
    })
    transcript = _transcript_with_tool_call("issue_refund", args={"amount": 250})
    r = ah.run_assertions(contract, transcript)
    assert r.passed is True


# --------------------------------------------------------------------------- #
# Aggregate scoring — τ-bench reward = product (single failure zeroes reward)
# --------------------------------------------------------------------------- #
def test_partial_pass_scores_fraction_but_passed_false():
    """τ-bench reward is binary; one failing clause zeroes it."""
    contract = ah.Contract.from_dict({
        "task_id": "t1",
        "required_substrings": ["refund issued", "never gonna happen xyz"],
    })
    transcript = _transcript(("assistant", "refund issued"))
    r = ah.run_assertions(contract, transcript)
    assert r.passed is False  # binary reward
    assert r.score == 0.5  # but graded score still reports the fraction


def test_empty_contract_returns_zero_score_not_passed():
    contract = ah.Contract.from_dict({"task_id": "t1"})
    r = ah.run_assertions(contract, [])
    assert r.passed is False
    assert r.score == 0.0


def test_full_tau_bench_scenario():
    """End-to-end: COMMUNICATE × DB both satisfied → reward = 1.0."""
    contract = ah.Contract.from_dict({
        "task_id": "tau-bench/airline/0",
        "required_substrings": ["refund issued", "confirmation #"],
        # COMMUNICATE checks in τ-bench are conventionally case-insensitive.
        "required_substrings_case_insensitive": True,
        "forbidden_substrings": ["I cannot"],
        "env_state_assertions": [
            {"type": "tool_called", "tool": "issue_refund"},
            {
                "type": "tool_arg_equals",
                "tool": "issue_refund",
                "arg_path": "$.amount",
                "value": 250,
            },
        ],
    })
    transcript = _transcript_with_tool_call(
        "issue_refund",
        args={"amount": 250},
        utterance="Your refund issued. Confirmation #abc123",
    )
    r = ah.run_assertions(contract, transcript)
    assert r.passed is True
    assert r.score == 1.0


# --------------------------------------------------------------------------- #
# Contract validation
# --------------------------------------------------------------------------- #
def test_invalid_contract_type_raises():
    import pytest

    with pytest.raises(ValueError):
        ah.load_contract([1, 2, 3])


def test_unknown_assertion_type_raises():
    import pytest

    with pytest.raises(ValueError, match="env_state_assertions.type"):
        ah.load_contract({"task_id": "t1", "env_state_assertions": [{"type": "bogus"}]})


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_passes_exits_0(tmp_path, capsys):
    contract_file = tmp_path / "contract.json"
    transcript_file = tmp_path / "transcript.json"
    contract_file.write_text(
        json.dumps({"task_id": "t1", "required_substrings": ["ok"]})
    )
    transcript_file.write_text(
        json.dumps([{"role": "assistant", "content": "ok done"}])
    )
    rc = ah.main([str(contract_file), str(transcript_file)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["passed"] is True


def test_cli_fails_exits_1(tmp_path, capsys):
    contract_file = tmp_path / "contract.json"
    transcript_file = tmp_path / "transcript.json"
    contract_file.write_text(
        json.dumps({"task_id": "t1", "required_substrings": ["missing"]})
    )
    transcript_file.write_text(json.dumps([{"role": "assistant", "content": "nope"}]))
    rc = ah.main([str(contract_file), str(transcript_file)])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["passed"] is False


def test_cli_bad_contract_exits_2(tmp_path, capsys):
    contract_file = tmp_path / "contract.json"
    contract_file.write_text("{not valid json")
    rc = ah.main([str(contract_file), str(tmp_path / "any.json")])
    assert rc == 2


def test_cli_reads_transcript_from_stdin(tmp_path, monkeypatch, capsys):
    import io

    contract_file = tmp_path / "contract.json"
    contract_file.write_text(
        json.dumps({"task_id": "t1", "required_substrings": ["ok"]})
    )
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps([{"role": "assistant", "content": "ok"}]))
    )
    rc = ah.main([str(contract_file)])
    assert rc == 0


def test_cli_accepts_trajectory_wrapper_shape(tmp_path, capsys):
    """trajectory_samples.jsonl wraps messages under 'conversations'."""
    contract_file = tmp_path / "contract.json"
    transcript_file = tmp_path / "traj.json"
    contract_file.write_text(
        json.dumps({"task_id": "t1", "required_substrings": ["hello"]})
    )
    transcript_file.write_text(
        json.dumps({"conversations": [{"role": "assistant", "content": "hello world"}]})
    )
    rc = ah.main([str(contract_file), str(transcript_file)])
    assert rc == 0
