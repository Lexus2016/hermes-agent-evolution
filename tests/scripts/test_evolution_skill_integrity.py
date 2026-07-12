"""Enforced skill-integrity gate (scripts/evolution_skill_lint.py).

The real-repo test is the ENFORCEMENT: if any evolution skill ever instructs a
stage to run a script that doesn't exist, or a command in a stage without the
`terminal` toolset, CI fails — turning the lesson of #101/#188 from "the agent
should notice" into "merge is blocked".

``TestCheckSkillEditBudget`` / ``TestFindEditBudgetViolations`` cover the #907
(SkillOpt) bounded-edit gate: the pure per-cycle churn-ratio check, exercised
with synthetic diff stats (the git-diff IO boundary is untested here by design
— same convention as ``evolution_merge_gate.py``'s atomic-merge IO, which is
"exercised only via the pure policy" in its own test file).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_skill_lint import (  # noqa: E402
    check_skill_edit_budget,
    extract_run_commands,
    find_edit_budget_violations,
    find_violations,
    lint_repo,
    skill_ref_to_name,
)


class TestRealRepoEnforcement:
    def test_real_repo_has_no_dead_wiring(self):
        # Blocks merge if a skill gains a dead script reference or a command its
        # stage can't run. Also proves the #188 fix left nothing else broken.
        violations = lint_repo()
        assert violations == [], f"dead skill->script wiring: {violations}"


class TestExtractRunCommands:
    def test_python_and_python3(self):
        assert extract_run_commands("run python scripts/a.py now") == ["scripts/a.py"]
        assert extract_run_commands("python3 scripts/b.py --x") == ["scripts/b.py"]

    def test_bash_and_dotslash(self):
        assert extract_run_commands("bash scripts/c.sh") == ["scripts/c.sh"]
        assert extract_run_commands("./scripts/d.sh") == ["scripts/d.sh"]

    def test_ignores_bare_inline_mention(self):
        # The #101 cautionary note names a non-existent script in prose — NOT a
        # command. Must not be flagged.
        assert extract_run_commands("a `scripts/evolution_watchdog.sh` that never existed") == []

    def test_dedup_and_sorted(self):
        assert extract_run_commands("python scripts/z.py; python scripts/a.py; python scripts/z.py") == [
            "scripts/a.py",
            "scripts/z.py",
        ]


class TestFindViolations:
    def _stage(self, toolsets, skills=("evolution/research",)):
        return [{"name": "research", "skills": list(skills), "toolsets": list(toolsets)}]

    def test_missing_script_flagged(self):
        v = find_violations(
            self._stage(["web", "file", "terminal"]),
            {"evolution-research": "bash scripts/nope.sh"},
            {"scripts/other.py"},
        )
        assert len(v) == 1 and v[0]["kind"] == "missing_script"

    def test_command_without_terminal_flagged(self):  # the #188 case
        v = find_violations(
            self._stage(["web", "file"]),
            {"evolution-research": "python scripts/evolution_funnel.py --summary"},
            {"scripts/evolution_funnel.py"},
        )
        assert len(v) == 1 and v[0]["kind"] == "no_terminal"

    def test_existing_script_in_terminal_stage_is_ok(self):
        v = find_violations(
            self._stage(["web", "file", "terminal"]),
            {"evolution-research": "python scripts/evolution_funnel.py"},
            {"scripts/evolution_funnel.py"},
        )
        assert v == []

    def test_prose_mention_not_flagged_even_if_missing(self):
        v = find_violations(
            self._stage(["web", "file"]),
            {"evolution-research": "warning: `scripts/ghost.sh` never existed (see #101)"},
            set(),
        )
        assert v == []

    def test_missing_skill_md_flagged(self):
        v = find_violations(self._stage(["web", "file", "terminal"]), {}, set())
        assert len(v) == 1 and v[0]["kind"] == "missing_skill"


def test_skill_ref_to_name():
    assert skill_ref_to_name("evolution/research") == "evolution-research"
    assert skill_ref_to_name("evolution/upstream-sync") == "evolution-upstream-sync"


class TestCheckSkillEditBudget:
    """#907 (SkillOpt) — the per-cycle churn-ratio cap on one SKILL.md."""

    def test_small_edit_within_budget_is_clean(self):
        # 20 changed lines of 100 = 20%, well under the 50% default budget.
        assert check_skill_edit_budget("skills/x/SKILL.md", 100, 10, 10) is None

    def test_edit_exceeding_budget_is_flagged(self):
        v = check_skill_edit_budget("skills/x/SKILL.md", 100, 40, 20)  # 60%
        assert v is not None
        assert v["kind"] == "edit_budget_exceeded"
        assert v["path"] == "skills/x/SKILL.md"
        assert "60%" in v["detail"]

    def test_edit_at_exact_ratio_is_ok(self):
        # Exactly at the cap (50/100 = 50%) — inclusive, not a violation.
        assert check_skill_edit_budget("skills/x/SKILL.md", 100, 50, 0) is None

    def test_edit_just_over_ratio_is_flagged(self):
        v = check_skill_edit_budget("skills/x/SKILL.md", 100, 51, 0)
        assert v is not None and v["kind"] == "edit_budget_exceeded"

    def test_brand_new_skill_is_exempt(self):
        # before_lines == 0 -> creation, not a rewrite.
        assert check_skill_edit_budget("skills/x/SKILL.md", 0, 500, 0) is None

    def test_tiny_existing_skill_is_exempt(self):
        # Below MIN_SKILL_LINES_FOR_BUDGET (20) the ratio isn't meaningful yet.
        assert check_skill_edit_budget("skills/x/SKILL.md", 10, 10, 0) is None

    def test_custom_ratio_override(self):
        # 30% churn passes the 50% default but fails a stricter 20% budget.
        assert check_skill_edit_budget("skills/x/SKILL.md", 100, 30, 0) is None
        v = check_skill_edit_budget("skills/x/SKILL.md", 100, 30, 0, ratio=0.2)
        assert v is not None

    def test_custom_min_lines_override(self):
        # A 15-line skill is exempt by default (< 20) but covered once the
        # floor is lowered.
        assert check_skill_edit_budget("skills/x/SKILL.md", 15, 15, 0) is None
        v = check_skill_edit_budget("skills/x/SKILL.md", 15, 15, 0, min_lines=10)
        assert v is not None


class TestFindEditBudgetViolations:
    def test_mixed_diffs_only_flags_the_oversized_one(self):
        diffs = [
            {"path": "skills/a/SKILL.md", "before_lines": 100, "added": 10, "removed": 5},
            {"path": "skills/b/SKILL.md", "before_lines": 100, "added": 60, "removed": 10},
        ]
        v = find_edit_budget_violations(diffs)
        assert [x["path"] for x in v] == ["skills/b/SKILL.md"]

    def test_no_diffs_is_clean(self):
        assert find_edit_budget_violations([]) == []

    def test_missing_keys_default_safely(self):
        # A malformed entry (missing counts) defaults to 0 everywhere rather
        # than raising — before_lines=0 makes it exempt (looks "new").
        assert find_edit_budget_violations([{"path": "skills/x/SKILL.md"}]) == []
