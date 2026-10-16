"""Replay skill routing contracts against an immutable pre-edit baseline (#3462)."""
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.skill_contracts import check_contract_edit


def check_repository_edits(root, base="origin/main"):
    root = Path(root)
    def git(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
                              errors="strict", check=True).stdout
    try:
        baseline = git("rev-parse", "--verify", f"{base}^{{commit}}").strip()
        paths = set(git("diff", "--name-only", "--no-renames", "-z", baseline, "--", "skills/", "optional-skills/").split("\0"))
        paths.update(git("ls-files", "--others", "--exclude-standard", "-z", "--", "skills/", "optional-skills/").split("\0"))
        errors = []
        for path in sorted(paths):
            if not path.endswith("/SKILL.md"):
                continue
            existed = bool(git("ls-tree", baseline, "--", path).strip())
            old = git("show", f"{baseline}:{path}") if existed else None
            target = root / path
            if target.is_symlink():
                errors.append(f"{path}: symbolic-link skills require manual review")
                continue
            new = target.read_text(encoding="utf-8-sig") if target.exists() else ""
            if error := check_contract_edit(new, old):
                errors.append(f"{path}: {error}")
        return errors
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        return [f"Skill contract baseline unavailable; cannot verify replay: {exc}"]
