"""Executable routing contracts, immutable history, and real mutation boundaries."""
import copy
import json
from unittest.mock import patch

import pytest
import hermes_yaml as yaml

from tools.skill_contracts import applicability, check_contract_edit
from tools.skill_retrieval import rank_skills
from tools.skill_manager_tool import _edit_skill, _delete_skill, _remove_file


def contract():
    return dict(version=1, intent="Extract research safely", behavioral_criteria=["routing-applicability"],
                applies_to=["research"], excludes=["email"], protected_constraints=["Keep sources."],
                replay_cases=[dict(id="positive", task="research", applicable=True),
                              dict(id="negative", task="email", applicable=False),
                              dict(id="mixed", task="research email", applicable=None)])


def content(c=None, name="research", body="Keep sources."):
    fm = dict(name=name, description="Research email extract ideas")
    if c is not None:
        fm["contract"] = c
    return "---\n" + yaml.safe_dump(fm) + "---\n" + body + "\n"


def test_replay_and_unknown_are_actual_routing_behavior():
    c = contract()
    assert check_contract_edit(content(c)) is None
    assert applicability(c, "EMAIL") is False
    assert applicability(c, "research email") is None
    assert applicability(c, "unknown") is None
    assert applicability(c, "emails") is None
    for query, present in [("email", False), ("research email", True), ("extract ideas", True)]:
        candidates = [dict(name="research", description="Research email extract ideas", contract=c)]
        assert bool(rank_skills(query, candidates=candidates, use_usage_signal=False)) is present
    assert rank_skills("email", candidates=[dict(name="legacy", description="email")], use_usage_signal=False)


@pytest.mark.parametrize("mutation", ["remove", "rename", "constraint", "case", "version", "replay", "body"])
def test_edit_rejections_preserve_original(tmp_path, mutation):
    c = contract()
    original = content(c)
    new = copy.deepcopy(c)
    new["version"] = 2
    name, body = "research", "Keep sources."
    if mutation == "remove":
        new = None
    elif mutation == "rename":
        name = "replacement"
    elif mutation == "constraint":
        new["protected_constraints"] = []
    elif mutation == "case":
        new["replay_cases"].pop()
    elif mutation == "version":
        new["version"] = 1
        body += " More."
    elif mutation == "replay":
        new["excludes"] = []
    else:
        body = "No original instruction."
    proposed = content(new, name, body)
    assert check_contract_edit(proposed, original)
    skill = tmp_path / "research"
    skill.mkdir()
    target = skill / "SKILL.md"
    target.write_text(original, encoding="utf-8")
    with patch("tools.skill_manager_tool.SKILLS_DIR", tmp_path), patch("agent.skill_utils.get_all_skills_dirs", return_value=[tmp_path]):
        result = _edit_skill("research", proposed)
    assert result["success"] is False
    assert target.read_text(encoding="utf-8") == original


def test_contract_delete_rejected_and_legacy_edits_allowed(tmp_path):
    skill = tmp_path / "research"
    skill.mkdir()
    target = skill / "SKILL.md"
    original = content(contract())
    target.write_text(original, encoding="utf-8")
    with patch("tools.skill_manager_tool.SKILLS_DIR", tmp_path), patch("agent.skill_utils.get_all_skills_dirs", return_value=[tmp_path]):
        assert _delete_skill("research")["success"] is False
        assert _remove_file("research", "SKILL.md")["success"] is False
    assert target.read_text(encoding="utf-8") == original
    assert check_contract_edit(content(body="Changed"), content()) is None
    c = contract()
    old = content(c)
    c["version"] = 2
    assert check_contract_edit(content(c, body="Keep sources. More detail."), old) is None


@pytest.mark.parametrize("field,value", [("version", True), ("replay_cases", []), ("replay_cases", [dict(id="x", task="research", applicable=True)] * 33), ("applies_to", ["x"] * 17), ("intent", "x" * 513)])
def test_complexity_and_schema_bounds(field, value):
    c = contract()
    c[field] = value
    assert check_contract_edit(content(c))


@pytest.mark.parametrize("padding", ["", "x" * 4500])
def test_live_registry_search_consumes_contract(tmp_path, padding):
    from tools.skills_tool import skills_search
    skill = tmp_path / "research"
    skill.mkdir()
    (skill / "SKILL.md").write_text(content(contract()).replace("---\n", "---\nextra: " + padding + "\n", 1), encoding="utf-8")
    with patch("tools.skills_tool.SKILLS_DIR", tmp_path), patch("agent.skill_utils.get_all_skills_dirs", return_value=[tmp_path]):
        assert json.loads(skills_search("email"))["skills"] == []
        result = json.loads(skills_search("research email"))
        assert any(s["name"] == "research" for s in result["skills"])
