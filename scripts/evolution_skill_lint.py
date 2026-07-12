#!/usr/bin/env python3
"""Deterministic skill-integrity gate for the evolution pipeline.

An ENFORCED check (not an instruction the agent might follow) for the class of
bug that self-review keeps missing: a skill tells a stage to RUN a script that
either does not exist, or that the stage cannot run because it lacks the
``terminal`` toolset. This has bitten the project twice:

  * #101 — a skill cited ``scripts/evolution_watchdog.sh`` that never existed,
    and a wanted issue was destructively closed on the fabricated path.
  * #188 — ``evolution-research`` (toolsets: web+file, NO terminal) was wired to
    "run ``python scripts/evolution_funnel.py --summary``" — a dead instruction
    it could never execute; unit tests passed, the integration was non-functional.

CI self-tests cover code; they do NOT cover "does the stage that runs this skill
have the capability the skill assumes". This closes that gap MECHANICALLY: run it
in CI (see tests/scripts/test_evolution_skill_integrity.py) so a dead skill→script
wiring blocks merge instead of relying on the agent to notice.

Scope is deliberately narrow + precise to avoid false positives: it flags only
COMMAND-shaped invocations (``python scripts/X.py``, ``bash scripts/X.sh``,
``./scripts/X``) — NOT bare inline-code mentions in cautionary prose (e.g. the
``scripts/evolution_watchdog.sh`` named in #101's warning is correctly ignored).

Pure functions + explicit IO boundary so it is import-safe and unit-testable.

Part 2 — bounded skill edits (#907, SkillOpt)
----------------------------------------------
SkillOpt (arXiv:2605.23904) treats a skill's system-prompt text as an optimized
parameter and argues for an "edit budget": bounded add/delete/replace changes per
cycle instead of unstable full rewrites. This adds that ONE deterministic,
CI-testable piece of the proposal — a per-cycle churn cap on ``skills/**/SKILL.md``
— without the rest of SkillOpt (held-out validation sets, a rejection buffer,
cross-model validation), which need an LLM-driven eval harness and are out of
scope for a mechanical gate. ``check_skill_edit_budget`` / ``find_edit_budget_violations``
are the pure core (unit-tested with synthetic diff stats); ``lint_skill_edit_budget``
is the git-diff IO boundary, run via ``--skill-edit-budget`` (not part of the
default ``lint_repo()`` static check, since it is inherently relative to a base
ref — see ``main()``).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# A script invocation the stage is told to RUN — `python/python3/bash/sh scripts/X`
# or `./scripts/X`. Bare inline mentions (no runner, no `./`) are NOT commands.
_RUN_RE = re.compile(r"(?:(?:python3?|bash|sh)\s+|\./)(scripts/[A-Za-z0-9_./-]+\.(?:py|sh))")

# Frontmatter `name:` line in a SKILL.md.
_NAME_RE = re.compile(r"^name:\s*(\S+)\s*$", re.MULTILINE)


def extract_run_commands(text: str) -> List[str]:
    """Return the distinct ``scripts/...`` paths the text instructs running."""
    return sorted({m.group(1) for m in _RUN_RE.finditer(text or "")})


def skill_ref_to_name(skill_ref: str) -> str:
    """A cron `skills:` entry ``evolution/research`` matches the SKILL.md
    frontmatter name ``evolution-research``."""
    return skill_ref.replace("/", "-")


# ── Bounded skill edits (#907 — SkillOpt) ──────────────────────────────────────
# "Edit budget": cap the FRACTION of a skill's pre-change lines a single cycle
# may touch (added + removed), not an absolute line count — a 20-line and a
# 2000-line skill are held to the same proportional standard. Below
# MIN_SKILL_LINES_FOR_BUDGET the ratio is not meaningful (a brand-new skill, or
# one tiny enough that its first real edit always looks like ~100% churn), so
# such files are exempt — this is a "no wholesale rewrite" check, not a
# "no big skill" check. Overridable via EVOLUTION_SKILL_EDIT_BUDGET_RATIO for
# the same reason DEFAULT_MAX_LINES in evolution_merge_gate.py is
# env-overridable via EVOLUTION_MERGE_MAX_LINES.
DEFAULT_EDIT_BUDGET_RATIO = 0.5
MIN_SKILL_LINES_FOR_BUDGET = 20


def check_skill_edit_budget(
    path: str,
    before_lines: int,
    added: int,
    removed: int,
    ratio: float = DEFAULT_EDIT_BUDGET_RATIO,
    min_lines: int = MIN_SKILL_LINES_FOR_BUDGET,
) -> Optional[Dict[str, str]]:
    """Pure check for one changed ``SKILL.md``. ``before_lines`` is the file's
    line count at the base ref; ``added``/``removed`` are the diff's numstat
    counts. A brand-new file has ``before_lines == 0`` and is exempt (creation
    is not a "rewrite"); so is any existing skill under ``min_lines`` (the
    ratio isn't meaningful yet). Returns a violation dict, or ``None`` if the
    edit is within budget."""
    if before_lines < min_lines:
        return None
    frac = (added + removed) / before_lines
    if frac <= ratio:
        return None
    return {
        "path": path,
        "kind": "edit_budget_exceeded",
        "detail": (
            f"{added + removed} changed lines ({frac:.0%}) of a {before_lines}-line "
            f"skill exceed the {ratio:.0%} per-cycle edit budget — split into "
            f"smaller incremental edits instead of a wholesale rewrite"
        ),
    }


def find_edit_budget_violations(
    diffs: List[Dict[str, Any]],
    ratio: float = DEFAULT_EDIT_BUDGET_RATIO,
    min_lines: int = MIN_SKILL_LINES_FOR_BUDGET,
) -> List[Dict[str, str]]:
    """Pure core, mirrors ``find_violations``. ``diffs`` = [{path, before_lines,
    added, removed}, ...] for changed ``skills/**/SKILL.md`` files. No git IO."""
    out: List[Dict[str, str]] = []
    for d in diffs:
        v = check_skill_edit_budget(
            path=str(d.get("path") or ""),
            before_lines=int(d.get("before_lines") or 0),
            added=int(d.get("added") or 0),
            removed=int(d.get("removed") or 0),
            ratio=ratio,
            min_lines=min_lines,
        )
        if v:
            out.append(v)
    return out


def find_violations(
    stages: List[Dict[str, Any]],
    skill_texts: Dict[str, str],
    existing_scripts: set,
) -> List[Dict[str, str]]:
    """Pure core. ``stages`` = [{name, skills:[ref], toolsets:[..]}]; ``skill_texts``
    maps skill frontmatter-name -> SKILL.md text; ``existing_scripts`` = set of
    ``scripts/X`` paths that exist. Returns a list of violation dicts."""
    out: List[Dict[str, str]] = []
    for stage in stages:
        toolsets = set(stage.get("toolsets") or [])
        has_terminal = "terminal" in toolsets
        for ref in stage.get("skills") or []:
            name = skill_ref_to_name(str(ref))
            text = skill_texts.get(name)
            if text is None:
                out.append(
                    {
                        "stage": stage["name"],
                        "skill": name,
                        "kind": "missing_skill",
                        "detail": f"stage references skill '{ref}' with no SKILL.md",
                    }
                )
                continue
            for script in extract_run_commands(text):
                if script not in existing_scripts:
                    out.append(
                        {
                            "stage": stage["name"],
                            "skill": name,
                            "kind": "missing_script",
                            "detail": f"runs `{script}` which does not exist",
                        }
                    )
                elif not has_terminal:
                    out.append(
                        {
                            "stage": stage["name"],
                            "skill": name,
                            "kind": "no_terminal",
                            "detail": (
                                f"runs `{script}` but stage toolsets {sorted(toolsets)} "
                                f"lack 'terminal' — the command can never execute"
                            ),
                        }
                    )
    return out


# ── IO boundary ────────────────────────────────────────────────────────────────
def _load_stages(cron_dir: Path) -> List[Dict[str, Any]]:
    import yaml

    stages: List[Dict[str, Any]] = []
    for y in sorted(cron_dir.glob("*.yaml")):
        try:
            d = yaml.safe_load(y.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if d.get("no_agent"):  # script-only stages own no skills
            continue
        stages.append(
            {
                "name": y.stem,
                "skills": d.get("skills") or [],
                "toolsets": d.get("toolsets") or [],
            }
        )
    return stages


def _load_skill_texts(skills_dir: Path) -> Dict[str, str]:
    texts: Dict[str, str] = {}
    for md in skills_dir.glob("**/SKILL.md"):
        text = md.read_text(encoding="utf-8")
        m = _NAME_RE.search(text)
        name = m.group(1) if m else md.parent.name
        texts[name] = text
    return texts


def lint_repo(repo_root: Optional[Path] = None) -> List[Dict[str, str]]:
    """Lint the real repo: evolution cron stages × their skills × scripts/."""
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[1]
    stages = _load_stages(root / "cron" / "evolution")
    skill_texts = _load_skill_texts(root / "skills" / "evolution")
    existing = {
        f"scripts/{p.name}" for p in (root / "scripts").glob("*") if p.is_file()
    }
    return find_violations(stages, skill_texts, existing)


def _git_show_line_count(repo_root: Path, ref: str, path: str) -> int:
    """Line count of ``path`` at ``ref``, or 0 if it doesn't exist there (a
    brand-new file) or git fails for any reason. Never raises."""
    try:
        proc = subprocess.run(
            ["git", "show", f"{ref}:{path}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return 0
    if proc.returncode != 0:
        return 0
    return len(proc.stdout.splitlines())


def _git_merge_base(repo_root: Path, base_ref: str) -> Optional[str]:
    """``git merge-base HEAD base_ref``, or ``None`` if it can't be resolved."""
    try:
        proc = subprocess.run(
            ["git", "merge-base", "HEAD", base_ref],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _git_skill_md_diffs(repo_root: Path, base_ref: str) -> List[Dict[str, Any]]:
    """Real git IO: numstat for changed ``skills/**/SKILL.md`` files vs the
    merge-base of ``HEAD`` and ``base_ref``, plus each file's pre-change line
    count. Diffed against the WORKING TREE (not just HEAD) so this can run
    pre-commit — same convention as the evolution-implementation skill's
    Step 3 landability gate (``git diff --shortstat "$(git merge-base HEAD
    origin/main)"``). Best-effort — any git failure yields an empty list (a
    broken/shallow checkout must never crash the gate, only skip it)."""
    merge_base = _git_merge_base(repo_root, base_ref) or base_ref
    try:
        proc = subprocess.run(
            ["git", "diff", "--numstat", merge_base, "--", "skills/**/SKILL.md"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    if proc.returncode != 0:
        return []
    out: List[Dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added_s, removed_s, path = parts
        if added_s == "-" or removed_s == "-":
            continue  # binary — not applicable to a SKILL.md, skip defensively
        try:
            added, removed = int(added_s), int(removed_s)
        except ValueError:
            continue
        out.append(
            {
                "path": path,
                "added": added,
                "removed": removed,
                "before_lines": _git_show_line_count(repo_root, merge_base, path),
            }
        )
    return out


def lint_skill_edit_budget(
    repo_root: Optional[Path] = None, base_ref: str = "origin/main"
) -> List[Dict[str, str]]:
    """Lint the real repo's pending diff: does any changed ``SKILL.md`` blow its
    per-cycle edit budget vs ``base_ref``? Meant to run pre-PR in the
    evolution-implementation stage (see skills/evolution/evolution-implementation),
    mirroring how ``evolution_merge_gate.py``'s diff cap is checked locally
    before committing."""
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[1]
    try:
        ratio = float(os.environ.get("EVOLUTION_SKILL_EDIT_BUDGET_RATIO", DEFAULT_EDIT_BUDGET_RATIO))
    except ValueError:
        ratio = DEFAULT_EDIT_BUDGET_RATIO
    diffs = _git_skill_md_diffs(root, base_ref)
    return find_edit_budget_violations(diffs, ratio=ratio)


def main(argv: List[str]) -> int:
    args = argv[1:]
    if "--skill-edit-budget" in args:
        base_ref = args[args.index("--base") + 1] if "--base" in args else "origin/main"
        violations = lint_skill_edit_budget(base_ref=base_ref)
        if not violations:
            print("[evolution-skill-lint] OK — no skill exceeds its per-cycle edit budget")
            return 0
        print(f"[evolution-skill-lint] {len(violations)} edit-budget violation(s):", file=sys.stderr)
        for v in violations:
            print(f"  - [{v['kind']}] {v['path']}: {v['detail']}", file=sys.stderr)
        return 1

    violations = lint_repo()
    if not violations:
        print("[evolution-skill-lint] OK — no dead skill→script wiring")
        return 0
    print(f"[evolution-skill-lint] {len(violations)} violation(s):", file=sys.stderr)
    for v in violations:
        print(f"  - [{v['kind']}] {v['stage']}/{v['skill']}: {v['detail']}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
