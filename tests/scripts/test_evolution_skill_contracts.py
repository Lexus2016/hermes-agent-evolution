"""Repository replay runs on actual git baselines, including skill-only edits."""
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from evolution_skill_contracts import check_repository_edits
from evolution_pre_pr_test_runner import main

BASE = '''---
name: example
description: Example routing
contract:
  version: 1
  intent: Research
  behavioral_criteria: [routing-applicability]
  applies_to: [research]
  excludes: [email]
  protected_constraints: [Keep sources.]
  replay_cases:
    - {id: positive, task: research, applicable: true}
---
Keep sources.
'''


@pytest.fixture
def repo(tmp_path):
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True, encoding="utf-8").stdout.strip()
    git("init")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    target = tmp_path / "skills/example/SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text(BASE, encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "baseline")
    return tmp_path, target, git("rev-parse", "HEAD")


@pytest.mark.parametrize("change", ["remove_contract", "delete", "rename", "rewrite", "replay"])
def test_pre_pr_rejects_skill_only_regression_without_mutation(repo, change):
    root, target, base = repo
    if change == "delete":
        target.unlink()
    elif change == "rename":
        target.rename(target.with_name("OLD.md"))
    elif change == "remove_contract":
        target.write_text("---\nname: example\ndescription: example\n---\nKeep sources.\n", encoding="utf-8")
    elif change == "replay":
        target.write_text(BASE.replace("version: 1", "version: 2").replace("applies_to: [research]", "applies_to: []"), encoding="utf-8")
    else:
        target.write_text(BASE + "Changed without version bump.\n", encoding="utf-8")
    before = {p.relative_to(root): p.read_bytes() for p in root.glob("skills/**/*") if p.is_file()}
    assert check_repository_edits(root, base)
    assert main(["--repo-root", str(root), "--contract-base", base, "--changed-files", "skills/example/SKILL.md"]) == 1
    assert before == {p.relative_to(root): p.read_bytes() for p in root.glob("skills/**/*") if p.is_file()}


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_valid_versioned_edit_and_missing_baseline(repo, encoding):
    root, target, base = repo
    target.write_text(BASE.replace("version: 1", "version: 2") + "More detail.\n", encoding=encoding)
    assert check_repository_edits(root, base) == []
    assert check_repository_edits(root, "missing-ref")
