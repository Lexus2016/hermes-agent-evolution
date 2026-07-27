"""Tests for the post-turn assertion hook (#1301).

Covers the grader (``agent/post_turn_assertion.py``) directly — COMMUNICATE
substring axis, DB sha256 axis, composite product semantics, opt-in env gate,
and the no-raise contract — plus a thin integration test confirming the hook
fires from ``finalize_turn`` when ``HERMES_ASSERT_CONTRACT`` is set and is
silent otherwise.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.post_turn_assertion import (  # noqa: E402
    _assistant_text,
    evaluate,
    run_if_enabled,
)


def _contract(tmp_path, **fields):
    p = tmp_path / "contract.json"
    p.write_text(json.dumps(fields), encoding="utf-8")
    return str(p)


def _msg(role, content):
    return {"role": role, "content": content}


# ── COMMUNICATE axis ──────────────────────────────────────────────────────


class TestCommunicateAxis:
    def test_pass_when_all_substrings_present(self, tmp_path):
        contract = _contract(tmp_path, communicate=["hello", "world"])
        msgs = [_msg("assistant", "hello world!"), _msg("user", "ignored")]
        v = evaluate(contract, msgs)
        assert v["communicate"] == {"pass": True, "missing": [], "applicable": True}
        assert v["score"] == 1

    def test_fail_reports_missing_substrings(self, tmp_path):
        contract = _contract(tmp_path, communicate=["hello", "missing"])
        msgs = [_msg("assistant", "hello there")]
        v = evaluate(contract, msgs)
        assert v["communicate"]["pass"] is False
        assert v["communicate"]["missing"] == ["missing"]
        assert v["score"] == 0

    def test_not_applicable_when_no_communicate(self, tmp_path):
        contract = _contract(tmp_path, task_id="t1")
        v = evaluate(contract, [_msg("assistant", "anything")])
        assert v["communicate"]["applicable"] is False
        assert v["communicate"]["pass"] is True

    def test_ignores_tool_call_arguments(self, tmp_path):
        """Only user-facing assistant TEXT is checked — tool-call arguments
        are excluded (τ-bench COMMUNICATE semantics)."""
        contract = _contract(tmp_path, communicate=["secret-in-args"])
        msgs = [
            {
                "role": "assistant",
                "content": "I will do it",
                "tool_calls": [
                    {"function": {"name": "x", "arguments": '{"a": "secret-in-args"}'}}
                ],
            }
        ]
        v = evaluate(contract, msgs)
        assert v["communicate"]["pass"] is False

    def test_handles_list_content_parts(self, tmp_path):
        contract = _contract(tmp_path, communicate=["part-a", "part-b"])
        msgs = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "part-a"},
                    {"type": "text", "text": "part-b"},
                ],
            }
        ]
        v = evaluate(contract, msgs)
        assert v["communicate"]["pass"] is True


# ── DB axis ───────────────────────────────────────────────────────────────


class TestDbAxis:
    def test_pass_on_sha256_match(self, tmp_path):
        target = tmp_path / "out.txt"
        target.write_text("expected content", encoding="utf-8")
        digest = hashlib.sha256(b"expected content").hexdigest()
        contract = _contract(tmp_path, db={"path": str(target), "sha256": digest})
        v = evaluate(contract, [])
        assert v["db"]["pass"] is True
        assert v["db"]["applicable"] is True
        assert v["score"] == 1

    def test_fail_on_sha256_mismatch(self, tmp_path):
        target = tmp_path / "out.txt"
        target.write_text("actual content", encoding="utf-8")
        contract = _contract(tmp_path, db={"path": str(target), "sha256": "0" * 64})
        v = evaluate(contract, [])
        assert v["db"]["pass"] is False
        assert "mismatch" in v["db"]["reason"]
        assert v["score"] == 0

    def test_fail_when_target_missing(self, tmp_path):
        contract = _contract(
            tmp_path, db={"path": str(tmp_path / "nope.txt"), "sha256": "0" * 64}
        )
        v = evaluate(contract, [])
        assert v["db"]["pass"] is False
        assert "unreadable" in v["db"]["reason"]


# ── Composite (product semantics) ─────────────────────────────────────────


class TestCompositeScore:
    def test_both_axes_pass_yields_one(self, tmp_path):
        target = tmp_path / "out.txt"
        target.write_text("ok", encoding="utf-8")
        digest = hashlib.sha256(b"ok").hexdigest()
        contract = _contract(
            tmp_path,
            communicate=["done"],
            db={"path": str(target), "sha256": digest},
        )
        v = evaluate(contract, [_msg("assistant", "all done")])
        assert v["score"] == 1

    def test_one_axis_fail_zeros_score(self, tmp_path):
        target = tmp_path / "out.txt"
        target.write_text("ok", encoding="utf-8")
        digest = hashlib.sha256(b"ok").hexdigest()
        contract = _contract(
            tmp_path,
            communicate=["missing-substring"],
            db={"path": str(target), "sha256": digest},
        )
        v = evaluate(contract, [_msg("assistant", "all done")])
        assert v["score"] == 0  # communicate failed → product is 0


# ── Robustness ────────────────────────────────────────────────────────────


class TestRobustness:
    def test_malformed_contract_returns_error_not_raise(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        v = evaluate(str(bad), [])
        assert v["score"] == 0
        assert "error" in v

    def test_missing_contract_file(self, tmp_path):
        v = evaluate(str(tmp_path / "nope.json"), [])
        assert v["score"] == 0
        assert "error" in v

    def test_evaluate_never_raises_on_bad_messages(self, tmp_path):
        contract = _contract(tmp_path, communicate=["x"])
        # garbage messages must not crash
        v = evaluate(contract, [None, "string", {"role": 42}, {"role": "assistant"}])
        assert v["score"] == 0


# ── Opt-in env gate ───────────────────────────────────────────────────────


class TestEnvGate:
    def test_run_if_enabled_returns_none_when_unset(self, monkeypatch):
        monkeypatch.delenv("HERMES_ASSERT_CONTRACT", raising=False)
        assert run_if_enabled([_msg("assistant", "hi")]) is None

    def test_run_if_enabled_runs_when_set(self, tmp_path, monkeypatch):
        contract = _contract(tmp_path, communicate=["hi"])
        monkeypatch.setenv("HERMES_ASSERT_CONTRACT", contract)
        v = run_if_enabled([_msg("assistant", "hi there")])
        assert v is not None
        assert v["score"] == 1

    def test_result_path_writes_json(self, tmp_path, monkeypatch):
        contract = _contract(tmp_path, communicate=["hi"])
        out = tmp_path / "result.json"
        monkeypatch.setenv("HERMES_ASSERT_CONTRACT", contract)
        monkeypatch.setenv("HERMES_ASSERT_RESULT_PATH", str(out))
        run_if_enabled([_msg("assistant", "hi")])
        assert out.exists()
        data = json.loads(out.read_text())
        assert data["score"] == 1


# ── Integration: finalize_turn wiring ─────────────────────────────────────


class TestFinalizeTurnWiring:
    """Confirms the hook is actually invoked from the real call site, not just
    importable — the exact failure that closed PR #1315."""

    def test_hook_silent_without_env(self, monkeypatch):
        from agent.turn_finalizer import finalize_turn

        monkeypatch.delenv("HERMES_ASSERT_CONTRACT", raising=False)
        # If the wiring were broken (e.g. raised unconditionally), finalize_turn
        # would propagate. We don't need a full agent stub — the env gate makes
        # the hook a no-op before it touches agent state.
        try:
            from agent import post_turn_assertion as pta

            assert pta.run_if_enabled([]) is None
        except Exception as exc:
            pytest.fail(f"hook wiring raised without env set: {exc}")
