"""Lint agent instruction/config files for prompt-injection patterns (#88).

Scans the standard agent-instruction surfaces — ``AGENTS.md``, ``CLAUDE.md``,
``.cursorrules``, ``SOUL.md``/``.hermes.md``/``HERMES.md``, skills
(``**/SKILL.md`` under any ``skills`` directory) and hooks (any file under a
``hooks`` directory) — and reports prompt-injection / promptware /
exfiltration patterns.

The scanner is :mod:`tools.threat_patterns` — the SAME single source of truth
that guards context-file injection at system-prompt assembly time
(``agent/prompt_builder.py``) and memory writes (``tools/memory_tool.py``).
This module only chooses the *targets* and the *reporting* format; it never
blocks, never mutates files, and never loads anything into a prompt.

Line attribution is best-effort: threat patterns use bounded ``\\w+\\s+``
filler so a match is usually contained in one line; a pattern that only
matches across lines is reported with line 0 (file-level).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence, Set, Tuple

from tools.threat_patterns import scan_for_threats

# Instruction files conventionally placed at a project root.
INSTRUCTION_FILE_NAMES: tuple[str, ...] = (
    "AGENTS.md",
    "CLAUDE.md",
    ".cursorrules",
    "SOUL.md",
    ".hermes.md",
    "HERMES.md",
)

_SKILL_DIR_NAMES = ("skills",)
_HOOK_DIR_NAMES = ("hooks",)


@dataclass(frozen=True)
class Finding:
    """One prompt-injection pattern hit in one file."""

    path: Path
    line: int  # 0 == file-level (cross-line match, best-effort attribution)
    pattern_id: str

    def __str__(self) -> str:
        loc = f"{self.path}:{self.line}" if self.line else str(self.path)
        return f"{loc}: threat pattern {self.pattern_id!r}"


def collect_target_files(roots: Sequence[Path]) -> list[Path]:
    """Return the instruction/config files to lint under ``roots``.

    Matches: root-level instruction files; ``SKILL.md`` under any directory
    named ``skills``; any file under a directory named ``hooks``.  Results
    are de-duplicated across roots and sorted for deterministic output.
    """
    seen: set[str] = set()
    files: list[Path] = []
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for name in INSTRUCTION_FILE_NAMES:
            candidate = root / name
            if candidate.is_file():
                key = str(candidate.resolve())
                if key not in seen:
                    seen.add(key)
                    files.append(candidate)
        for candidate in sorted(root.rglob("SKILL.md")):
            if any(part.lower() in _SKILL_DIR_NAMES for part in candidate.parts):
                key = str(candidate.resolve())
                if key not in seen:
                    seen.add(key)
                    files.append(candidate)
        for candidate in sorted(root.rglob("*")):
            if candidate.is_file() and any(
                part.lower() in _HOOK_DIR_NAMES for part in candidate.parts
            ):
                key = str(candidate.resolve())
                if key not in seen:
                    seen.add(key)
                    files.append(candidate)
    return files


def lint_paths(roots: Sequence[Path], *, scope: str = "context") -> list[Finding]:
    """Lint instruction/config files under ``roots``; return findings.

    ``scope`` maps directly to :func:`tools.threat_patterns.scan_for_threats`:
    - ``"context"`` (default) — classic injection + promptware/C2 + role-play
      hijack; the same scope the context-file assembly scanner uses.
    - ``"strict"`` — adds exfil-URL / persistence / hardcoded-secret patterns
      (noisier; for user-mediated content).
    - ``"all"`` — narrow classic-injection set (minimal false positives).

    Findings are sorted by path, line, pattern id.  Never raises on
    unreadable files (skipped).
    """
    findings: list[Finding] = []
    for path in collect_target_files(roots):
        try:
            content = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        findings.extend(_lint_content(content, path, scope=scope))
    return sorted(findings, key=lambda f: (str(f.path), f.line, f.pattern_id))


def _lint_content(content: str, path: Path, *, scope: str) -> list[Finding]:
    """Scan one file's content; attribute hits to lines where possible."""
    hits = set(scan_for_threats(content, scope=scope))
    if not hits:
        return []
    by_line: dict[str, int] = {}
    for lineno, line in enumerate(content.splitlines(), start=1):
        for pid in scan_for_threats(line, scope=scope):
            if pid not in by_line:
                by_line[pid] = lineno
    return [
        Finding(path=path, line=by_line.get(pid, 0), pattern_id=pid)
        for pid in sorted(hits)
    ]


def default_lint_roots() -> list[Path]:
    """Default scan roots: enclosing git root (or cwd) plus HERMES_HOME."""
    roots: list[Path] = []
    try:
        from agent.skill_utils import find_project_root

        project_root = find_project_root()
    except Exception:
        project_root = None
    roots.append(project_root if project_root is not None else Path.cwd())
    try:
        from hermes_constants import get_hermes_home

        roots.append(Path(get_hermes_home()))
    except Exception:
        pass
    return roots


def cmd_config_lint(args) -> int:
    """CLI handler for ``hermes security lint``.

    Lints the instruction/config files under the configured roots and prints
    one finding per line (``path:line: threat pattern 'id'``). Audit-only: it
    never modifies or deletes a file. Exit code is 0 when clean and 1 when
    findings are present (so it can gate CI/upgrade), unless ``--no-fail`` is
    given.
    """
    if getattr(args, "roots", None):
        roots = [Path(r) for r in args.roots]
    else:
        roots = default_lint_roots()

    findings = lint_paths(roots, scope=getattr(args, "scope", "context"))
    if not findings:
        print("No prompt-injection findings in instruction/config files.")
        return 0

    for finding in findings:
        print(str(finding))
    print(
        f"{len(findings)} prompt-injection finding(s) across "
        f"{len(collect_target_files(roots))} scanned file(s).",
        file=sys.stderr,
    )
    return 0 if getattr(args, "no_fail", False) else 1


__all__ = [
    "INSTRUCTION_FILE_NAMES",
    "Finding",
    "cmd_config_lint",
    "collect_target_files",
    "default_lint_roots",
    "lint_paths",
]
