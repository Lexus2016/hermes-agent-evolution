"""Tests for scripts/evolution_synthetic_trajectory.py — synthetic trajectory
generator for skill evolution (#317).

The generated episodes MUST round-trip the exact JSONL schema that
``agent.trajectory.save_trajectory`` writes (the ``trajectory_samples.jsonl``
pipeline), so downstream consumers treat synthetic and live episodes uniformly.
"""

import json
import sys
from pathlib import Path

# Project root is on sys.path via tests/conftest.py (for ``agent.*`` imports),
# but the script under test lives in scripts/ — add it explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_synthetic_trajectory import (  # noqa: E402
    SCENARIOS,
    generate_batch,
    generate_episode,
    validate_episode_schema,
)


# --- ShareGPT / trajectory schema reference -------------------------------
# Mirrors the structure agent.agent_runtime_helpers builds and
# agent.trajectory.save_trajectory persists.
_VALID_FROMS = {"system", "human", "gpt", "tool"}


def _is_schema_valid_entry(entry: dict) -> bool:
    """Independent re-implementation of the schema check, used to cross-verify
    the module's own validate_episode_schema (so a buggy validator can't pass
    a buggy generator)."""
    if not isinstance(entry, dict):
        return False
    if set(entry.keys()) < {"conversations", "timestamp", "model", "completed"}:
        return False
    convs = entry["conversations"]
    if not isinstance(convs, list) or not convs:
        return False
    for msg in convs:
        if not isinstance(msg, dict):
            return False
        if set(msg.keys()) != {"from", "value"}:
            return False
        if msg["from"] not in _VALID_FROMS:
            return False
        if not isinstance(msg["value"], str):
            return False
    if not isinstance(entry["completed"], bool):
        return False
    if not isinstance(entry["model"], str) or not isinstance(entry["timestamp"], str):
        return False
    return True


class TestScenarioRegistry:
    def test_at_least_one_scenario_registered(self):
        assert len(SCENARIOS) >= 1

    def test_tool_planning_scenario_present(self):
        assert "tool_augmented_planning" in SCENARIOS


class TestSchemaRoundTrip:
    def test_generated_episode_is_schema_valid(self):
        ep = generate_episode("tool_augmented_planning", {"seed": 1})
        # The module's own validator agrees it is valid.
        assert validate_episode_schema(ep) is True
        # And an independent check agrees (defends against a lenient validator).
        assert _is_schema_valid_entry(ep)

    def test_episode_has_canonical_top_level_keys(self):
        ep = generate_episode("tool_augmented_planning", {"seed": 1})
        assert set(ep.keys()) == {"conversations", "timestamp", "model", "completed", "labels"}

    def test_conversation_shape_matches_sharegpt(self):
        ep = generate_episode("tool_augmented_planning", {"seed": 7})
        convs = ep["conversations"]
        # system -> human -> ... at minimum.
        assert convs[0]["from"] == "system"
        assert convs[1]["from"] == "human"
        froms = [m["from"] for m in convs]
        # A tool-augmented episode must exercise at least one gpt + tool turn.
        assert "gpt" in froms
        assert "tool" in froms

    def test_gpt_turns_have_think_block(self):
        ep = generate_episode("tool_augmented_planning", {"seed": 3})
        gpt_turns = [m for m in ep["conversations"] if m["from"] == "gpt"]
        assert gpt_turns
        for turn in gpt_turns:
            assert "<think>" in turn["value"] and "</think>" in turn["value"]

    def test_tool_turns_are_wrapped(self):
        ep = generate_episode("tool_augmented_planning", {"seed": 3})
        tool_turns = [m for m in ep["conversations"] if m["from"] == "tool"]
        assert tool_turns
        for turn in tool_turns:
            assert "<tool_response>" in turn["value"] and "</tool_response>" in turn["value"]

    def test_assistant_tool_calls_are_wrapped(self):
        ep = generate_episode("tool_augmented_planning", {"seed": 3})
        gpt_blob = "\n".join(m["value"] for m in ep["conversations"] if m["from"] == "gpt")
        assert "<tool_call>" in gpt_blob and "</tool_call>" in gpt_blob

    def test_roundtrips_through_save_trajectory(self, tmp_path):
        """Write via the REAL agent.trajectory.save_trajectory and read back —
        proves a synthetic episode is consumed identically to a live one."""
        from agent.trajectory import save_trajectory  # real pipeline writer

        ep = generate_episode("tool_augmented_planning", {"seed": 11})
        out = tmp_path / "trajectory_samples.jsonl"
        save_trajectory(
            ep["conversations"],
            model=ep["model"],
            completed=ep["completed"],
            filename=str(out),
        )
        lines = out.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        reloaded = json.loads(lines[0])
        # save_trajectory rebuilds the canonical entry; conversations survive verbatim.
        assert reloaded["conversations"] == ep["conversations"]
        assert reloaded["completed"] == ep["completed"]
        assert reloaded["model"] == ep["model"]
        assert _is_schema_valid_entry(reloaded)


class TestLabels:
    def test_labels_present_and_typed(self):
        ep = generate_episode("tool_augmented_planning", {"seed": 1})
        labels = ep["labels"]
        assert isinstance(labels, dict)
        # Labels are the supervised signal the evolution pipeline keys on.
        assert labels["scenario"] == "tool_augmented_planning"
        assert isinstance(labels["success"], bool)
        assert isinstance(labels["plan_steps"], list) and labels["plan_steps"]
        assert isinstance(labels["tools_used"], list) and labels["tools_used"]
        assert isinstance(labels["synthetic"], bool) and labels["synthetic"] is True

    def test_success_label_consistent_with_completed(self):
        ep = generate_episode("tool_augmented_planning", {"seed": 1})
        assert ep["labels"]["success"] == ep["completed"]


class TestDeterminism:
    def test_same_params_same_episode(self):
        a = generate_episode("tool_augmented_planning", {"seed": 42})
        b = generate_episode("tool_augmented_planning", {"seed": 42})
        # Exclude the wall-clock timestamp, which is intentionally non-seeded.
        a2 = {k: v for k, v in a.items() if k != "timestamp"}
        b2 = {k: v for k, v in b.items() if k != "timestamp"}
        assert a2 == b2

    def test_different_seed_different_episode(self):
        a = generate_episode("tool_augmented_planning", {"seed": 1})
        b = generate_episode("tool_augmented_planning", {"seed": 2})
        assert a["conversations"] != b["conversations"]

    def test_unknown_scenario_raises(self):
        try:
            generate_episode("does_not_exist", {"seed": 1})
        except KeyError:
            return
        raise AssertionError("expected KeyError for unknown scenario")


class TestBatch:
    def test_batch_size_and_distinctness(self):
        eps = generate_batch("tool_augmented_planning", n=5, base_seed=100)
        assert len(eps) == 5
        for ep in eps:
            assert validate_episode_schema(ep)
        # Distinct seeds -> distinct conversations.
        blobs = {json.dumps(e["conversations"], sort_keys=True) for e in eps}
        assert len(blobs) == 5

    def test_batch_is_deterministic(self):
        a = generate_batch("tool_augmented_planning", n=3, base_seed=7)
        b = generate_batch("tool_augmented_planning", n=3, base_seed=7)
        strip = lambda eps: [{k: v for k, v in e.items() if k != "timestamp"} for e in eps]
        assert strip(a) == strip(b)
