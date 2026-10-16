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

Part 3 — checked lowering for declared tool inputs (#77, SkillEffect, S2)
-------------------------------------------------------------------------
``scripts/evolution_tool_cap_check.py`` (S1) is the pure per-tool input-cap
checker; THIS is the real call site that wires it into the skill gate, per the
#77 implementation brief ("gate evolution-generated skills behind S1 at the
skill gate before they are trusted for reuse"). A skill may DECLARE the tool
invocations it intends to make in a ``declared_inputs:`` frontmatter block
(see :func:`extract_declared_inputs`). The deterministic lint then runs each
declaration through the S1 checker and rejects skills whose declared inputs
exceed the per-tool caps — closing the issue's acceptance criterion
("adversarial oversized-input proposals rejected before execution") at the
skill gate, without touching the core tool path (that is S3, bounded
execution, which needs its own design review).

The S1 import is deliberately LAZY (:func:`_cap_checker`): this module is also
loaded by ``scripts/register_evolution_cron.py`` through an importlib file-spec
with only ``repo_root`` on sys.path, where a top-level
``from evolution_tool_cap_check import ...`` would raise ModuleNotFoundError
and silently disable the whole skill→toolset pre-flight (the #77 rework
review).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

# Standalone source invocations must resolve the canonical repository YAML policy.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from typing import Any, Dict, List, Optional

# A script invocation the stage is told to RUN — `python/python3/bash/sh scripts/X`
# or `./scripts/X`. Bare inline mentions (no runner, no `./`) are NOT commands.
_RUN_RE = re.compile(r"(?:(?:python3?|bash|sh)\s+|\./)(scripts/[A-Za-z0-9_./-]+\.(?:py|sh))")

# Frontmatter `name:` line in a SKILL.md.
_NAME_RE = re.compile(r"^name:\s*(\S+)\s*$", re.MULTILINE)


def extract_run_commands(text: str) -> list[str]:
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
) -> Optional[dict[str, str]]:
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
    diffs: list[dict[str, Any]],
    ratio: float = DEFAULT_EDIT_BUDGET_RATIO,
    min_lines: int = MIN_SKILL_LINES_FOR_BUDGET,
) -> list[dict[str, str]]:
    """Pure core, mirrors ``find_violations``. ``diffs`` = [{path, before_lines,
    added, removed}, ...] for changed ``skills/**/SKILL.md`` files. No git IO."""
    out: list[dict[str, str]] = []
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
    stages: list[dict[str, Any]],
    skill_texts: dict[str, str],
    existing_scripts: set,
) -> list[dict[str, str]]:
    """Pure core. ``stages`` = [{name, skills:[ref], toolsets:[..]}]; ``skill_texts``
    maps skill frontmatter-name -> SKILL.md text; ``existing_scripts`` = set of
    ``scripts/X`` paths that exist. Returns a list of violation dicts."""
    out: list[dict[str, str]] = []
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


# ── Checked lowering for declared tool inputs (#77 — S2 call site) ──────────
def _cap_checker():
    """Lazily resolve the S1 cap checker (checked lowering, #77 S2).

    scripts/register_evolution_cron.py loads this module through an
    importlib file-spec with only ``repo_root`` on sys.path — a top-level
    ``from evolution_tool_cap_check import ...`` raised ModuleNotFoundError
    there and silently disabled the whole skill→toolset pre-flight (the #77
    rework review). Importing inside the function keeps module load safe in
    every loader context; if the sibling module cannot be resolved, the cap
    gate degrades to a no-op (other lint rules still run). Returns
    ``(check_input_caps, load_caps)`` or ``(None, None)``.
    """
    try:
        from evolution_tool_cap_check import check_input_caps, load_caps

        return check_input_caps, load_caps
    except ImportError:
        pass
    try:
        import importlib.util

        here = Path(__file__).resolve().parent
        spec = importlib.util.spec_from_file_location(
            "evolution_tool_cap_check", here / "evolution_tool_cap_check.py"
        )
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod.check_input_caps, mod.load_caps
    except Exception:
        pass
    return None, None


def extract_declared_inputs(skill_text: str) -> list[dict[str, Any]]:
    """Parse the ``declared_inputs:`` frontmatter block of a SKILL.md.

    Declarations are OPT-IN: a skill with no block yields ``[]`` and is not
    flagged, so the existing corpus has zero false positives. Accepted shapes:

    .. code-block:: yaml

       declared_inputs:
         - tool: read_file
           args: {path: "data/big.txt"}
         - {tool: search_files, args: {pattern: "*.py", limit: 50}}
         - read_file: {path: "data/big.txt"}      # single-tool map form

    Returns a normalized list of ``{"tool": str, "args": dict}``. Malformed
    blocks degrade to ``[]`` (never raise) — a broken declaration must not
    crash the gate, and an absent one is simply no declaration.
    """
    try:
        import hermes_yaml as yaml
    except Exception:
        return []
    text = skill_text or ""
    if "---" not in text:
        return []
    # Frontmatter = the YAML between the FIRST two ``---`` lines.
    parts = text.split("---", 2)
    if len(parts) < 3:
        return []
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except Exception:
        return []
    raw = fm.get("declared_inputs")
    out: list[dict[str, Any]] = []
    if raw is None:
        return out
    if isinstance(raw, dict):
        # Map form: {tool: {arg: val}} or {tool: {"args": {...}}}.
        for tool, spec in raw.items():
            if not isinstance(tool, str):
                continue
            if isinstance(spec, dict) and "args" in spec and isinstance(spec["args"], dict):
                out.append({"tool": tool, "args": dict(spec["args"])})
            elif isinstance(spec, dict):
                out.append({"tool": tool, "args": dict(spec)})
        return out
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            tool = item.get("tool")
            if not isinstance(tool, str) or not tool:
                continue
            args = item.get("args")
            out.append({"tool": tool, "args": dict(args) if isinstance(args, dict) else {}})
        return out
    return out


def find_cap_violations(
    declared_inputs: list[dict[str, Any]],
    caps: Optional[dict[str, dict[str, int]]] = None,
) -> list[dict[str, str]]:
    """Run declared tool inputs through the S1 cap checker.

    Pure core, mirrors ``find_violations``. Each returned dict gains the
    ``kind`` prefix ``cap_`` so violations from the two gates are
    distinguishable in output and tests. When the S1 module is unreachable
    (lazy import no-op) this returns ``[]`` — the cap gate degrades, other
    rules still run.
    """
    checker, loader = _cap_checker()
    if checker is None:
        return []
    if caps is None and loader is not None:
        caps = loader()
    out: list[dict[str, str]] = []
    for decl in declared_inputs or []:
        tool = decl.get("tool") or ""
        args = decl.get("args")
        if not tool:
            continue
        for v in checker(tool, args, caps=caps):
            out.append(
                {
                    "kind": f"cap_{v['kind']}",
                    "tool": v["tool"],
                    "detail": v["detail"],
                    "declared": v.get("declared", ""),
                    "cap": v.get("cap", ""),
                }
            )
    return out


# ── IO boundary ────────────────────────────────────────────────────────────────
def _load_stages(cron_dir: Path) -> list[dict[str, Any]]:
    import hermes_yaml as yaml

    stages: list[dict[str, Any]] = []
    for y in sorted(cron_dir.glob("*.yaml")):
        try:
            d = yaml.safe_load(y.read_text(encoding="utf-8-sig")) or {}
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


def _load_skill_texts(skills_dir: Path) -> dict[str, str]:
    texts: dict[str, str] = {}
    for md in skills_dir.glob("**/SKILL.md"):
        text = md.read_text(encoding="utf-8-sig")
        m = _NAME_RE.search(text)
        name = m.group(1) if m else md.parent.name
        texts[name] = text
    return texts


def lint_repo(repo_root: Optional[Path] = None) -> list[dict[str, str]]:
    """Lint the real repo: evolution cron stages × their skills × scripts/."""
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[1]
    stages = _load_stages(root / "cron" / "evolution")
    skill_texts = _load_skill_texts(root / "skills" / "evolution")
    existing = {
        f"scripts/{p.name}" for p in (root / "scripts").glob("*") if p.is_file()
    }
    violations = find_violations(stages, skill_texts, existing)
    # #77 S2: checked lowering — a skill that DECLARES tool inputs must stay
    # within per-tool caps before it is trusted for reuse. No declaration
    # means no new violations (opt-in, zero false positives); an unreachable
    # S1 module degrades to a no-op via the lazy import, never a crash.
    for name, text in skill_texts.items():
        for v in find_cap_violations(extract_declared_inputs(text)):
            v["skill"] = name
            violations.append(v)
    return violations


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


def _git_skill_md_diffs(repo_root: Path, base_ref: str) -> list[dict[str, Any]]:
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
    out: list[dict[str, Any]] = []
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
) -> list[dict[str, str]]:
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


def main(argv: list[str]) -> int:
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
