"""Enforced skill-integrity gate (scripts/evolution_skill_lint.py).

The real-repo test is the ENFORCEMENT: if any evolution skill ever instructs a
stage to run a script that doesn't exist, or a command in a stage without the
`terminal` toolset, CI fails — turning the lesson of #101/#188 from "the agent
should notice" into "merge is blocked".
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_skill_lint import (  # noqa: E402
    extract_run_commands,
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
