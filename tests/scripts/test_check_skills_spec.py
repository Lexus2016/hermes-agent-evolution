"""Tests for the Agent Skills spec validator (#125).

Validates that skills/**/SKILL.md files satisfy the open agentskills.io
contract (name + description frontmatter, folder-name match, extension
namespace) so malformed skill manifests fail fast in CI.
"""

import json

from scripts.check_skills_spec import (
    check_skill_file,
    extract_frontmatter,
    scan_skills_tree,
)

GOOD_SKILL = """---
name: test-skill
description: A perfectly spec-compliant test skill.
metadata:
  hermes:
    tags: [test]
---

# Test Skill

Instructions here.
"""


def _check(p):
    violations, warnings = check_skill_file(p, p.parent)
    return violations, warnings


def test_extract_frontmatter_parses_yaml(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text(GOOD_SKILL, encoding="utf-8")
    raw, meta = extract_frontmatter(p.read_text(encoding="utf-8"))
    assert raw is not None
    assert meta is not None
    assert meta["name"] == "test-skill"
    assert meta["description"].startswith("A perfectly")


def test_missing_frontmatter_is_a_violation(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text("# No frontmatter\n\nJust text.\n", encoding="utf-8")
    violations, _ = _check(p)
    assert len(violations) == 1
    assert "no YAML frontmatter" in violations[0].message


def test_missing_name_and_description(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text("---\nversion: 1.0.0\n---\n\n# Body\n", encoding="utf-8")
    violations, _ = _check(p)
    msgs = [v.message for v in violations]
    assert any("missing required 'name'" in m for m in msgs)
    assert any("missing required 'description'" in m for m in msgs)


def test_folder_name_mismatch(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text(GOOD_SKILL, encoding="utf-8")  # name: test-skill
    violations, _ = _check(p)  # folder is tmp_path root → mismatch
    assert any("does not match folder name" in v.message for v in violations)


def test_compliant_skill_passes(tmp_path):
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    p = skill_dir / "SKILL.md"
    p.write_text(GOOD_SKILL, encoding="utf-8")
    violations, warnings = _check(p)
    assert violations == []
    assert warnings == []


def test_non_spec_top_level_field_warns(tmp_path):
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    p = skill_dir / "SKILL.md"
    p.write_text(
        GOOD_SKILL.replace(
            "metadata:\n  hermes:\n    tags: [test]\n",
            "hermes_only_setting: true\nmetadata:\n  hermes:\n    tags: [test]\n",
        ),
        encoding="utf-8",
    )
    violations, warnings = _check(p)
    assert violations == []  # advisory only — never a hard failure
    assert any(
        "non-spec top-level field 'hermes_only_setting'" in w.message for w in warnings
    )


def test_scan_skills_tree_collects_all(tmp_path):
    good = tmp_path / "skills" / "test-skill"
    good.mkdir(parents=True)
    (good / "SKILL.md").write_text(GOOD_SKILL, encoding="utf-8")

    bad = tmp_path / "skills" / "bad-skill"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_text("---\nversion: 1.0.0\n---\n", encoding="utf-8")

    report = scan_skills_tree(tmp_path / "skills")
    assert len(report.violations) == 2  # bad-skill: missing name + description
    assert report.ok is False
    assert all(v.skill == "bad-skill" for v in report.violations)


def test_unparseable_yaml_reported(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text("---\nname: [unclosed\n---\n", encoding="utf-8")
    violations, _ = _check(p)
    assert any("not valid YAML" in v.message for v in violations)


def test_violations_serializable(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text("# No frontmatter\n", encoding="utf-8")
    violations, _ = _check(p)
    json.dumps([v.__dict__ for v in violations])  # must not raise


def test_cli_exit_codes(tmp_path, capsys):
    from scripts.check_skills_spec import main

    # Empty skills dir → pass
    skills = tmp_path / "skills"
    skills.mkdir()
    assert main([str(skills)]) == 0

    # Non-compliant skill → fail with SPEC_VIOLATION lines on stdout
    bad = skills / "bad-skill"
    bad.mkdir()
    (bad / "SKILL.md").write_text("no frontmatter at all\n", encoding="utf-8")
    assert main([str(skills)]) == 1
    out = capsys.readouterr().out
    assert "SPEC_VIOLATION" in out
    assert "skill spec violation(s) found" in out


def test_cli_warnings_do_not_fail(tmp_path, capsys):
    from scripts.check_skills_spec import main

    skills = tmp_path / "skills"
    good = skills / "test-skill"
    good.mkdir(parents=True)
    (good / "SKILL.md").write_text(
        GOOD_SKILL.replace(
            "metadata:\n  hermes:\n    tags: [test]\n",
            "author: Someone\nmetadata:\n  hermes:\n    tags: [test]\n",
        ),
        encoding="utf-8",
    )
    assert main([str(skills)]) == 0  # warning-only → exit 0
    out = capsys.readouterr().out
    assert "SKILL_SPEC_WARNING" in out
    assert "advisory only" in out
