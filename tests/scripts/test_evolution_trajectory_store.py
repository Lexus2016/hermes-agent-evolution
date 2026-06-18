"""Tests for scripts/evolution_trajectory_store.py (#321).

Positive-signal counterpart to ``test_evolution_trace_miner.py``: where the
miner turns recurring *failures* into weakness records, this store turns
recurring *successful* trajectories into improvement proposals. Pure +
deterministic; operates on the EXISTING ``trajectory_samples.jsonl`` schema
(``save_trajectory`` in ``agent/trajectory.py``) — no capture, no live model.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_trajectory_store import (  # noqa: E402
    TrajectoryStore,
    classify_task_type,
    extract_success_patterns,
    format_proposals,
)


# --------------------------------------------------------------------------- #
# fixtures: build entries in the exact save_trajectory()/ShareGPT shape.
# --------------------------------------------------------------------------- #
def _gpt_turn(text="", tool_names=None):
    """One assistant ('gpt') turn, optionally with embedded <tool_call> blocks."""
    value = f"<think>{text}</think>\n" if text else "<think></think>\n"
    for name in tool_names or []:
        call = json.dumps({"name": name, "arguments": {}}, ensure_ascii=False)
        value += f"<tool_call>\n{call}\n</tool_call>\n"
    return {"from": "gpt", "value": value.rstrip()}


def _entry(prompt, *gpt_turns, completed=True, model="m"):
    convs = [
        {"from": "system", "value": "tools..."},
        {"from": "human", "value": prompt},
    ]
    convs.extend(gpt_turns)
    return {
        "conversations": convs,
        "timestamp": "2026-06-18T00:00:00",
        "model": model,
        "completed": completed,
    }


def _write_jsonl(path, entries):
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
class TestClassifyTaskType:
    def test_coding_keywords(self):
        assert classify_task_type("Write a Python function to sort a list") == "coding"
        assert classify_task_type("Fix the bug in this code") == "coding"

    def test_research_keywords(self):
        assert classify_task_type("Research the latest papers on RLHF") == "research"

    def test_deployment_keywords(self):
        assert classify_task_type("Deploy the service to production") == "deployment"

    def test_unknown_falls_back(self):
        assert classify_task_type("Hello there") == "general"
        assert classify_task_type("") == "general"
        assert classify_task_type(None) == "general"


class TestStoreIndexing:
    def test_index_by_task_type(self, tmp_path):
        p = tmp_path / "trajectory_samples.jsonl"
        _write_jsonl(p, [
            _entry("Write a function to add two numbers", _gpt_turn("ok", ["run_tests"])),
            _entry("Fix the failing test in module x", _gpt_turn("ok", ["run_tests"])),
            _entry("Research transformer scaling laws", _gpt_turn("done")),
        ])
        store = TrajectoryStore.from_jsonl(p)
        assert store.count() == 3
        assert set(store.task_types()) == {"coding", "research"}
        assert len(store.by_type("coding")) == 2
        assert len(store.by_type("research")) == 1

    def test_only_successful_indexed(self, tmp_path):
        # a store over trajectory_samples.jsonl is successful-only by contract,
        # but defensively skip any entry flagged completed=False.
        p = tmp_path / "trajectory_samples.jsonl"
        _write_jsonl(p, [
            _entry("Write code for a parser", _gpt_turn("ok", ["run_tests"])),
            _entry("Write code that failed", _gpt_turn("nope"), completed=False),
        ])
        store = TrajectoryStore.from_jsonl(p)
        assert store.count() == 1
        assert len(store.by_type("coding")) == 1

    def test_query_unknown_type_empty(self, tmp_path):
        p = tmp_path / "trajectory_samples.jsonl"
        _write_jsonl(p, [_entry("Deploy to prod", _gpt_turn("ok", ["terminal"]))])
        store = TrajectoryStore.from_jsonl(p)
        assert store.by_type("coding") == []


class TestRobustness:
    def test_missing_file_is_empty(self, tmp_path):
        store = TrajectoryStore.from_jsonl(tmp_path / "does_not_exist.jsonl")
        assert store.count() == 0
        assert store.task_types() == []
        assert extract_success_patterns(store) == []

    def test_malformed_lines_tolerated(self, tmp_path):
        p = tmp_path / "trajectory_samples.jsonl"
        with open(p, "w", encoding="utf-8") as f:
            f.write("not json at all\n")
            f.write("{ broken json\n")
            f.write("\n")  # blank line
            f.write(json.dumps({"no": "conversations"}) + "\n")  # missing key
            f.write(json.dumps(_entry("Write code", _gpt_turn("ok", ["run_tests"]))) + "\n")
        store = TrajectoryStore.from_jsonl(p)
        assert store.count() == 1  # only the one valid coding entry survives

    def test_entry_without_human_turn_is_general(self, tmp_path):
        p = tmp_path / "trajectory_samples.jsonl"
        _write_jsonl(p, [{"conversations": [{"from": "system", "value": "x"}],
                          "completed": True}])
        store = TrajectoryStore.from_jsonl(p)
        assert store.count() == 1
        assert store.by_type("general")  # no human prompt -> general bucket


class TestExtractSuccessPatterns:
    def test_emits_proposal_for_recurring_tool_pattern(self, tmp_path):
        # 3/3 coding successes end by running tests -> a strong positive pattern.
        p = tmp_path / "trajectory_samples.jsonl"
        _write_jsonl(p, [
            _entry("Write add()", _gpt_turn("plan"), _gpt_turn("verify", ["run_tests"])),
            _entry("Fix parser bug", _gpt_turn("plan"), _gpt_turn("verify", ["run_tests"])),
            _entry("Implement cache", _gpt_turn("plan"), _gpt_turn("verify", ["run_tests"])),
        ])
        store = TrajectoryStore.from_jsonl(p)
        props = extract_success_patterns(store, min_support=3)
        assert len(props) >= 1
        prop = next(pr for pr in props if pr["task_type"] == "coding")
        # proposal envelope contract (mirrors harness_proposer / extract drafts).
        assert prop["source"] == "trajectory-success"
        assert prop["task_type"] == "coding"
        assert prop["tool"] == "run_tests"
        assert prop["support"] == 3 and prop["total"] == 3
        assert prop["fraction"] == 1.0
        assert "run_tests" in prop["title"]
        assert "run_tests" in prop["body"]
        # frequency phrasing like "3/3" appears in the rationale.
        assert "3/3" in prop["body"]

    def test_min_support_filters_rare_patterns(self, tmp_path):
        p = tmp_path / "trajectory_samples.jsonl"
        _write_jsonl(p, [
            _entry("Write add()", _gpt_turn("v", ["run_tests"])),
            _entry("Write sub()", _gpt_turn("v", ["git_commit"])),
        ])
        store = TrajectoryStore.from_jsonl(p)
        # each tool appears once in coding -> below the default threshold.
        assert extract_success_patterns(store, min_support=2) == []

    def test_proposals_sorted_by_support_desc(self, tmp_path):
        p = tmp_path / "trajectory_samples.jsonl"
        entries = []
        # 4 coding successes use run_tests, 2 of them also use read_file.
        for i in range(4):
            entries.append(_entry(f"Write func {i}", _gpt_turn("v", ["run_tests"])))
        for i in range(2):
            entries.append(_entry(f"Edit func {i}", _gpt_turn("v", ["read_file"])))
        _write_jsonl(p, entries)
        store = TrajectoryStore.from_jsonl(p)
        props = extract_success_patterns(store, min_support=2)
        coding = [pr for pr in props if pr["task_type"] == "coding"]
        supports = [pr["support"] for pr in coding]
        assert supports == sorted(supports, reverse=True)
        assert coding[0]["tool"] == "run_tests" and coding[0]["support"] == 4

    def test_no_raw_content_leaks(self, tmp_path):
        # proposals carry only task type, tool names, counts, and our templated
        # title/body — never raw trajectory prompt/answer text.
        secret = "SENSITIVE-USER-PROMPT-PsWd123"
        p = tmp_path / "trajectory_samples.jsonl"
        _write_jsonl(p, [
            _entry(f"Write code {secret}", _gpt_turn("v", ["run_tests"])),
            _entry(f"Fix code {secret}", _gpt_turn("v", ["run_tests"])),
        ])
        store = TrajectoryStore.from_jsonl(p)
        blob = json.dumps(extract_success_patterns(store, min_support=2))
        assert secret not in blob


class TestFormatProposals:
    def test_empty(self):
        assert "no recurring success patterns" in format_proposals([])

    def test_lists_proposals(self):
        props = [{
            "source": "trajectory-success", "task_type": "coding", "tool": "run_tests",
            "support": 3, "total": 3, "fraction": 1.0,
            "title": "[SUCCESS] coding tasks call `run_tests`",
            "body": "In 3/3 ...",
        }]
        out = format_proposals(props)
        assert "1 success pattern" in out and "run_tests" in out and "coding" in out
