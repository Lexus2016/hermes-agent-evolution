# -*- coding: utf-8 -*-
"""Live-seam wiring tests for decision-level model-call capture (#2877).

First attempt (PR #2906) was bounced: the capture had no production call
site. This rework folds ``extract_model_calls`` into the REAL trajectory
save seam (``run_agent._save_trajectory`` → ``agent.trajectory.save_trajectory``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from agent.tool_call_capture import extract_model_calls  # noqa: E402


def _raw_turn():
    """A realistic RAW message list as ``_save_trajectory`` receives it."""
    return [
        {"role": "user", "content": "run the check"},
        {"role": "assistant", "model": "hermes-small", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
        {"role": "assistant", "model": "hermes-small", "content": "done"},
    ]


class TestExtractModelCalls:
    def test_one_entry_per_assistant_message(self):
        calls = extract_model_calls(_raw_turn())
        assert len(calls) == 2
        assert calls[0] == {"model": "hermes-small", "decision": "tool_call",
                            "tool_call_count": 1}
        assert calls[1]["decision"] == "content"

    def test_classifies_refusal(self):
        calls = extract_model_calls(
            [{"role": "assistant", "content": "I can't run that without approval."}])
        assert calls[0]["decision"] == "refusal"

    def test_ignores_non_assistant_and_never_raises(self):
        assert extract_model_calls(
            [{"role": "user", "content": "hi"},
             {"role": "tool", "tool_call_id": "x", "content": "{}"}]) == []
        assert extract_model_calls(None) == []
        assert extract_model_calls("not a list") == []

    def test_field_is_metadata_only(self):
        raw = json.dumps(extract_model_calls(_raw_turn()))
        assert "run the check" not in raw and "done" not in raw


class TestSaveTrajectoryModelCalls:
    def test_writes_model_calls_when_provided(self, tmp_path):
        from agent.trajectory import save_trajectory

        out = tmp_path / "trajectory_samples.jsonl"
        save_trajectory(_raw_turn(), model="hermes-small", completed=True,
                        filename=str(out),
                        model_calls=extract_model_calls(_raw_turn()))
        entry = json.loads(out.read_text(encoding="utf-8").strip())
        assert [c["decision"] for c in entry["model_calls"]] == ["tool_call", "content"]
        # The field is metadata-only; conversations keep pre-existing semantics.
        assert "run the check" not in json.dumps(entry["model_calls"])

    def test_omits_key_when_empty_legacy_shape_preserved(self, tmp_path):
        from agent.trajectory import save_trajectory

        out = tmp_path / "trajectory_samples.jsonl"
        save_trajectory([{"from": "human", "value": "hi"}], model="m",
                        completed=True, filename=str(out), model_calls=None)
        entry = json.loads(out.read_text(encoding="utf-8").strip())
        assert set(entry.keys()) == {"conversations", "timestamp", "model", "completed"}


class TestRunAgentSaveTrajectoryWiring:
    """The live seam must extract from RAW messages and reach the writer."""

    def _fake_agent(self):
        from types import SimpleNamespace

        return SimpleNamespace(
            save_trajectories=True,
            model="hermes-small",
            _convert_to_trajectory_format=lambda messages, user_query, completed: messages,
        )

    def test_capture_reaches_the_writer(self, monkeypatch):
        from run_agent import AIAgent

        captured = {}
        monkeypatch.setattr("run_agent._save_trajectory_to_file",
                            lambda trajectory, model, completed, **kw: captured.update(kw))
        AIAgent._save_trajectory(self._fake_agent(), _raw_turn(), "run the check", True)
        assert [c["decision"] for c in captured["model_calls"]] == ["tool_call", "content"]

    def test_refusal_decision_reaches_the_writer(self, monkeypatch):
        from run_agent import AIAgent

        captured = {}
        monkeypatch.setattr("run_agent._save_trajectory_to_file",
                            lambda trajectory, model, completed, **kw: captured.update(kw))
        msgs = [{"role": "user", "content": "delete the db"},
                {"role": "assistant", "model": "hermes-small", "content": "I can't do that."}]
        AIAgent._save_trajectory(self._fake_agent(), msgs, "delete the db", True)
        assert [c["decision"] for c in captured["model_calls"]] == ["refusal"]

    def test_capture_failure_never_breaks_trajectory_save(self, monkeypatch):
        from run_agent import AIAgent

        def _boom(_messages):
            raise RuntimeError("capture exploded")

        monkeypatch.setattr("agent.tool_call_capture.extract_model_calls", _boom)
        captured = {}
        monkeypatch.setattr("run_agent._save_trajectory_to_file",
                            lambda *a, **kw: captured.update(kw))
        AIAgent._save_trajectory(self._fake_agent(), _raw_turn(), "q", True)
        assert captured.get("model_calls") is None

    def test_disabled_writes_nothing(self, monkeypatch):
        from run_agent import AIAgent

        called = {"n": 0}
        monkeypatch.setattr(
            "run_agent._save_trajectory_to_file",
            lambda *a, **kw: called.__setitem__("n", called["n"] + 1))
        agent = self._fake_agent()
        agent.save_trajectories = False
        AIAgent._save_trajectory(agent, _raw_turn(), "q", True)
        assert called["n"] == 0
