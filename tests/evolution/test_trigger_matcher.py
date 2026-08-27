# -*- coding: utf-8 -*-
"""Unit tests for evolution.lib.trigger_matcher (#3229)."""

import pytest
from evolution.lib.trigger_matcher import (
    best_matches,
    extract_triggers,
    get_matching_skills,
    parse_triggers,
    render_triggers,
    score_state_against_skill,
    score_trigger,
)


def _skill(name, triggers=None, md=None):
    s = {"name": name}
    if triggers is not None:
        s["triggers"] = triggers
    if md is not None:
        s["skill_markdown"] = md
    return s


def _trig(ttype, values, weight=1.0):
    return {"type": ttype, "values": values, "weight": weight}


class TestExtractTriggers:
    def test_extracts_goal_and_tools(self):
        ts = extract_triggers({
            "goal": "Run the database migration and verify indexes",
            "tool_calls": [
                {"name": "read_file"},
                {"name": "terminal"},
                {"name": "terminal"},
            ],
        })
        assert any(t["type"] == "goal_contains" for t in ts)
        tool_t = next(t for t in ts if t["type"] == "tool_used")
        assert sorted(tool_t["values"]) == ["read_file", "terminal"]

    def test_ignores_malformed_trace(self):
        assert extract_triggers("not a dict") == []  # type: ignore[arg-type]
        assert extract_triggers({"tool_calls": None}) == []
        assert (
            extract_triggers({
                "goal": "The and of to in on a an can please help me with this"
            })
            == []
        )


class TestParseAndRenderTriggers:
    def test_round_trip(self):
        ts = [
            _trig("goal_contains", ["migration", "index"]),
            _trig("tool_used", ["terminal", "read_file"], 0.8),
        ]
        rendered = render_triggers(ts)
        assert "triggers:" in rendered and "goal_contains" in rendered
        assert parse_triggers(rendered) == ts

    def test_parse_from_full_skill_markdown(self):
        md = "---\nname: migrate-db\ntriggers:\n  - type: goal_contains\n    values: [migration, database]\n    weight: 1\n  - type: tool_used\n    values: [terminal, read_file]\n    weight: 0.8\n---\n# DB migration\n"
        ts = parse_triggers(md)
        assert len(ts) == 2
        assert ts[0]["type"] == "goal_contains" and ts[0]["values"] == [
            "migration",
            "database",
        ]
        assert ts[1]["type"] == "tool_used"

    def test_parse_empty_and_render_skips_invalid(self):
        assert parse_triggers("no frontmatter") == []
        assert parse_triggers("---\nname: x\n---\n") == []
        rendered = render_triggers([
            _trig("goal_contains", ["ok"]),
            _trig("unknown_type", ["x"]),
            _trig("goal_contains", []),
        ])
        assert "unknown_type" not in rendered and "ok" in rendered


class TestScoreTrigger:
    def test_goal_contains_fires(self):
        assert (
            score_trigger(
                {"goal": "Migrate the database schema", "tools_used": []},
                _trig("goal_contains", ["migrate", "schema"]),
            )
            == 1.0
        )
        assert score_trigger(
            {"goal": "Migrate the database schema", "tools_used": []},
            _trig("goal_contains", ["MIGRATE"], 0.9),
        ) == pytest.approx(0.9)
        assert score_trigger(
            {"goal": "", "tools_used": ["terminal", "read_file"]},
            _trig("tool_used", ["terminal"], 0.8),
        ) == pytest.approx(0.8)
        assert (
            score_trigger(
                {"goal": "Deploy web app", "tools_used": ["terminal"]},
                _trig("goal_contains", ["migration"]),
            )
            == 0.0
        )

    def test_error_class_matching(self):
        assert (
            score_trigger(
                {"error_class": "database_connection_timeout"},
                _trig("error_class", ["database_connection", "timeout"]),
            )
            == 1.0
        )
        assert (
            score_trigger(
                {"last_error": "ConnectionRefusedError: port 5432"},
                _trig("error_class", ["ConnectionRefusedError"]),
            )
            == 1.0
        )

    def test_task_kind_matching(self):
        assert (
            score_trigger(
                {"task_kind": "database_migration"},
                _trig("task_kind", ["database_migration"]),
            )
            == 1.0
        )

    def test_intent_matching(self):
        assert (
            score_trigger(
                {"intent": "fix broken tests in CI"},
                _trig("intent", ["tests", "ci"]),
            )
            == 1.0
        )

    def test_tool_constellation_matching(self):
        assert (
            score_trigger(
                {"tool_constellation": ["terminal", "browser_navigate", "read_file"]},
                _trig("tool_used", ["browser_navigate"]),
            )
            == 1.0
        )


class TestStateAndMatchingSkills:
    def test_score_state_against_skill(self):
        skill = _skill("db-ops", [_trig("goal_contains", ["database"])])
        state = {"goal": "Optimize the database query"}
        assert score_state_against_skill(state, skill) == 1.0
        assert score_state_against_skill({}, skill) == 0.0

    def test_get_matching_skills_top_k(self):
        skills = [
            _skill("db-1", [_trig("goal_contains", ["db"], 0.8)]),
            _skill("db-2", [_trig("goal_contains", ["db"], 0.95)]),
            _skill("db-3", [_trig("goal_contains", ["db"], 0.9)]),
            _skill("other", [_trig("goal_contains", ["other"], 0.9)]),
        ]
        matches = get_matching_skills({"goal": "fix db index"}, skills, threshold=0.7, top_k=2)
        assert len(matches) == 2
        assert matches[0][0]["name"] == "db-2"
        assert matches[1][0]["name"] == "db-3"


class TestBestMatches:
    def test_ranks_by_best_trigger(self):
        state = {"goal": "Migrate the database", "tools_used": ["terminal"]}
        skills = [
            _skill("db-migrate", [_trig("goal_contains", ["migrate"])]),
            _skill("web-deploy", [_trig("goal_contains", ["deploy"])]),
            _skill(
                "db-verify",
                md="---\ntriggers:\n  - type: goal_contains\n    values: [database]\n    weight: 0.9\n---\n# DB verify\n",
            ),
        ]
        result = best_matches(state, skills, threshold=0.7)
        assert [r[0] for r in result] == ["db-migrate", "db-verify"]
        assert result[0][1] == 1.0 and result[1][1] == pytest.approx(0.9)
        state2 = {"goal": "Migrate", "tools_used": []}
        skills2 = [
            _skill("exact", [_trig("goal_contains", ["migrate"])]),
            _skill("weak", [_trig("goal_contains", ["not-here"], 0.6)]),
        ]
        assert best_matches(state2, skills2, threshold=0.7) == [("exact", 1.0)]
        state3 = {"goal": "abc", "tools_used": []}
        skills3 = [
            _skill("zebra", [_trig("goal_contains", ["abc"])]),
            _skill("apple", [_trig("goal_contains", ["abc"])]),
        ]
        assert [r[0] for r in best_matches(state3, skills3, threshold=0.7)] == [
            "apple",
            "zebra",
        ]

