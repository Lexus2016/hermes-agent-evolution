"""Behavior contracts for the co-evolution loop (#2262, parent #2251)."""

import json
from unittest.mock import MagicMock

from agent.coevolution import (
    record_delegation_and_tools,
    suggest_configuration,
    tool_usage_tags_path,
)


class _FakePattern:
    """Minimal stand-in for experience_bank.DelegationPattern."""

    def __init__(self, task_type, role="leaf"):
        self.task_type = task_type
        self.role = role

    def to_dict(self):
        return {"task_type": self.task_type, "role": self.role}


def _trace(*names):
    return [{"tool": name, "status": "ok"} for name in names]


class TestRecordDelegationAndTools:
    def test_writes_tags_file_and_delegates_to_bank(self, tmp_path):
        bank = MagicMock()
        bank.record_delegation_outcome.return_value = True
        tags = tmp_path / "tool_usage_tags.json"

        ok = record_delegation_and_tools(
            session_key="telegram:42",
            goal="Parse CSV Files With Pandas",
            outcome={"status": "completed", "completed": True},
            tool_calls=_trace("read_file", "terminal"),
            bank=bank,
            tags_path=tags,
        )

        assert ok is True
        bank.record_delegation_outcome.assert_called_once_with(
            task_type="parse csv files with pandas",
            role="leaf",
            model="",
            goal_template="Parse CSV Files With Pandas",
            success=True,
        )
        data = json.loads(tags.read_text(encoding="utf-8"))
        task = data["tasks"]["parse csv files with pandas"]
        assert task["last_session"] == "telegram:42"
        assert task["tools"]["read_file"] == {"success": 1, "total": 1}
        assert task["tools"]["terminal"] == {"success": 1, "total": 1}

    def test_failed_outcome_tags_tools_as_unsuccessful(self, tmp_path):
        bank = MagicMock()
        bank.record_delegation_outcome.return_value = True
        tags = tmp_path / "tool_usage_tags.json"

        record_delegation_and_tools(
            session_key="cli",
            goal="scrape the docs site",
            outcome={"status": "failed", "completed": False},
            tool_calls=_trace("browser_navigate"),
            bank=bank,
            tags_path=tags,
        )

        data = json.loads(tags.read_text(encoding="utf-8"))
        assert data["tasks"]["scrape the docs site"]["tools"]["browser_navigate"] == {
            "success": 0,
            "total": 1,
        }
        assert bank.record_delegation_outcome.call_args.kwargs["success"] is False

    def test_aggregates_across_recordings(self, tmp_path):
        bank = MagicMock()
        tags = tmp_path / "tool_usage_tags.json"
        for outcome in ({"status": "completed"}, {"status": "failed"}):
            record_delegation_and_tools(
                session_key="s",
                goal="same goal",
                outcome=outcome,
                tool_calls=_trace("terminal"),
                bank=bank,
                tags_path=tags,
            )
        data = json.loads(tags.read_text(encoding="utf-8"))
        assert data["tasks"]["same goal"]["tools"]["terminal"] == {
            "success": 1,
            "total": 2,
        }

    def test_fail_open_on_bank_error(self, tmp_path):
        bank = MagicMock()
        bank.record_delegation_outcome.side_effect = RuntimeError("bank down")
        tags = tmp_path / "tool_usage_tags.json"

        ok = record_delegation_and_tools(
            session_key="s",
            goal="g",
            outcome={"status": "completed"},
            tool_calls=_trace("terminal"),
            bank=bank,
            tags_path=tags,
        )

        assert ok is False  # reported, not raised
        # The tool-bank half still records (independent stores).
        data = json.loads(tags.read_text(encoding="utf-8"))
        assert data["tasks"]["g"]["tools"]["terminal"]["total"] == 1


class TestSuggestConfiguration:
    def _seeded_store(self, tmp_path, bank):
        bank.record_delegation_outcome.return_value = True
        record_delegation_and_tools(
            session_key="s1",
            goal="parse csv files with pandas",
            outcome={"status": "completed"},
            tool_calls=_trace("read_file", "terminal"),
            bank=bank,
            tags_path=tmp_path / "tags.json",
        )
        record_delegation_and_tools(
            session_key="s2",
            goal="parse csv files with pandas",
            outcome={"status": "failed"},
            tool_calls=_trace("browser_navigate"),
            bank=bank,
            tags_path=tmp_path / "tags.json",
        )

    def test_returns_tools_and_patterns_for_similar_goal(self, tmp_path):
        bank = MagicMock()
        self._seeded_store(tmp_path, bank)
        bank.find_matching_delegation_patterns.return_value = [
            _FakePattern("parse csv files with pandas")
        ]

        suggestion = suggest_configuration(
            "parse csv files", bank=bank, tags_path=tmp_path / "tags.json"
        )

        assert set(suggestion["suggested_tools"]) == {"read_file", "terminal"}
        assert suggestion["matched_patterns"] == [
            {"task_type": "parse csv files with pandas", "role": "leaf"}
        ]

    def test_empty_for_novel_goal_and_never_writes(self, tmp_path):
        bank = MagicMock()
        bank.find_matching_delegation_patterns.return_value = []
        tags = tmp_path / "tags.json"

        suggestion = suggest_configuration(
            "refactor quantum foam solver", bank=bank, tags_path=tags
        )

        assert suggestion == {"suggested_tools": [], "matched_patterns": []}
        assert not tags.exists()  # retrieval-only: no store created

    def test_corrupted_tags_degrade_to_empty(self, tmp_path):
        bank = MagicMock()
        bank.find_matching_delegation_patterns.return_value = []
        tags = tmp_path / "tags.json"
        tags.write_text("{not valid json!!", encoding="utf-8")

        suggestion = suggest_configuration(
            "parse csv files", bank=bank, tags_path=tags
        )

        assert suggestion == {"suggested_tools": [], "matched_patterns": []}


class TestPaths:
    def test_tool_usage_tags_path_is_profile_aware(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        assert tool_usage_tags_path() == (
            tmp_path / "evolution" / "coevolution" / "tool_usage_tags.json"
        )
