"""Tests for skill-schema progressive disclosure (issue #303, child of #229).

The skills system already had two tiers:
  * ``skills_list``                     -> name + description (tier 1)
  * ``skill_view(name)``                -> full SKILL.md body (tier 3)

This adds the cheap middle tier:
  * ``skill_view(name, schema_only=True)`` -> name + description + structured
    schema (inputs / outputs / examples / required env), WITHOUT the body.

These tests cover the new ``extract_skill_schema`` helper, the ``schema_only``
short-circuit in ``skill_view``, the additive ``schema`` field on full loads,
and that all the existing security/disabled/platform gates still fire.
"""

import json
from unittest.mock import patch

from tools.skills_tool import (
    extract_skill_schema,
    skill_view,
)


def _make_skill(skills_dir, name, frontmatter_extra="", body="Step 1: Do the thing."):
    """Create a minimal skill directory (mirrors test_skills_tool helper)."""
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    content = f"""\
---
name: {name}
description: Description for {name}.
{frontmatter_extra}---

# {name}

{body}
"""
    (skill_dir / "SKILL.md").write_text(content)
    return skill_dir


# A SKILL.md frontmatter block declaring a full schema (inputs/outputs/examples).
_SCHEMA_FRONTMATTER = (
    "inputs:\n"
    "  - name: dataset_path\n"
    "    description: Path to the training data.\n"
    "outputs:\n"
    "  - name: model_dir\n"
    "    description: Directory of the fine-tuned model.\n"
    "examples:\n"
    "  - \"skill_view('demo')\"\n"
    "  - description: Train on a CSV\n"
    "    command: run --data foo.csv\n"
)


# ---------------------------------------------------------------------------
# extract_skill_schema
# ---------------------------------------------------------------------------


class TestExtractSkillSchema:
    def test_extracts_inputs_outputs_examples(self):
        frontmatter, _ = _parse(_SCHEMA_FRONTMATTER)
        schema = extract_skill_schema(frontmatter)
        assert schema["inputs"][0]["name"] == "dataset_path"
        assert schema["outputs"][0]["name"] == "model_dir"
        # str and dict examples both survive, in order
        assert schema["examples"][0] == "skill_view('demo')"
        assert schema["examples"][1]["command"] == "run --data foo.csv"

    def test_empty_when_no_schema_fields(self):
        frontmatter, _ = _parse("")
        assert extract_skill_schema(frontmatter) == {}

    def test_required_env_folded_into_schema(self):
        fm = {
            "required_environment_variables": [
                {"name": "API_KEY", "prompt": "Enter API key"}
            ]
        }
        schema = extract_skill_schema(fm)
        assert schema["required_environment_variables"][0]["name"] == "API_KEY"

    def test_blank_and_bad_examples_dropped(self):
        fm = {"examples": ["good", "", "  ", 42, {"x": 1}]}
        schema = extract_skill_schema(fm)
        assert schema["examples"] == ["good", {"x": 1}]

    def test_single_example_coerced_to_list(self):
        fm = {"examples": "only-one"}
        assert extract_skill_schema(fm)["examples"] == ["only-one"]


def _parse(frontmatter_body):
    """Build full SKILL.md content from a frontmatter body and parse it."""
    from tools.skills_tool import _parse_frontmatter

    content = f"---\nname: x\ndescription: d.\n{frontmatter_body}---\n\n# x\n\nBody.\n"
    return _parse_frontmatter(content)


# ---------------------------------------------------------------------------
# skill_view(schema_only=True)
# ---------------------------------------------------------------------------


class TestSchemaOnlyLoad:
    def test_schema_only_returns_schema_without_body(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(
                tmp_path,
                "schema-skill",
                frontmatter_extra=_SCHEMA_FRONTMATTER,
                body="SECRET BODY MARKER should not be loaded.",
            )
            raw = skill_view("schema-skill", schema_only=True)
        result = json.loads(raw)
        assert result["success"] is True
        assert result["schema_only"] is True
        assert result["name"] == "schema-skill"
        assert result["description"] == "Description for schema-skill."
        # The cheap tier must NOT carry the full body.
        assert "content" not in result
        assert "SECRET BODY MARKER" not in raw
        # But it MUST carry the structured schema.
        assert result["schema"]["inputs"][0]["name"] == "dataset_path"
        assert result["schema"]["outputs"][0]["name"] == "model_dir"

    def test_schema_only_empty_schema_when_none_declared(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(tmp_path, "bare-skill")
            raw = skill_view("bare-skill", schema_only=True)
        result = json.loads(raw)
        assert result["success"] is True
        assert result["schema"] == {}
        assert "content" not in result

    def test_schema_only_respects_disabled_gate(self, tmp_path):
        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch("tools.skills_tool._is_skill_disabled", return_value=True),
        ):
            _make_skill(tmp_path, "hidden", frontmatter_extra=_SCHEMA_FRONTMATTER)
            raw = skill_view("hidden", schema_only=True)
        result = json.loads(raw)
        assert result["success"] is False
        assert "disabled" in result["error"].lower()

    def test_schema_only_nonexistent_skill(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            raw = skill_view("nope", schema_only=True)
        result = json.loads(raw)
        assert result["success"] is False

    def test_schema_only_ignored_when_file_path_given(self, tmp_path):
        """file_path is already a scoped load; schema_only must not hijack it."""
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            skill_dir = _make_skill(tmp_path, "with-ref")
            refs = skill_dir / "references"
            refs.mkdir()
            (refs / "guide.md").write_text("REF FILE CONTENT")
            raw = skill_view(
                "with-ref", file_path="references/guide.md", schema_only=True
            )
        result = json.loads(raw)
        assert result["success"] is True
        # We got the linked file, not the schema preview.
        assert result["content"] == "REF FILE CONTENT"
        assert "schema_only" not in result


# ---------------------------------------------------------------------------
# Full skill_view still carries schema (additive, non-regressive)
# ---------------------------------------------------------------------------


class TestFullViewSchemaField:
    def test_full_view_includes_schema_when_declared(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(
                tmp_path, "full-schema", frontmatter_extra=_SCHEMA_FRONTMATTER
            )
            raw = skill_view("full-schema")
        result = json.loads(raw)
        assert result["success"] is True
        # Full body is present (tier 3) ...
        assert "Step 1" in result["content"]
        # ... and the schema is surfaced alongside it (same shape as tier 2).
        assert result["schema"]["inputs"][0]["name"] == "dataset_path"

    def test_full_view_omits_schema_when_absent(self, tmp_path):
        """Skills with no schema keep the lean legacy response (no empty key)."""
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(tmp_path, "no-schema")
            raw = skill_view("no-schema")
        result = json.loads(raw)
        assert result["success"] is True
        assert "schema" not in result


# ---------------------------------------------------------------------------
# Registry handler: schema_only is a browse (view), not a use
# ---------------------------------------------------------------------------


class TestHandlerUsageBump:
    def test_schema_only_bumps_view_not_use(self, tmp_path):
        from tools.skills_tool import _skill_view_with_bump

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(tmp_path, "browsed", frontmatter_extra=_SCHEMA_FRONTMATTER)
            with (
                patch("tools.skill_usage.bump_view") as mock_view,
                patch("tools.skill_usage.bump_use") as mock_use,
            ):
                _skill_view_with_bump({"name": "browsed", "schema_only": True})
        mock_view.assert_called_once_with("browsed")
        mock_use.assert_not_called()

    def test_full_view_bumps_both_view_and_use(self, tmp_path):
        from tools.skills_tool import _skill_view_with_bump

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(tmp_path, "loaded")
            with (
                patch("tools.skill_usage.bump_view") as mock_view,
                patch("tools.skill_usage.bump_use") as mock_use,
            ):
                _skill_view_with_bump({"name": "loaded"})
        mock_view.assert_called_once_with("loaded")
        mock_use.assert_called_once_with("loaded")
