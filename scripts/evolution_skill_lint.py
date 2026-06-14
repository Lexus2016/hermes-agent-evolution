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
"""

from __future__ import annotations

import re
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


def main(argv: List[str]) -> int:
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
