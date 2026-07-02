"""Git-aware task scoping and change-impact prediction.

Issue #651: When a user asks Hermes to change code, the agent should understand
not just the current file tree but what code has RECENTLY changed and what is
currently in flux.  This module adds a lightweight, read-only git probe that runs
before a coding task begins and produces a small structured report the agent can
consume as extra context.

The module is intentionally separate from the stable session-start workspace
snapshot in :mod:`agent.coding_context`.  That snapshot is built once per session
and baked into the system prompt (cache-safe); the data here is *task-local*
because it depends on which files the agent plans to edit, and it is injected
via the ``pre_llm_call`` plugin hook so it only appears on coding turns.

All git commands run with short timeouts, never prompt for credentials, and
fail closed (return empty / None) so they cannot block a conversation.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from hermes_cli._subprocess_compat import windows_hide_flags

logger = logging.getLogger("hermes.coding_context.task_scope")

_GIT_TIMEOUT = 2.5


@dataclass
class GitTaskScope:
    """Structured git context for a single coding task."""

    repo_root: Optional[str] = None
    current_branch: Optional[str] = None
    branch_ahead_behind: Optional[str] = None
    recent_commits: List[str] = field(default_factory=list)
    branch_diff_stat: List[str] = field(default_factory=list)
    unmerged_changes: List[str] = field(default_factory=list)
    target_files_blame: Dict[str, List[str]] = field(default_factory=dict)
    estimated_impact: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _git(cwd: Path, *args: str) -> str:
    """Run a read-only git command and return stdout; empty string on failure."""
    _popen_kwargs: Dict[str, Any] = {}
    _win_flags = windows_hide_flags()
    if _win_flags:
        _popen_kwargs["creationflags"] = _win_flags
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            **_popen_kwargs,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _find_git_root(cwd: Path) -> Optional[Path]:
    current = cwd.resolve()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def _branch_status(git_root: Path) -> Dict[str, Any]:
    """Parse ``git status --porcelain=2 --branch`` for branch + ahead/behind."""
    out = _git(git_root, "status", "--porcelain=2", "--branch")
    info: Dict[str, Any] = {"head": None, "upstream": None, "ahead": 0, "behind": 0}
    for line in out.splitlines():
        if line.startswith("# branch.head"):
            info["head"] = line.split(maxsplit=2)[-1]
        elif line.startswith("# branch.upstream"):
            info["upstream"] = line.split(maxsplit=2)[-1]
        elif line.startswith("# branch.ab"):
            parts = line.split()
            info["ahead"] = int(parts[2].lstrip("+"))
            info["behind"] = int(parts[3].lstrip("-"))
    return info


def _recent_commits(git_root: Path, n: int = 20) -> List[str]:
    return _git(git_root, "log", f"-{max(1, n)}", "--pretty=%h %s").splitlines()


def _branch_diff_stat(git_root: Path) -> List[str]:
    """Files changed on the current branch relative to the merge base with upstream/main."""
    # Prefer upstream of current branch if it exists.
    branch = _branch_status(git_root)
    upstream = branch.get("upstream")
    base: Optional[str] = None
    if upstream:
        base = _git(git_root, "merge-base", "HEAD", upstream) or None
    if not base:
        # Fall back to main/master if no upstream is configured.
        for candidate in ("origin/main", "origin/master", "main", "master"):
            base = _git(git_root, "merge-base", "HEAD", candidate) or None
            if base:
                break
    if not base:
        return []
    stat = _git(git_root, "diff", "--stat", f"{base}...HEAD")
    return [line for line in stat.splitlines() if line.strip()]


def _unmerged_changes(git_root: Path) -> List[str]:
    """Files currently changed in the working tree relative to HEAD."""
    stat = _git(git_root, "diff", "--stat", "HEAD")
    return [line for line in stat.splitlines() if line.strip()]


def _blame_summary(git_root: Path, paths: Iterable[str], n: int = 3) -> Dict[str, List[str]]:
    """Last n unique authors touching each target file (name + date)."""
    result: Dict[str, List[str]] = {}
    for p in paths:
        rel = Path(p)
        if rel.is_absolute() and not str(rel).startswith(str(git_root)):
            continue
        rel = rel.relative_to(git_root) if str(rel).startswith(str(git_root)) else rel
        blame = _git(
            git_root,
            "log",
            f"-{max(1, n)}",
            "--pretty=%an (%ad)",
            "--date=short",
            "--",
            str(rel),
        )
        seen: List[str] = []
        for line in blame.splitlines():
            if line and line not in seen:
                seen.append(line)
        if seen:
            result[str(rel)] = seen[:n]
    return result


def _downstream_files(git_root: Path, paths: Iterable[str]) -> Set[str]:
    """Heuristic downstream consumers: files that import or reference target files."""
    consumers: Set[str] = set()
    targets = {Path(p).stem for p in paths if Path(p).suffix in {".py", ".ts", ".js", ".tsx", ".jsx"}}
    if not targets:
        return consumers
    try:
        # Fast path: grep for import/use statements referencing the target module names.
        grep = _git(
            git_root,
            "grep",
            "-lE",
            "(" + "|".join(re.escape(t) for t in targets) + ")",
            "--",
            "*.py",
            "*.ts",
            "*.tsx",
            "*.js",
            "*.jsx",
        )
        for line in grep.splitlines():
            line = line.strip()
            if line:
                consumers.add(line)
    except Exception:
        pass
    # Drop the target files themselves.
    return consumers - {str(Path(p).relative_to(git_root)) for p in paths if str(Path(p)).startswith(str(git_root))}


def build_git_task_scope(
    cwd: Optional[str | Path] = None,
    target_paths: Optional[Iterable[str]] = None,
    max_commits: int = 20,
) -> Optional[GitTaskScope]:
    """Build a git-aware task scope for a coding task.

    Args:
        cwd: Working directory.  If not inside a git repo, returns None.
        target_paths: Files/directories the agent intends to edit.  Used for
            blame + downstream-consumer estimation.  May be empty/None.
        max_commits: Number of recent commits to include.

    Returns:
        GitTaskScope or None if not in a git repo or git commands fail.
    """
    cwd_path = Path(cwd or Path.cwd()).expanduser().resolve()
    git_root = _find_git_root(cwd_path)
    if not git_root:
        return None

    try:
        branch = _branch_status(git_root)
        recent = _recent_commits(git_root, n=max_commits)
        branch_diff = _branch_diff_stat(git_root)
        unmerged = _unmerged_changes(git_root)

        targets = [str(Path(p).resolve().relative_to(git_root)) if Path(p).is_absolute() else str(p) for p in (target_paths or [])]
        blame: Dict[str, List[str]] = {}
        if targets:
            blame = _blame_summary(git_root, targets)

        consumers = _downstream_files(git_root, targets)

        ahead_behind = None
        if branch.get("ahead") or branch.get("behind"):
            ahead_behind = f"+{branch.get('ahead', 0)}/-{branch.get('behind', 0)}"

        impact: Dict[str, Any] = {
            "target_files": targets,
            "downstream_consumer_count": len(consumers),
            "downstream_consumers": sorted(consumers)[:20],
        }

        return GitTaskScope(
            repo_root=str(git_root),
            current_branch=branch.get("head"),
            branch_ahead_behind=ahead_behind,
            recent_commits=recent,
            branch_diff_stat=branch_diff,
            unmerged_changes=unmerged,
            target_files_blame=blame,
            estimated_impact=impact,
        )
    except Exception as exc:
        logger.debug("build_git_task_scope failed: %s", exc)
        return None


def format_task_scope(scope: Optional[GitTaskScope]) -> str:
    """Render a task scope as markdown for injection into a user message."""
    if not scope:
        return ""
    lines: List[str] = ["## Git-aware task scope"]
    if scope.current_branch:
        branch_line = f"- Branch: {scope.current_branch}"
        if scope.branch_ahead_behind:
            branch_line += f" ({scope.branch_ahead_behind} vs upstream)"
        lines.append(branch_line)
    if scope.recent_commits:
        lines.append("- Recent commits:")
        lines.extend(f"  - {c}" for c in scope.recent_commits[:10])
    if scope.branch_diff_stat:
        lines.append("- Changes already on this branch (vs merge base):")
        lines.extend(f"  - {line}" for line in scope.branch_diff_stat[:15])
    if scope.unmerged_changes:
        lines.append("- Uncommitted changes:")
        lines.extend(f"  - {line}" for line in scope.unmerged_changes[:15])
    if scope.target_files_blame:
        lines.append("- Last authors on target files:")
        for path, authors in scope.target_files_blame.items():
            lines.append(f"  - {path}: {', '.join(authors)}")
    impact = scope.estimated_impact
    if impact.get("downstream_consumers"):
        lines.append("- Estimated downstream consumers:")
        lines.extend(f"  - {c}" for c in impact["downstream_consumers"])
    if len(lines) == 1:
        return ""
    return "\n".join(lines)
