#!/usr/bin/env python3
"""File Tools Module - LLM agent file manipulation tools."""

import base64
import errno
import json
import logging
import os
import posixpath
import re
import sys
import threading
import unicodedata
from pathlib import Path, PurePosixPath

from agent.file_safety import get_read_block_error
from tools.binary_extensions import (
    has_binary_extension,
    has_opaque_document_extension,
    is_pdf_path,
)
from tools.file_operations import (
    ShellFileOperations,
    normalize_read_pagination,
    normalize_search_pagination,
)
from tools import file_state
from tools.path_validation import format_nearby_hint, suggest_nearby_paths
from agent.redact import redact_sensitive_text

logger = logging.getLogger(__name__)


_EXPECTED_WRITE_ERRNOS = {errno.EACCES, errno.EPERM, errno.EROFS}



# Invisible / compatibility spaces that render like a normal space in a
# terminal. Folding them (plus NFC) lets read_file recover a path the
# model retyped visually-correctly but with the wrong bytes.
_FILENAME_SPACE_FOLDS = (
    "\u00a0",  # no-break space
    "\u202f",  # narrow no-break space
    "\u2007",  # figure space
    "\u2009",  # thin space
    "\u200a",  # hair space
)


def _fold_filename_for_unicode_match(name: str) -> str:
    folded = unicodedata.normalize("NFC", name)
    for src in _FILENAME_SPACE_FOLDS:
        folded = folded.replace(src, " ")
    return folded


def _find_unicode_equivalent_path(requested: Path) -> Path | None:
    """Return the single same-dir file that is a unicode-equivalent of *requested*.

    Conservative: only NFC + invisible-space folding, and only when exactly
    one sibling matches. Visible differences (straight vs curly quote,
    missing accents) stay as not-found + similar_files.
    """
    try:
        parent = requested.parent
        if not parent.is_dir():
            return None
        target = _fold_filename_for_unicode_match(requested.name)
        matches = [
            entry
            for entry in parent.iterdir()
            if entry.is_file()
            and _fold_filename_for_unicode_match(entry.name) == target
        ]
    except OSError:
        return None
    if len(matches) == 1 and matches[0].name != requested.name:
        return matches[0]
    return None


def _find_auto_repaired_path(
    requested: Path,
    raw_path: str,
    task_id: str = "default",
) -> tuple[Path | None, str | None]:
    """Find a single unambiguous valid path candidate when *requested* does not exist (#2411).

    Strategies evaluated in priority order:
      1. Unicode normalization (NFC + invisible-space folding via _find_unicode_equivalent_path)
      2. Case-insensitive match in the requested parent directory (e.g. readme.md -> README.md)
      3. Case-insensitive component-wise path traversal (e.g. Tools/file_tools.py -> tools/file_tools.py)
      4. Workspace-root fallback (if relative path failed against cwd)
      5. Extraneous prefix stripping (e.g. hermes-agent/tools/foo.py -> tools/foo.py)
      6. Unique filename in workspace tree (for non-generic filenames >3 chars)

    Returns (repaired_path, explanation_hint) or (None, None) if ambiguous or none found.
    """
    # 1. Unicode normalization
    unicode_hit = _find_unicode_equivalent_path(requested)
    if unicode_hit is not None and unicode_hit.is_file():
        return (
            unicode_hit,
            f"Opened unicode-equivalent filename {unicode_hit.name!r} instead of {requested.name!r}.",
        )

    # 2. Case-insensitive match in parent directory
    try:
        parent = requested.parent
        if parent.is_dir():
            target_lower = requested.name.lower()
            ci_matches = [
                entry
                for entry in parent.iterdir()
                if entry.is_file() and entry.name.lower() == target_lower
            ]
            if len(ci_matches) == 1 and ci_matches[0].name != requested.name:
                return (
                    ci_matches[0],
                    f"Opened case-corrected filename {ci_matches[0].name!r} instead of {requested.name!r}.",
                )
    except OSError:
        pass

    # 3. Case-insensitive component-wise path traversal
    try:
        curr_dir = requested.parent
        unresolved_parts = [requested.name]
        while not curr_dir.exists() and curr_dir.parent != curr_dir:
            unresolved_parts.insert(0, curr_dir.name)
            curr_dir = curr_dir.parent
        if (
            curr_dir.exists()
            and curr_dir.is_dir()
            and unresolved_parts != [requested.name]
        ):
            matched_all = True
            for part in unresolved_parts:
                if not curr_dir.is_dir():
                    matched_all = False
                    break
                part_lower = part.lower()
                matches = [
                    e for e in curr_dir.iterdir() if e.name.lower() == part_lower
                ]
                if len(matches) == 1:
                    curr_dir = matches[0]
                else:
                    matched_all = False
                    break
            if matched_all and curr_dir.is_file() and curr_dir != requested:
                return (
                    curr_dir,
                    f"Opened case-corrected path '{curr_dir}' instead of '{raw_path}'.",
                )
    except OSError:
        pass

    # 4. Workspace / Project root vs CWD fallback
    ws_root = None
    base_dir = None
    try:
        ws_root = _authoritative_workspace_root(task_id)
        base_dir = str(_resolve_base_dir(task_id, container_paths=False))
        if not Path(raw_path).is_absolute():
            # Check explicit workspace root
            if ws_root and ws_root != base_dir:
                ws_candidate = (Path(ws_root) / raw_path).resolve()
                if ws_candidate.is_file() and ws_candidate != requested:
                    return (
                        ws_candidate,
                        f"Resolved path relative to workspace root '{ws_root}' instead of working directory.",
                    )
            # Check project root by walking up to find .git, pyproject.toml, package.json, config.yaml
            start_dir = Path(base_dir if base_dir else os.getcwd())
            proj_root = start_dir
            while proj_root.parent != proj_root:
                if (
                    (proj_root / ".git").exists()
                    or (proj_root / "pyproject.toml").exists()
                    or (proj_root / "package.json").exists()
                    or (proj_root / "config.yaml").exists()
                ):
                    break
                proj_root = proj_root.parent
            if proj_root != start_dir:
                proj_cand = (proj_root / raw_path).resolve()
                if proj_cand.is_file() and proj_cand != requested:
                    return (
                        proj_cand,
                        f"Resolved path relative to project root '{proj_root}' instead of working directory.",
                    )
            if base_dir:
                cwd_candidate = (Path(base_dir) / raw_path).resolve()
                if cwd_candidate.is_file() and cwd_candidate != requested:
                    return (
                        cwd_candidate,
                        f"Resolved path relative to working directory '{base_dir}'.",
                    )
    except Exception:
        pass

    # 5. Extraneous prefix stripping (e.g. repo name or /workspace/ or workspace/)
    try:
        raw_parts = Path(raw_path.lstrip("/\\")).parts
        if len(raw_parts) > 1:
            base_p = Path(base_dir if base_dir else os.getcwd())
            ws_p = Path(ws_root) if ws_root else base_p
            # Try stripping 1 leading component
            stripped_1 = Path(*raw_parts[1:])
            for anchor in (base_p, ws_p):
                cand = (anchor / stripped_1).resolve()
                if cand.is_file() and cand != requested:
                    return (
                        cand,
                        f"Stripped leading directory prefix from '{raw_path}' to '{cand}'.",
                    )
            # If 2+ components and starts with common container/repo names, try stripping 2
            if len(raw_parts) > 2 and raw_parts[0].lower() in {
                "workspace",
                "config",
                "app",
            }:
                stripped_2 = Path(*raw_parts[2:])
                for anchor in (base_p, ws_p):
                    cand = (anchor / stripped_2).resolve()
                    if cand.is_file() and cand != requested:
                        return (
                            cand,
                            f"Stripped leading directory prefix from '{raw_path}' to '{cand}'.",
                        )
    except Exception:
        pass

    # 6. Unique matching file in workspace tree for non-generic filenames
    _GENERIC_NAMES = frozenset({
        "__init__.py",
        "index.js",
        "index.ts",
        "index.html",
        "setup.py",
        "pyproject.toml",
        "package.json",
        "cargo.toml",
        "main.py",
        "app.py",
        "readme.md",
        "license",
        "config.yaml",
        "config.yml",
        "config.json",
        "conftest.py",
        "makefile",
        "dockerfile",
    })
    filename = requested.name
    if filename.lower() not in _GENERIC_NAMES and len(filename) > 3:
        try:
            ws_root_path = Path(
                ws_root if ws_root else (base_dir if base_dir else os.getcwd())
            )
            if ws_root_path.is_dir():
                matches = []
                target_name_lower = filename.lower()
                for root_dir, dirs, files in os.walk(ws_root_path):
                    dirs[:] = [
                        d
                        for d in dirs
                        if d
                        not in {
                            ".git",
                            ".venv",
                            "venv",
                            "node_modules",
                            "__pycache__",
                            ".pytest_cache",
                            ".claude",
                        }
                    ]
                    for f in files:
                        if f.lower() == target_name_lower:
                            matches.append(Path(root_dir) / f)
                            if len(matches) > 1:
                                break
                    if len(matches) > 1:
                        break
                if (
                    len(matches) == 1
                    and matches[0].is_file()
                    and matches[0] != requested
                ):
                    return (
                        matches[0],
                        f"Found unique matching file '{matches[0]}' in workspace for '{raw_path}'.",
                    )
        except Exception:
            pass

    return None, None


def _expand_tilde(path: str) -> str:
    """Expand ``~`` using the effective profile home when available.

    In-process file tools share the gateway process's HOME, which may differ
    from the profile-specific HOME that interactive CLI sessions use.  This
    mirrors ``hermes_constants.get_subprocess_home()`` so that ``~`` resolves
    consistently regardless of whether the tool runs interactively or inside a
    gateway-driven cron job (#48552).
    """
    if not path or "~" not in path:
        return path
    try:
        from hermes_constants import get_subprocess_home

        home = get_subprocess_home()
    except Exception:
        home = None
    if home and (path == "~" or path.startswith("~/")):
        return home if path == "~" else os.path.join(home, path[2:])
    return os.path.expanduser(path)


# ---------------------------------------------------------------------------
# Read-size guard: cap the character count returned to the model.
# We're model-agnostic so we can't count tokens; characters are a safe proxy.
# 100K chars ≈ 25–35K tokens across typical tokenisers.  Files larger than
# this in a single read are a context-window hazard — the model should use
# offset+limit to read the relevant section.
#
# Configurable via config.yaml:  file_read_max_chars: 200000
# ---------------------------------------------------------------------------
_DEFAULT_MAX_READ_CHARS = 100_000
_max_read_chars_cached: int | None = None


def _get_max_read_chars() -> int:
    """Return the configured max characters per file read.

    Reads ``file_read_max_chars`` from config.yaml on first call, caches
    the result for the lifetime of the process.  Falls back to the
    built-in default if the config is missing or invalid.
    """
    global _max_read_chars_cached
    if _max_read_chars_cached is not None:
        return _max_read_chars_cached
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        val = cfg.get("file_read_max_chars")
        if isinstance(val, (int, float)) and val > 0:
            _max_read_chars_cached = int(val)
            return _max_read_chars_cached
    except Exception:
        pass
    _max_read_chars_cached = _DEFAULT_MAX_READ_CHARS
    return _max_read_chars_cached


def _truncate_to_char_budget(content: str, max_chars: int) -> tuple[str, int, bool]:
    """Trim line-numbered ``read_file`` content to fit a char budget.

    Ported in spirit from nearai/ironclaw#5029 (dual line/byte cap on
    ``read_file``). Where hermes previously hard-rejected an oversized read
    (forcing the model to guess a smaller ``limit`` and burn a round-trip
    returning nothing), this trims the content to the last *complete line*
    that fits within ``max_chars`` and reports how many lines were kept so
    the caller can offer a ``next_offset`` continuation.

    ``content`` is the gutter-rendered text (``LINE_NUM|CONTENT`` joined by
    ``\\n``). Individual lines are already clamped to ``get_max_line_length()``
    upstream, so a single line never blows the whole budget on its own; the
    overflow this handles is the *accumulation* of many lines under the
    line-count limit (logs, wide CSV rows, minified data).

    Returns ``(kept_text, lines_kept, truncated)``. When ``content`` already
    fits, returns it unchanged with ``truncated=False``. If not even the
    first line fits, that single line is clamped on a code-point boundary
    (Python ``str`` slicing never splits a code point) so the read never
    returns empty and the cursor can still advance.
    """
    if len(content) <= max_chars:
        return content, (content.count("\n") + 1 if content else 0), False

    lines = content.split("\n")
    kept: list[str] = []
    running = 0
    for line in lines:
        # +1 for the "\n" that rejoins this line to the previous one.
        addition = len(line) + (1 if kept else 0)
        if running + addition > max_chars:
            break
        kept.append(line)
        running += addition

    if not kept:
        # First line alone exceeds the budget. Clamp on a code-point
        # boundary rather than emitting nothing.
        kept.append(lines[0][:max_chars])

    return "\n".join(kept), len(kept), True


# If the total file size exceeds this AND the caller didn't specify a narrow
# range (limit <= 200), we include a hint encouraging targeted reads.
_LARGE_FILE_HINT_BYTES = 512_000  # 512 KB

# ---------------------------------------------------------------------------
# Device path blocklist — reading these hangs the process (infinite output
# or blocking on input).  Checked by path only (no I/O).
# ---------------------------------------------------------------------------
_BLOCKED_DEVICE_PATHS = frozenset({
    # Infinite output — never reach EOF
    "/dev/zero", "/dev/random", "/dev/urandom", "/dev/full",
    # Blocks waiting for input
    "/dev/stdin", "/dev/tty", "/dev/console",
    # Nonsensical to read
    "/dev/stdout", "/dev/stderr",
    # fd aliases
    "/dev/fd/0", "/dev/fd/1", "/dev/fd/2",
})


def _resolve_path(filepath: str, task_id: str = "default") -> Path | PurePosixPath:
    """Resolve a path relative to TERMINAL_CWD (the worktree base directory)
    instead of the main repository root.
    """
    return _resolve_path_for_task(filepath, task_id)


# Sentinel ``TERMINAL_CWD`` values that mean "not configured", NOT a literal
# directory to resolve against. A stale config / .env commonly leaves the
# literal "." here; "auto"/"cwd" are setup-wizard placeholders. Treating any of
# these as a real relative base silently anchors edits to the agent PROCESS cwd
# (e.g. the main repo while a worktree session is active), routing writes to the
# wrong checkout. The gateway sanitizes the same set at import time
# (gateway/run.py); the file/terminal-tool layer must do likewise so CLI
# sessions get the same protection. See references/worktree-cwd-discipline.md.
_TERMINAL_CWD_SENTINELS = frozenset({"", ".", "./", "auto", "cwd"})
_CONTAINER_PATH_BACKENDS_FALLBACK = frozenset({"docker", "singularity", "modal", "daytona", "vercel_sandbox"})


def _terminal_env_type_for_task(task_id: str = "default") -> str:
    """Best-effort terminal backend type for path-resolution decisions."""
    try:
        from tools.terminal_tool import (
            _active_environments,
            _env_lock,
            _get_env_config,
            _resolve_container_task_id,
        )

        try:
            container_key = _resolve_container_task_id(task_id)
        except Exception:
            container_key = task_id
        with _env_lock:
            env = _active_environments.get(container_key) or _active_environments.get(task_id)
        if env is not None:
            name = env.__class__.__name__.lower()
            if "local" in name:
                return "local"
            if "ssh" in name:
                return "ssh"
            if "docker" in name:
                return "docker"
            if "singularity" in name:
                return "singularity"
            if "modal" in name:
                return "modal"
            if "daytona" in name:
                return "daytona"
            stamped = getattr(env, "_hermes_backend_name", None)
            if isinstance(stamped, str) and stamped:
                return stamped
        cfg = _get_env_config()
        return str(cfg.get("env_type") or os.getenv("TERMINAL_ENV") or "local").lower()
    except Exception:
        return str(os.getenv("TERMINAL_ENV") or "local").lower()


def _uses_container_paths(task_id: str = "default") -> bool:
    env_type = _terminal_env_type_for_task(task_id)
    try:
        from tools.terminal_tool import _is_container_backend

        return _is_container_backend(env_type)
    except Exception:
        return env_type in _CONTAINER_PATH_BACKENDS_FALLBACK


def _normalize_without_host_deref(path: str | Path | PurePosixPath) -> PurePosixPath:
    """Normalize path syntax without following host symlinks.

    Container backends use paths that are meaningful inside the sandbox. Calling
    ``Path.resolve()`` on the host can dereference a host-side symlink such as
    ``/workspace`` and rewrite the path before Docker sees it.
    """
    return PurePosixPath(posixpath.normpath(str(path)))


def _sentinel_free_abs_cwd(raw: str | None) -> str | None:
    """Normalize a cwd candidate to an absolute, sentinel-free anchor.

    Returns the expanded path only when *raw* is non-empty, not a sentinel (see
    ``_TERMINAL_CWD_SENTINELS``), and absolute. A relative anchor is meaningless
    without knowing which cwd it is relative to — exactly the ambiguity that
    misroutes worktree edits — so relative/sentinel/empty values yield ``None``.
    """
    raw = str(raw or "").strip()
    if raw.lower() in _TERMINAL_CWD_SENTINELS:
        return None
    expanded = _expand_tilde(raw)
    if not os.path.isabs(expanded):
        return None
    return expanded


def _configured_terminal_cwd() -> str | None:
    """Return ``$TERMINAL_CWD`` only when it names a real directory anchor.

    Sentinel values (see ``_TERMINAL_CWD_SENTINELS``) and relative paths are
    rejected — a relative anchor is meaningless without knowing which cwd it is
    relative to, which is exactly the ambiguity that misroutes worktree edits.
    Only an absolute, sentinel-free value is honored.
    """
    # Scope-aware: under gateway multiplexing the routed profile's cwd lives in
    # the per-turn terminal scope, not the process env (#68559).
    from agent.runtime_cwd import scope_terminal_cwd

    return _sentinel_free_abs_cwd(scope_terminal_cwd() or None)


def _registered_task_cwd_override(task_id: str = "default") -> str | None:
    """Return a registered cwd override for the raw task id, when available.

    ``terminal_tool`` intentionally collapses CWD-only task overrides to the
    shared ``"default"`` environment so TUI/dashboard/ACP sessions do not spin
    up isolated sandboxes just because they have different workspaces. The cwd
    value itself is still keyed by the raw session/task id, so file tools must
    read that raw override before falling back to the collapsed container key.
    """
    try:
        from tools.terminal_tool import resolve_task_overrides

        overrides = resolve_task_overrides(task_id)
    except Exception:
        return None

    return _sentinel_free_abs_cwd(overrides.get("cwd"))


def _authoritative_workspace_root(task_id: str = "default") -> str | None:
    """Best-effort absolute workspace root for divergence checks.

    Resolution:

      1. The session's own cwd RECORD (``terminal_tool.get_session_cwd``) —
         written on every completed terminal command and seeded by workspace
         registration, keyed by the raw session id. Because the record is
         per-session, one session's ``cd`` can never leak into another
         session's resolution.
      2. A registered task/session cwd override (TUI/Desktop/ACP sessions
         register a raw-keyed cwd before any tool runs). Normally already
         mirrored into the record at registration; kept as a direct fallback
         so a cleared/never-written record still resolves the workspace.
      3. A sentinel-free absolute ``$TERMINAL_CWD`` (the worktree path set by
         ``cli.py``/``main.py`` for ``-w`` sessions).

    Returns ``None`` only when there is genuinely no reliable anchor, in which
    case callers fall back to the process cwd.
    """
    try:
        from tools.terminal_tool import get_session_cwd

        recorded = get_session_cwd(task_id)
    except Exception:
        recorded = None
    if recorded:
        return recorded
    registered = _registered_task_cwd_override(task_id)
    if registered:
        return registered
    return _configured_terminal_cwd()


def _resolve_base_dir(
    task_id: str = "default",
    *,
    container_paths: bool | None = None,
) -> Path | PurePosixPath:
    """Return the ABSOLUTE base directory for resolving relative paths.

    Resolution order:
      1. The task's live terminal cwd (the directory the agent is actually
         working in — e.g. a git worktree). Authoritative when known.
      2. A registered task/session cwd override (TUI/Desktop/ACP sessions
         register a raw-keyed workspace cwd before any terminal command runs).
      3. A sentinel-free, absolute ``$TERMINAL_CWD`` (the worktree path set by
         ``cli.py``/``main.py`` for ``-w`` sessions). Used even before any
         terminal command has populated the live cwd registry.
      4. The process cwd.

    The returned base is ALWAYS absolute. This is the core invariant that
    prevents the worktree-cwd divergence bug: a relative or sentinel
    ``TERMINAL_CWD`` (commonly the literal ``"."`` from a stale config) is
    meaningless as a resolution anchor — left to ``Path.resolve()`` it silently
    resolves against whatever the agent PROCESS cwd happens to be (e.g. the main
    repo while the terminal is in a worktree), routing edits to the wrong
    checkout. We therefore reject sentinel/relative ``TERMINAL_CWD`` values
    outright (rather than anchoring them to the process cwd) and fall through to
    the process cwd only as a last resort, deterministically.
    """
    root = _authoritative_workspace_root(task_id)
    if container_paths is None:
        container_paths = _uses_container_paths(task_id)
    if root:
        base_text = _expand_tilde(root)
    else:
        base_text = os.getcwd()
    if container_paths:
        if not posixpath.isabs(base_text):
            base_text = posixpath.join(os.getcwd(), base_text)
        return _normalize_without_host_deref(base_text)
    # Git Bash ``pwd -P`` reports ``/c/Users/...``; translate before Path so
    # relative file-tool paths don't anchor under a nonexistent ``\\c\\Users``.
    from tools.environments.local import _msys_to_windows_path

    base_text = _msys_to_windows_path(base_text)
    if sys.platform == "win32":
        import ntpath

        if not ntpath.isabs(base_text):
            base_text = ntpath.join(os.getcwd(), base_text)
        return Path(ntpath.normpath(base_text))
    base = Path(base_text)
    if not base.is_absolute():
        # Last-resort anchoring: a live cwd should already be absolute, but if a
        # terminal backend ever reports a relative cwd, anchor it to the process
        # cwd once, here, so the result no longer depends on cwd at resolve().
        base = Path(os.getcwd()) / base
    return base.resolve()


def _resolve_path_for_task(filepath: str, task_id: str = "default") -> Path | PurePosixPath:
    """Resolve *filepath* against the task's absolute base directory.

    See :func:`_resolve_base_dir` for how the base is chosen. Absolute input
    paths are returned resolved-but-unanchored.

    On native Windows, Git Bash / MSYS drive paths (``/c/Users/...``) are
    translated to ``C:\\Users\\...`` before resolution so file tools don't
    treat them as relative ``\\c\\Users\\...`` under the process cwd.
    """
    container_paths = _uses_container_paths(task_id)
    if container_paths:
        expanded = _expand_tilde(filepath)
        if posixpath.isabs(expanded):
            return _normalize_without_host_deref(expanded)
        resolved = _resolve_base_dir(task_id, container_paths=True) / expanded
        return _normalize_without_host_deref(resolved)

    # Host paths only — never rewrite Linux paths inside a container/WSL env.
    from tools.environments.local import _msys_to_windows_path

    expanded = _expand_tilde(_msys_to_windows_path(filepath))
    if sys.platform == "win32":
        import ntpath

        if ntpath.isabs(expanded):
            return Path(ntpath.normpath(expanded))
        joined = ntpath.join(str(_resolve_base_dir(task_id, container_paths=False)), expanded)
        return Path(ntpath.normpath(joined))

    p = Path(expanded)
    if p.is_absolute():
        return p.resolve()
    resolved = _resolve_base_dir(task_id, container_paths=False) / p
    return resolved.resolve()


def _path_resolution_warning(filepath: str, resolved: Path, task_id: str = "default") -> str | None:
    """Warn when a relative path resolved OUTSIDE the task's workspace root.

    Surfaces the worktree-cwd divergence the moment it would matter: if the
    agent passes a relative path but it resolves under a directory that is not
    the workspace root (i.e. the edit is about to land in a different checkout
    than the one the agent is working in), return a message naming the absolute
    target. ``None`` when the path is absolute, the base is unknown, or the
    resolved path is correctly under the workspace root.

    The workspace root is the live terminal cwd when known, else a registered
    task/session cwd override, else a sentinel-free absolute ``$TERMINAL_CWD``
    — so a worktree or Desktop session whose terminal registry is still empty
    (no ``cd`` run yet) is warned on the very first write.
    """
    try:
        if Path(_expand_tilde(filepath)).is_absolute():
            return None
        workspace_root = _authoritative_workspace_root(task_id)
        if not workspace_root:
            return None  # No authoritative workspace root to compare against.
        if _uses_container_paths(task_id):
            root = _normalize_without_host_deref(Path(_expand_tilde(workspace_root)))
        else:
            root = Path(_expand_tilde(workspace_root)).resolve()
        # Is `resolved` inside `root`?
        try:
            resolved.relative_to(root)
            return None  # Inside the workspace — expected.
        except ValueError:
            return (
                f"Relative path {filepath!r} resolved to {str(resolved)!r}, which is "
                f"OUTSIDE the active workspace ({str(root)!r}). The edit will land in "
                f"a different directory than the terminal's cwd. If this is not "
                f"intended (e.g. a git-worktree session writing into the main "
                f"checkout), pass an absolute path under the workspace instead."
            )
    except Exception:
        return None


def _file_ops_uses_host_paths(file_ops) -> bool:
    """Return True when *file_ops* targets the same host filesystem as Hermes.

    Only then may we rewrite V4A header paths to resolved host-absolute
    paths: a container/remote backend has its own filesystem namespace where
    a host-absolute path would be meaningless.
    """
    env = getattr(file_ops, "env", None)
    if env is None:
        return True
    try:
        from tools.environments.local import LocalEnvironment
    except ImportError:
        return True
    return isinstance(env, LocalEnvironment)


def _rewrite_v4a_patch_paths_for_host(
    patch: str,
    path_to_resolved: dict,
    file_ops,
) -> str:
    """Rewrite V4A file headers to the exact host paths the tool layer resolved.

    ``patch_tool`` resolves every header path against the task's workspace for
    locking, staleness, and reporting, but historically handed the *original*
    patch text to ``file_ops.patch_v4a`` — so the shell layer re-resolved the
    (often relative) header against its own cwd, which can differ from the
    tool layer's workspace (the git-worktree cwd bug). That made a relative
    header land in a different directory than everything else the tool
    reported. This rewrites ``*** Update/Add/Delete/Move File:`` headers to the
    resolved absolute paths so both layers agree on the target.

    Header patterns mirror ``patch_parser`` (``\\s*`` after ``***`` accepts the
    no-space ``***Update File:`` form) and cover ``Move File: src -> dst``.
    Only applied when *file_ops* targets the host filesystem.
    """
    if not _file_ops_uses_host_paths(file_ops):
        return patch

    import re as _re

    def _resolved_or_original(raw: str) -> str:
        raw = raw.strip()
        return path_to_resolved.get(raw) or raw

    def _replace_single(match):
        prefix = match.group(1)
        resolved = _resolved_or_original(match.group(2))
        return f"{prefix}{resolved}"

    patch = _re.sub(
        r'^(\*\*\*\s*(?:Update|Add|Delete)\s+File:\s*)(.+)$',
        _replace_single,
        patch,
        flags=_re.MULTILINE,
    )

    def _replace_move(match):
        prefix = match.group(1)
        src = _resolved_or_original(match.group(2))
        dst = _resolved_or_original(match.group(3))
        return f"{prefix}{src} -> {dst}"

    patch = _re.sub(
        r'^(\*\*\*\s*Move\s+File:\s*)(.+?)\s*->\s*(.+)$',
        _replace_move,
        patch,
        flags=_re.MULTILINE,
    )
    return patch


def _is_blocked_device_path(path: str) -> bool:
    """Return True for concrete device/fd paths that can hang reads."""
    normalized = os.path.normpath(_expand_tilde(path))
    if normalized in _BLOCKED_DEVICE_PATHS:
        return True
    # /proc/self/fd/0-2 and /proc/<pid>/fd/0-2 are Linux aliases for stdio
    if normalized.startswith("/proc/") and normalized.endswith(
        ("/fd/0", "/fd/1", "/fd/2")
    ):
        return True
    # /proc/*/environ, /proc/*/cmdline, /proc/*/maps (and the maps variants
    # smaps, smaps_rollup, numa_maps) can leak secrets, command-line args, and
    # memory layout (ASLR bypass) from the host process (issue #4427).
    # /proc/*/mem exposes raw process memory; block it as defense-in-depth even
    # though it requires address knowledge to exploit usefully.
    # /proc/*/auxv leaks AT_RANDOM (stack canary seed) plus AT_BASE/AT_PHDR
    # load addresses — an ASLR oracle on par with maps. /proc/*/pagemap exposes
    # virtual->physical translation. Both are blocked alongside the maps family.
    # endswith matches both /proc/<pid>/X and /proc/<pid>/task/<tid>/X.
    if normalized.startswith("/proc/") and normalized.endswith(
        (
            "/environ",
            "/cmdline",
            "/maps",
            "/smaps",
            "/smaps_rollup",
            "/numa_maps",
            "/mem",
            "/auxv",
            "/pagemap",
        )
    ):
        return True
    return False


def _is_blocked_device(filepath: str, base_dir: str | Path | None = None) -> bool:
    """Return True if the path would hang the process (infinite output or blocking input).

    Check the literal path first so aliases like /dev/stdin are caught before
    they resolve to terminal-specific paths. Then check each symlink hop before
    the final resolved path so aliases to devices cannot bypass the guard.
    """
    expanded = _expand_tilde(filepath)
    if base_dir is not None and not os.path.isabs(expanded):
        expanded = os.path.join(os.fspath(base_dir), expanded)
    normalized = os.path.normpath(expanded)
    if _is_blocked_device_path(normalized):
        return True

    seen: set[str] = set()
    current = normalized
    for _ in range(20):
        try:
            target = os.readlink(current)
        except OSError:
            break
        if not os.path.isabs(target):
            target = os.path.join(os.path.dirname(current), target)
        target = os.path.normpath(target)
        if _is_blocked_device_path(target):
            return True
        if target in seen:
            break
        seen.add(target)
        current = target

    try:
        resolved = os.path.normpath(os.path.realpath(normalized))
    except (OSError, ValueError):
        return False
    if _is_blocked_device_path(resolved):
        return True
    return False


def _search_result_read_block_error(path: str, task_id: str = "default") -> str | None:
    """Return the read-safety error for a search result path.

    Search backends may return paths relative to the task cwd, while
    ``get_read_block_error`` expects an already-resolved path when the task cwd
    can differ from the Python process cwd. Mirror ``read_file_tool``'s path
    resolution before applying the shared read guard.
    """
    try:
        resolved = _resolve_path_for_task(path, task_id)
    except (OSError, ValueError, RuntimeError):
        return get_read_block_error(path)
    return get_read_block_error(str(resolved))


def _filter_read_blocked_search_results(result, task_id: str = "default") -> int:
    """Remove credential/cache/env paths from a SearchResult in-place."""
    omitted = 0

    if hasattr(result, "matches") and result.matches:
        allowed_matches = []
        for match in result.matches:
            if _search_result_read_block_error(match.path, task_id):
                omitted += 1
                continue
            allowed_matches.append(match)
        result.matches = allowed_matches

    if hasattr(result, "files") and result.files:
        allowed_files = []
        for file_path in result.files:
            if _search_result_read_block_error(file_path, task_id):
                omitted += 1
                continue
            allowed_files.append(file_path)
        result.files = allowed_files

    if hasattr(result, "counts") and result.counts:
        allowed_counts = {}
        for file_path, count in result.counts.items():
            if _search_result_read_block_error(file_path, task_id):
                omitted += 1
                continue
            allowed_counts[file_path] = count
        result.counts = allowed_counts

    return omitted


# Paths that file tools should refuse to write to without going through the
# terminal tool's approval system.  These match prefixes after os.path.realpath.
_SENSITIVE_PATH_PREFIXES = (
    "/etc/", "/boot/", "/usr/lib/systemd/",
    "/private/etc/",
    # macOS: /private/var mirrors /var. Block the sensitive subtrees, NOT the
    # whole thing — a blanket "/private/var/" refused every legitimate temp-file
    # write, because $TMPDIR, /tmp, and /var/folders all realpath() into
    # /private/var/folders/... on macOS (and _resolve_path_for_task resolves
    # symlinks), and /private/var/tmp is a normal temp dir.
    "/private/var/db/", "/private/var/root/",
)
_SENSITIVE_EXACT_PATHS = {"/var/run/docker.sock", "/run/docker.sock"}

_hermes_config_resolved: str | None = None
_hermes_config_resolved_loaded = False


def _get_hermes_config_resolved() -> str | None:
    """Return the resolved absolute path of the Hermes config file (cached)."""
    global _hermes_config_resolved, _hermes_config_resolved_loaded
    if _hermes_config_resolved_loaded:
        return _hermes_config_resolved
    _hermes_config_resolved_loaded = True
    try:
        from hermes_cli.config import get_config_path
        _hermes_config_resolved = str(get_config_path().resolve())
    except Exception:
        try:
            _hermes_config_resolved = str(Path(_expand_tilde("~/.hermes/config.yaml")).resolve())
        except Exception:
            _hermes_config_resolved = None
    return _hermes_config_resolved


def _check_sensitive_path(filepath: str, task_id: str = "default") -> str | None:
    """Return an error message if the path targets a sensitive system location."""
    try:
        resolved = str(_resolve_path_for_task(filepath, task_id))
    except (OSError, ValueError):
        resolved = filepath
    normalized = os.path.normpath(_expand_tilde(filepath))
    _err = (
        f"Refusing to write to sensitive system path: {filepath}\n"
        "Use the terminal tool with sudo if you need to modify system files."
    )
    for prefix in _SENSITIVE_PATH_PREFIXES:
        if resolved.startswith(prefix) or normalized.startswith(prefix):
            return _err
    if resolved in _SENSITIVE_EXACT_PATHS or normalized in _SENSITIVE_EXACT_PATHS:
        return _err
    # Prevent agents from modifying the Hermes config file directly.
    # approvals.mode and other security settings live here; a malicious or
    # prompt-injected agent could silently disable exec approval by writing to
    # this file.
    hermes_config = _get_hermes_config_resolved()
    if hermes_config and (resolved == hermes_config or normalized == hermes_config):
        return (
            f"Refusing to write to Hermes config file: {filepath}\n"
            "Agent cannot modify security-sensitive configuration. "
            "Edit ~/.hermes/config.yaml directly or use 'hermes config' instead."
        )
    return None


# ---------------------------------------------------------------------------
# Protected agent-instruction files (always-ask approval gate)
# ---------------------------------------------------------------------------
# Files that steer FUTURE agent behavior are a prompt-injection persistence
# vector: an injected instruction that edits AGENTS.md / CLAUDE.md / SOUL.md /
# .cursorrules (or a project-local .hermes config tree) outlives the current
# turn and poisons every later session that loads it. Writes to these files
# therefore ALWAYS require human approval — even under --yolo / auto-approve —
# and fail closed when no human channel exists.
#
# Ported from: RooCodeInc/Roo-Code RooProtectedController (Apache-2.0).
# Companion: the terminal-tool vector is covered separately (#58631); this
# gate covers the write_file/patch vector. Symlink lesson from #41351:
# always realpath before matching.
#
# Scope decision (documented): basenames match in ANY directory, because
# project-context instruction files are loaded from cwd trees — an
# AGENTS.md anywhere the agent might later run from is a live target.
# Basenames match case-insensitively so case-variant spellings on
# case-insensitive filesystems (macOS/Windows) cannot slip past; on
# case-sensitive filesystems most loaders probe common case variants too,
# so the stricter behavior is kept uniform.
_PROTECTED_INSTRUCTION_BASENAMES = frozenset({
    "agents.md", "claude.md", "soul.md", ".cursorrules",
})

_real_hermes_home_cached: str | None = None
_real_hermes_home_loaded = False


def _get_real_hermes_home() -> str | None:
    """Return the realpath of the authoritative Hermes home (cached)."""
    global _real_hermes_home_cached, _real_hermes_home_loaded
    if _real_hermes_home_loaded:
        return _real_hermes_home_cached
    _real_hermes_home_loaded = True
    try:
        from hermes_constants import get_hermes_home
        _real_hermes_home_cached = os.path.realpath(str(get_hermes_home()))
    except Exception:
        try:
            _real_hermes_home_cached = os.path.realpath(_expand_tilde("~/.hermes"))
        except Exception:
            _real_hermes_home_cached = None
    return _real_hermes_home_cached


def _protected_instruction_config() -> tuple[bool, list[str]]:
    """Read the protected-instruction-files gate config.

    Returns ``(enabled, extra_patterns)``. Defaults to enabled with no extra
    patterns; config read failures keep the gate ON (fail-safe for a
    security boundary).

    Config keys (config.yaml)::

        security:
          protected_instruction_files: true       # default
          protected_instruction_extra_patterns: []  # fnmatch on basename
    """
    try:
        from hermes_cli.config import load_config, cfg_get
        cfg = load_config()
        enabled = cfg_get(cfg, "security", "protected_instruction_files",
                          default=True)
        extra = cfg_get(cfg, "security", "protected_instruction_extra_patterns",
                        default=[])
    except Exception:
        return True, []
    if not isinstance(enabled, bool):
        enabled = True
    if not isinstance(extra, list):
        extra = []
    return enabled, [str(p) for p in extra if p]


def _protected_instruction_reason(filepath: str, task_id: str = "default",
                                  *, enabled: bool | None = None,
                                  extra_patterns: list[str] | None = None) -> str | None:
    """Return a short label when ``filepath`` targets a protected
    agent-instruction file, else ``None``.

    Matching runs on BOTH the normalized input path and its realpath so
    neither a symlink pointing AT a protected file (#41351) nor a protected
    name that is itself a symlink escapes the gate. ``..`` traversal is
    neutralized by normpath/realpath before the basename compare.
    """
    if enabled is None or extra_patterns is None:
        enabled, extra_patterns = _protected_instruction_config()
    if not enabled:
        return None

    normalized = os.path.normpath(_expand_tilde(filepath))
    try:
        resolved = os.path.realpath(str(_resolve_path_for_task(filepath, task_id)))
    except (OSError, ValueError, RuntimeError):
        resolved = os.path.realpath(normalized)

    # The authoritative ~/.hermes home is governed by its own guards
    # (config.yaml hard-block, cross-profile guard, write_approval); this
    # gate targets PROJECT-LOCAL instruction files only. Checked before the
    # ``.hermes`` component rule below, which would otherwise match the
    # home directory itself.
    real_home = _get_real_hermes_home()
    if real_home and (resolved == real_home
                      or resolved.startswith(real_home + os.sep)):
        return None

    import fnmatch
    for candidate in (normalized, resolved):
        base = os.path.basename(candidate)
        base_lower = base.lower()
        if base_lower in _PROTECTED_INSTRUCTION_BASENAMES:
            return base
        for pattern in extra_patterns:
            if fnmatch.fnmatch(base_lower, pattern.lower()):
                return base
        # Project-local .hermes config dirs (e.g. <repo>/.hermes/config.yaml)
        # are loaded as project context and steer behavior the same way.
        # Scope: the file's IMMEDIATE parent must be ``.hermes`` — matching
        # any ancestor named .hermes would gate every write inside a
        # checkout that happens to live under ~/.hermes (e.g. the
        # hermes-agent repo itself at ~/.hermes/hermes-agent).
        parts = candidate.replace("\\", "/").rstrip("/").split("/")
        if len(parts) >= 2 and parts[-2] == ".hermes":
            return candidate
    return None


def _request_protected_instruction_approval(
        reasons: list[str], task_id: str = "default") -> str | None:
    """Ask the human to approve a write to protected instruction file(s).

    Returns ``None`` when approved, or a BLOCKED error string. This gate
    intentionally does NOT route through ``_run_approval_gate``: that gate
    honors --yolo and session/permanent allowlists, and the entire point
    here is one-operation approval EVERY time, with no persistent scope
    and no yolo bypass. Fail-closed when no human channel exists.
    """
    targets = ", ".join(dict.fromkeys(reasons))
    description = (
        f"Write to protected agent-instruction file(s): {targets}. "
        "These files steer future agent behavior; approval is always "
        "required (not bypassed by auto-approve)."
    )
    display = f"<write to {targets}>"
    blocked = (
        f"BLOCKED: write to protected agent-instruction file(s) ({targets}) "
        "{why} The user has NOT consented to this write. Do NOT retry it or "
        "attempt the same edit via another path (terminal, execute_code, "
        "etc.)."
    )

    try:
        import tools.approval as _approval
    except Exception:
        return blocked.format(why="requires approval but the approval "
                                  "subsystem is unavailable.")

    # Gateway surface: block on the button round-trip when a notify callback
    # is registered for this session (Telegram/Discord/Slack). One-operation
    # only — no session/permanent buttons are offered.
    session_key = _approval.get_current_session_key()
    notify_cb = None
    try:
        with _approval._lock:
            notify_cb = _approval._gateway_notify_cbs.get(session_key)
    except Exception:
        notify_cb = None

    if notify_cb is not None:
        approval_data = {
            "command": display,
            "pattern_key": "protected_instruction_file",
            "pattern_keys": ["protected_instruction_file"],
            "description": description,
            "allow_permanent": False,
            "allow_session": False,
        }
        decision = _approval._await_gateway_decision(
            session_key, notify_cb, approval_data, surface="gateway",
        )
        if decision.get("notify_failed"):
            return blocked.format(
                why="requires approval but the approval request could not "
                    "be delivered.")
        choice = decision.get("choice")
        if decision.get("resolved") and choice in {"once", "session", "always"}:
            # One-operation grant regardless of the tapped scope — nothing
            # is persisted for this gate.
            return None
        if not decision.get("resolved"):
            return blocked.format(
                why="approval prompt timed out without a user response. "
                    "Silence is not consent.")
        return blocked.format(why="was denied by the user.")

    # CLI surface: per-thread approval callback (prompt_toolkit panel).
    callback = None
    try:
        from tools.terminal_tool import _get_approval_callback
        callback = _get_approval_callback()
    except Exception:
        callback = None

    if callback is not None:
        choice = _approval.prompt_dangerous_approval(
            display, description,
            allow_permanent=False,
            allow_session=False,
            approval_callback=callback,
        )
        if choice in {"once", "session", "always"}:
            # One-operation grant; never persisted (see docstring).
            return None
        if choice == "timeout":
            return blocked.format(
                why="approval prompt timed out without a user response. "
                    "Silence is not consent.")
        return blocked.format(why="was denied by the user.")

    # No human channel at all (script, cron, background thread): fail
    # closed. Auto-approving here would recreate the persistence vector.
    return blocked.format(
        why="requires approval but no interactive user or gateway is "
            "present to approve it.")


def _check_protected_instruction_write(paths: list[str],
                                       task_id: str = "default") -> str | None:
    """Gate a write/patch touching protected instruction files.

    Returns ``None`` when no target is protected or the human approved;
    otherwise a BLOCKED error string. For multi-file V4A patches, ONE
    protected file gates the ENTIRE patch: a single prompt lists every
    protected target, and a deny applies nothing (including innocent
    files) — partial application of an approved-in-part patch would be
    more surprising than an atomic all-or-nothing outcome.
    """
    enabled, extra = _protected_instruction_config()
    if not enabled:
        return None
    reasons: list[str] = []
    for p in paths:
        reason = _protected_instruction_reason(
            p, task_id, enabled=enabled, extra_patterns=extra)
        if reason:
            reasons.append(reason)
    if not reasons:
        return None
    return _request_protected_instruction_approval(reasons, task_id)


def _check_approval_required_write(paths: list[str],
                                   task_id: str = "default") -> str | None:
    """Gate a write/patch touching an approval-required path (``~/.ssh/config``).

    These paths are NOT credentials and NOT hard-denied, but a write must
    be confirmed by a human because they can steer process execution
    (an SSH ``ProxyCommand`` / ``Match exec``). Unlike the protected-
    instruction gate this is a routine, user-initiated edit, so the prompt
    offers once/session/always scopes and honors --yolo (the historical
    dangerous-command semantics) rather than always re-asking.

    Returns ``None`` when no target is approval-gated or the human
    approved; otherwise a BLOCKED error string. Fail-closed when no
    interactive/gateway channel exists (a background/ACP caller cannot
    consent on the user's behalf).
    """
    try:
        from agent.file_safety import is_write_approval_required
    except Exception:
        return None

    targets = [p for p in paths if is_write_approval_required(p)]
    if not targets:
        return None

    display_targets = ", ".join(dict.fromkeys(targets))
    description = (
        f"Write to SSH client config file(s): {display_targets}. "
        "The SSH config can carry ProxyCommand / Match exec directives that "
        "run commands, so writes require your approval."
    )
    blocked = (
        f"BLOCKED: write to SSH config file(s) ({display_targets}) "
        "{why} Do NOT retry it via another path (terminal, execute_code) "
        "without the user's explicit consent."
    )

    try:
        import tools.approval as _approval
    except Exception:
        return blocked.format(why="requires approval but the approval "
                                  "subsystem is unavailable.")

    result = _approval._run_approval_gate(
        pattern_key="ssh_config_write",
        description=description,
        display_target=f"<write to {display_targets}>",
        cron_deny_message=blocked.format(
            why="requires approval but this cron session denies it."),
        single_query_deny_message=blocked.format(
            why="requires approval but single-query (-q) sessions run "
                "without a user present to approve it. To allow flagged "
                "actions in single-query mode, set approvals.single_query_mode: "
                "approve in config.yaml."),
        autoapprove_log_prefix="ssh_config_write",
        fail_closed_when_no_human=True,
        no_human_block_message=blocked.format(
            why="requires approval but no interactive user or gateway is "
                "present to approve it."),
    )
    if result.get("approved"):
        return None
    return result.get("message") or blocked.format(why="was denied.")


def _get_container_mirror_prefix_for_task(task_id: str = "default") -> str | None:
    """Return the container-side Hermes mirror prefix for Docker file tools."""
    try:
        from tools.terminal_tool import (
            _active_environments,
            _env_lock,
            _get_env_config,
            _resolve_container_task_id,
        )

        container_key = _resolve_container_task_id(task_id)
    except Exception:
        return None

    try:
        with _env_lock:
            env = _active_environments.get(container_key) or _active_environments.get(task_id)

        if env is not None:
            if env.__class__.__name__ == "DockerEnvironment" and bool(
                getattr(env, "_persistent", False)
            ):
                return "/root/.hermes"
            return None

        config = _get_env_config()
    except Exception:
        return None

    if config.get("env_type") == "docker" and config.get("container_persistent", True):
        return "/root/.hermes"
    return None


def _check_cross_profile_path(filepath: str, task_id: str = "default") -> str | None:
    """Return a soft-guard warning when ``filepath`` lands on a host-side
    sandbox-mirror of authoritative profile state, or the Docker
    container's sandbox mirror of Hermes state.

    Two detectors (both #32049): these catch writes that would be
    SILENTLY LOST — the host Hermes process never reads the mirror, so
    the write succeeds but changes nothing. That is a lost-work guard,
    not profile isolation.

    NOTE: the third detector this shared check used to run — the
    cross-PROFILE write guard (another profile's skills/plugins/cron/
    memories) — was removed by maintainer decision: profiles were never
    isolated (same OS user; terminal writes anywhere), so the guard was
    ceremony. The system prompt's profile hint remains the only
    steering. ``cross_profile=True`` still bypasses the mirror guards
    (name kept for replay/transcript compat).

    Returns ``None`` when the write is in-scope or outside Hermes scope.
    """
    try:
        from agent.file_safety import (
            get_container_mirror_warning,
            get_sandbox_mirror_warning,
        )
    except Exception:
        # Fail open on import error — the existing sensitive-path guard
        # plus the write_denied list still apply.
        return None

    # Resolve via the task's cwd so a relative path in a session that
    # cd'd elsewhere is classified against the right base.
    try:
        resolved = str(_resolve_path_for_task(filepath, task_id))
    except (OSError, ValueError):
        resolved = filepath

    warning = get_sandbox_mirror_warning(resolved)
    if warning is not None:
        return warning

    return get_container_mirror_warning(
        resolved,
        mirror_prefix=_get_container_mirror_prefix_for_task(task_id),
    )


def _is_expected_write_exception(exc: Exception) -> bool:
    """Return True for expected write denials that should not hit error logs."""
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError) and exc.errno in _EXPECTED_WRITE_ERRNOS:
        return True
    return False


_file_ops_lock = threading.Lock()
_file_ops_cache: dict = {}

# Track files read per task to detect re-read loops and deduplicate reads.
# Per task_id we store:
#   "last_key":     the key of the most recent read/search call (or None)
#   "consecutive":  how many times that exact call has been repeated in a row
#   "read_history": set of (path, offset, limit) tuples for get_read_files_summary
#   "dedup":        dict mapping (resolved_path, offset, limit) → mtime float
#                   Used to skip re-reads of unchanged files.  Survives
#                   context compression so unchanged files can resume
#                   returning lightweight stubs after one recovery read.
#   "dedup_generation_reads": set of dedup keys whose full content has been
#                   served since the latest compaction boundary. Cleared on
#                   compression so the first post-compaction read can recover
#                   exact bytes that the summary may have omitted.
#   "read_timestamps": dict mapping resolved_path → modification-time float
#                      recorded when the file was last read (or written) by
#                      this task.  Used by write_file and patch to detect
#                      external changes between the agent's read and write.
#                      Updated after successful writes so consecutive edits
#                      by the same task don't trigger false warnings.
_read_tracker_lock = threading.Lock()
_read_tracker: dict = {}

# Track consecutive patch failures per (task_id, resolved_path).  Used to
# escalate the hint when the model repeatedly fails to patch the same file
# (typical cause: stale view of file contents, ambiguous old_string, or
# the file was modified externally between the agent's read and patch
# attempt).  Reset on a successful patch to that path.
_patch_failure_lock = threading.Lock()
_patch_failure_tracker: dict = {}  # {task_id: {resolved_path: count}}


def _record_patch_failure(task_id: str, resolved_path: str) -> int:
    """Increment and return the consecutive-failure count for this path."""
    with _patch_failure_lock:
        task_failures = _patch_failure_tracker.setdefault(task_id, {})
        # Cap dict size per task to avoid unbounded growth in long sessions
        # where the agent fails on many distinct files.  64 distinct
        # failing files per task is generous; older entries get evicted.
        if len(task_failures) >= 64 and resolved_path not in task_failures:
            try:
                first_key = next(iter(task_failures))
                del task_failures[first_key]
            except StopIteration:
                pass
        task_failures[resolved_path] = task_failures.get(resolved_path, 0) + 1
        return task_failures[resolved_path]


def _reset_patch_failures(task_id: str, resolved_paths: list) -> None:
    """Clear consecutive-failure counts for the given paths."""
    if not resolved_paths:
        return
    with _patch_failure_lock:
        task_failures = _patch_failure_tracker.get(task_id)
        if not task_failures:
            return
        for rp in resolved_paths:
            task_failures.pop(rp, None)

# ── #1703 — empty-old_string loop guard ────────────────────────────────────
# When the model calls the patch tool in replace mode with an EMPTY (falsy)
# old_string, no valid replace can ever match — it is a usage error, not a
# match failure. The old "is None" guard at the top of patch_tool let an empty
# string fall through to patch_replace, which returned a generic error the
# model then retried 5-8 times (#1703). We return a targeted, actionable
# diagnostic at the tool boundary, and track consecutive identical failures
# per task+path so the second one escalates to a hard STOP — breaking the
# spiral at 2 instead of 8.
_empty_old_string_lock = threading.Lock()
_empty_old_string_tracker: dict = {}  # {task_id: {path: count}}


def _record_empty_old_string(task_id: str, path: str) -> int:
    """Increment and return the consecutive empty-old_string count for a path."""
    with _empty_old_string_lock:
        per_task = _empty_old_string_tracker.setdefault(task_id, {})
        if len(per_task) >= 64 and path not in per_task:
            try:
                first_key = next(iter(per_task))
                del per_task[first_key]
            except StopIteration:
                pass
        per_task[path] = per_task.get(path, 0) + 1
        return per_task[path]


def _reset_empty_old_string(task_id: str, path: str) -> None:
    """Clear the empty-old_string counter for a path — called when the model
    supplies a non-empty old_string (it has moved past the empty shape) or
    when a patch to that path succeeds."""
    if not path:
        return
    with _empty_old_string_lock:
        per_task = _empty_old_string_tracker.get(task_id)
        if per_task:
            per_task.pop(path, None)


def _empty_old_string_error(path: str, task_id: str) -> str:
    """Non-retryable diagnostic for replace-mode with an empty old_string."""
    count = _record_empty_old_string(task_id, path)
    msg = (
        "replace mode requires a non-empty old_string. The old_string is the "
        "exact text to find and replace in the file; an empty string cannot be "
        "matched. Either supply the text to replace, or use mode=patch (V4A "
        "format) for insertions."
    )
    if count >= 2:
        suffix = {2: "nd", 3: "rd"}.get(count % 10, "th")
        msg += (
            f" STOP: this is the {count}{suffix} consecutive replace-mode call with an "
            f"empty old_string on {path!r}. Do not retry this shape — re-read the "
            f"file to see the exact text, then supply a real old_string, or use "
            f"mode=patch / write_file instead."
        )
    return tool_error(msg)


# #3238 — patch preflight counter: every replace-mode call blocked for an
# empty/short/ambiguous old_string increments this per-task counter.  It is
# surfaced in the structured error so the spiral guard can act on it.
_patch_preflight_blocked_lock = threading.Lock()
_patch_preflight_blocked_counter: dict[str, int] = {}


def _record_patch_preflight_blocked(task_id: str) -> int:
    """Increment and return the per-task patch-preflight block count."""
    with _patch_preflight_blocked_lock:
        _patch_preflight_blocked_counter[task_id] = (
            _patch_preflight_blocked_counter.get(task_id, 0) + 1
        )
        return _patch_preflight_blocked_counter[task_id]


def _patch_preflight_blocked_structured(
    path: str, reason: str, task_id: str, extra_message: str = ""
) -> str:
    """Return a structured re_read_file instruction for a blocked patch.

    The payload is a non-retryable correction request that tells the model to
    re-read the target file and try again with a more precise old_string.
    """
    count = _record_empty_old_string(task_id, path)
    preflight_count = _record_patch_preflight_blocked(task_id)
    msg = (
        "replace mode requires a non-empty old_string. The old_string is the "
        "exact text to find and replace in the file; an empty string cannot be "
        "matched. Either supply the text to replace, or use mode=patch (V4A "
        "format) for insertions."
    )
    if count >= 2:
        suffix = {2: "nd", 3: "rd"}.get(count % 10, "th")
        msg += (
            f" STOP: this is the {count}{suffix} consecutive replace-mode call with an "
            f"empty old_string on {path!r}. Do not retry this shape — re-read the "
            f"file to see the exact text, then supply a real old_string, or use "
            f"mode=patch / write_file instead."
        )
    message = (
        f"Patch blocked by preflight: {reason}. "
        f"Re-read {path!r} with read_file, copy the exact text you want to replace, "
        f"and retry with a longer, unambiguous old_string."
    )
    if extra_message:
        message += " " + extra_message
    return json.dumps(
        {
            "error": msg,
            "action": "re_read_file",
            "path": path,
            "reason": reason,
            "message": message,
            "patch_preflight_blocked": preflight_count,
            "argument_shape_spiral": preflight_count >= 3,
        },
        ensure_ascii=False,
    )

# Per-task bounds for the containers inside each _read_tracker[task_id].
# A CLI session uses one stable task_id for its lifetime; without these
# caps, a 10k-read session would accumulate ~1.5MB of dict/set state that
# is never referenced again (only the most recent reads matter for dedup,
# loop detection, and external-edit warnings).  Hard caps bound the
# accretion to a few hundred KB regardless of session length.
_READ_HISTORY_CAP = 500       # set; used only by get_read_files_summary
_DEDUP_CAP = 1000             # dict; skip-identical-reread guard
_READ_TIMESTAMPS_CAP = 1000   # dict; external-edit detection for write/patch
_NOT_FOUND_CAP = 500          # dict; per-task negative-result cache for missing paths
_NOT_FOUND_TTL_SECONDS = 60.0 # short TTL — a path that didn't exist may be created soon
_READ_DEDUP_STATUS_MESSAGE = (
    "File unchanged since last read. The content from "
    "the earlier read_file result in this conversation is "
    "still current — refer to that instead of re-reading."
)


def _cap_read_tracker_data(task_data: dict) -> None:
    """Enforce size caps on the per-task read-tracker sub-containers.

    Must be called with ``_read_tracker_lock`` held.  Eviction policy:

      * ``read_history`` (set): pop arbitrary entries on overflow.  This
        is fine because the set only feeds diagnostic summaries; losing
        old entries just trims the summary's tail.
      * ``dedup`` / ``read_timestamps`` (dict): pop oldest by insertion
        order (Python 3.7+ dicts).  Evicted entries lose their dedup
        skip on a future re-read (the file gets re-sent once) and
        external-edit mtime comparison (the write/patch falls back to
        a non-mtime check).  Both are graceful degradations, not bugs.
    """
    rh = task_data.get("read_history")
    if rh is not None and len(rh) > _READ_HISTORY_CAP:
        excess = len(rh) - _READ_HISTORY_CAP
        for _ in range(excess):
            try:
                rh.pop()
            except KeyError:
                break

    dedup = task_data.get("dedup")
    if dedup is not None and len(dedup) > _DEDUP_CAP:
        excess = len(dedup) - _DEDUP_CAP
        for _ in range(excess):
            try:
                dedup.pop(next(iter(dedup)))
            except (StopIteration, KeyError):
                break

    dedup_hits = task_data.get("dedup_hits")
    if dedup_hits is not None and len(dedup_hits) > _DEDUP_CAP:
        excess = len(dedup_hits) - _DEDUP_CAP
        for _ in range(excess):
            try:
                dedup_hits.pop(next(iter(dedup_hits)))
            except (StopIteration, KeyError):
                break

    generation_reads = task_data.get("dedup_generation_reads")
    if generation_reads is not None and len(generation_reads) > _DEDUP_CAP:
        excess = len(generation_reads) - _DEDUP_CAP
        for _ in range(excess):
            try:
                generation_reads.pop()
            except KeyError:
                break

    ts = task_data.get("read_timestamps")
    if ts is not None and len(ts) > _READ_TIMESTAMPS_CAP:
        excess = len(ts) - _READ_TIMESTAMPS_CAP
        for _ in range(excess):
            try:
                ts.pop(next(iter(ts)))
            except (StopIteration, KeyError):
                break

    nf = task_data.get("not_found")
    if nf is not None and len(nf) > _NOT_FOUND_CAP:
        excess = len(nf) - _NOT_FOUND_CAP
        for _ in range(excess):
            try:
                nf.pop(next(iter(nf)))
            except (StopIteration, KeyError):
                break


def _check_not_found_cache(op: str, resolved_str: str, task_id: str) -> str | None:
    """Return cached not-found JSON for *(op, resolved_str)* if still fresh.

    Skips the expensive subprocess + suggestion walk when the model retries
    the same missing path. Observed in agent.log: a single typo'd path was
    retried 13 times — each retry forked a shell to walk the parent directory
    and score similar names.

    *op* is "read" or "search" — kept separate because the two callers return
    different error JSON shapes ("File not found:" vs "Path not found:").

    Eviction: TTL or write_file/patch on the path (see invalidate_for_path).
    """
    import os as _os
    import time
    with _read_tracker_lock:
        task_data = _read_tracker.get(task_id)
        if not task_data:
            return None
        nf = task_data.get("not_found")
        if not nf:
            return None
        entry = nf.get((op, resolved_str))
        if entry is None:
            return None
        ts, cached_json = entry
        if time.monotonic() - ts > _NOT_FOUND_TTL_SECONDS:
            nf.pop((op, resolved_str), None)
            return None
    # Existence guard: the path may have been created since we cached the
    # miss — by a terminal command, another agent, or any external process
    # (write_file/patch invalidate explicitly, but they're not the only
    # writers). The agent pattern "check file → create it → read it" is
    # common; serving a stale miss for up to the TTL breaks it. One stat is
    # ~free next to the subprocess walk we're skipping.
    #
    # The stat runs OUTSIDE _read_tracker_lock (matching the dedup mtime
    # check below in read_file_tool): the lock is global across all tasks,
    # and a hung stat on a dead network mount must not stall every other
    # task's read/search bookkeeping.
    if _os.path.exists(resolved_str):
        with _read_tracker_lock:
            task_data = _read_tracker.get(task_id)
            nf = task_data.get("not_found") if task_data else None
            if nf:
                nf.pop((op, resolved_str), None)
        return None
    return cached_json


def _record_not_found(op: str, resolved_str: str, task_id: str, error_json: str) -> None:
    """Cache a not-found error so the next *op* call for *resolved_str* skips I/O."""
    import time
    with _read_tracker_lock:
        task_data = _read_tracker.setdefault(task_id, {
            "last_key": None, "consecutive": 0,
            "read_history": set(), "dedup": {},
            "dedup_hits": {}, "read_timestamps": {},
        })
        nf = task_data.setdefault("not_found", {})
        nf[(op, resolved_str)] = (time.monotonic(), error_json)
        _cap_read_tracker_data(task_data)


def _is_internal_file_status_text(content: str) -> bool:
    """Return True when content looks like an internal file-tool status, not real file bytes.

    The read_file dedup status message must never be persisted as file
    content.  The obvious shape is the model echoing the message verbatim,
    but in practice it also wraps it with small framing text (a leading
    "Note:", a trailing newline + short comment, etc.) before calling
    write_file.  We treat any short-ish write whose body is dominated by
    the status message as the same class of corruption.

    Heuristic:
      * Strict equality (after strip) — the verbatim shape.
      * OR the stripped content contains the full status message AND is
        short enough that the status dominates it (<=2x the message length).
        Short, status-dominated writes can't plausibly be real files —
        legitimate docs/notes that happen to quote this internal message
        are always dramatically longer.
    """
    if not isinstance(content, str):
        return False
    stripped = content.strip()
    if not stripped:
        return False
    if stripped == _READ_DEDUP_STATUS_MESSAGE:
        return True
    if _READ_DEDUP_STATUS_MESSAGE in stripped and \
            len(stripped) <= 2 * len(_READ_DEDUP_STATUS_MESSAGE):
        return True
    return False


def _looks_like_read_file_line_numbered_content(content: str) -> bool:
    """Return True for content dominated by read_file's ``LINE_NUM|CONTENT`` display.

    ``read_file`` intentionally returns line-numbered text to the model. If
    that display format is echoed into ``write_file``, config/source files are
    silently corrupted with prefixes like `` 1|``.  We reject writes where the
    non-empty lines are mostly consecutive read_file-style numbered lines, while
    allowing sparse literal pipe content such as a single ``1|value`` line.
    """
    if not isinstance(content, str):
        return False

    lines = [line for line in content.splitlines() if line.strip()]
    if len(lines) < 2:
        return False

    numbered: list[int] = []
    for line in lines:
        stripped = line.lstrip()
        prefix, sep, _rest = stripped.partition("|")
        if sep and prefix.isdigit():
            numbered.append(int(prefix))

    if len(numbered) < 2:
        return False
    if len(numbered) / len(lines) < 0.6:
        return False

    consecutive_pairs = sum(
        1 for prev, current in zip(numbered, numbered[1:])
        if current == prev + 1
    )
    return consecutive_pairs >= len(numbered) - 1


def _is_internal_file_tool_content(content: str) -> bool:
    """Return True when content is file-tool display text, not intended file bytes."""
    return (
        _is_internal_file_status_text(content)
        or _looks_like_read_file_line_numbered_content(content)
    )


def _get_file_ops(task_id: str = "default") -> ShellFileOperations:
    """Get or create ShellFileOperations for a terminal environment.

    Respects the TERMINAL_ENV setting -- if the task_id doesn't have an
    environment yet, creates one using the configured backend (local, docker,
    modal, etc.) rather than always defaulting to local.

    Thread-safe: uses the same per-task creation locks as terminal_tool to
    prevent duplicate sandbox creation from concurrent tool calls.

    Note: subagent task_ids are collapsed to "default" via
    ``_resolve_container_task_id`` so delegate_task children share the
    parent's container and its cached file_ops. RL/benchmark task_ids with
    a registered env override keep their isolation.
    """
    from tools.terminal_tool import (
        _active_environments, _env_lock, _create_environment,
        _get_env_config, _last_activity, _start_cleanup_thread,
        _creation_locks,
        _creation_locks_lock,
        _resolve_container_task_id,
        _resolve_task_host_cwd,
        _is_unusable_container_cwd,
        _CONTAINER_BACKENDS,
    )
    import time

    raw_task_id = task_id or "default"
    task_id = _resolve_container_task_id(raw_task_id)

    # Fast path: check cache -- but also verify the underlying environment
    # is still alive (it may have been killed by the cleanup thread).
    with _file_ops_lock:
        cached = _file_ops_cache.get(task_id)
    if cached is not None:
        with _env_lock:
            if task_id in _active_environments:
                _last_activity[task_id] = time.time()
                return cached
            else:
                # Environment was cleaned up -- preserve the old cwd in the
                # session record before invalidating the stale cache entry
                # (fixes #26211: silent file-creation failures in long-running
                # conversations). Usually a no-op: every completed command
                # already recorded its cwd.
                #
                # Fill-only: ``cached.cwd`` is a snapshot of the SHARED env's
                # cwd at cache-build time, so it is not attributable to this
                # session (same class as the interrupted-command bug, #85658).
                # Rescue a session that has no record, but never overwrite a
                # record the session wrote for itself.
                old_cwd = getattr(cached, "cwd", None)
                if old_cwd:
                    try:
                        from tools.terminal_tool import (
                            get_session_cwd,
                            record_session_cwd,
                        )
                        if get_session_cwd(raw_task_id) is None:
                            record_session_cwd(raw_task_id, old_cwd)
                    except Exception:
                        pass
                with _file_ops_lock:
                    _file_ops_cache.pop(task_id, None)

    # Need to ensure the environment exists before building file_ops.
    # Acquire per-task lock so only one thread creates the sandbox.
    with _creation_locks_lock:
        if task_id not in _creation_locks:
            _creation_locks[task_id] = threading.Lock()
        task_lock = _creation_locks[task_id]

    with task_lock:
        # Double-check: another thread may have created it while we waited
        with _env_lock:
            if task_id in _active_environments:
                _last_activity[task_id] = time.time()
                terminal_env = _active_environments[task_id]
            else:
                terminal_env = None

        if terminal_env is None:
            from tools.terminal_tool import resolve_task_overrides

            config = _get_env_config()
            env_type = config["env_type"]
            overrides = resolve_task_overrides(raw_task_id)

            if env_type == "docker":
                image = overrides.get("docker_image") or config["docker_image"]
            elif env_type == "singularity":
                image = overrides.get("singularity_image") or config["singularity_image"]
            elif env_type == "modal":
                image = overrides.get("modal_image") or config["modal_image"]
            elif env_type == "daytona":
                image = overrides.get("daytona_image") or config["daytona_image"]
            else:
                image = ""

            try:
                from tools.terminal_tool import get_session_cwd
                recorded_cwd = get_session_cwd(raw_task_id)
            except Exception:
                recorded_cwd = None
            cwd = overrides.get("cwd") or recorded_cwd or config["cwd"]
            # Re-apply the container cwd guard that _get_env_config() already
            # ran on config["cwd"] (see #50636).  A per-task cwd override
            # registered by the gateway/TUI/ACP for workspace tracking is a
            # raw host path (e.g. a Desktop session's /Users/<me>/workspace or
            # C:\\Users\\<me>). On a container backend that reaches
            # ``docker run -w <host-path>`` and the container starts in a
            # directory that doesn't exist inside the sandbox, so search_files
            # and friends silently return empty results (#54447).  Sanitize it
            # back to the already-validated config["cwd"] so the override can't
            # bypass the guard.  Valid in-container override paths (RL/benchmark
            # sandboxes that set cwd to /workspace, /root, etc.) are absolute
            # non-host paths and pass through untouched.
            if env_type in _CONTAINER_BACKENDS and _is_unusable_container_cwd(cwd):
                if cwd != config["cwd"]:
                    logger.info(
                        "Ignoring host/relative cwd override %r for %s backend "
                        "(won't exist in sandbox). Using %r instead.",
                        cwd, env_type, config["cwd"],
                    )
                cwd = config["cwd"]
            logger.info("Creating new %s environment for task %s...", env_type, task_id[:8])

            container_config = None
            from tools.terminal_tool import _is_container_backend as _is_container

            if _is_container(env_type):
                container_config = {
                    "container_cpu": config.get("container_cpu", 1),
                    "container_memory": config.get("container_memory", 5120),
                    "container_disk": config.get("container_disk", 51200),
                    "container_persistent": config.get("container_persistent", True),
                    "vercel_runtime": config.get("vercel_runtime", ""),
                    "docker_volumes": config.get("docker_volumes", []),
                    "docker_mount_cwd_to_workspace": config.get("docker_mount_cwd_to_workspace", False),
                    "docker_forward_env": config.get("docker_forward_env", []),
                    "docker_run_as_host_user": config.get("docker_run_as_host_user", False),
                    "docker_network": config.get("docker_network", True),
                }

            ssh_config = None
            if env_type == "ssh":
                ssh_config = {
                    "host": config.get("ssh_host", ""),
                    "user": config.get("ssh_user", ""),
                    "port": config.get("ssh_port", 22),
                    "key": config.get("ssh_key", ""),
                    "persistent": config.get("ssh_persistent", False),
                }

            local_config = None
            if env_type == "local":
                local_config = {
                    "persistent": config.get("local_persistent", False),
                }

            terminal_env = _create_environment(
                env_type=env_type,
                image=image,
                cwd=cwd,
                timeout=config["timeout"],
                ssh_config=ssh_config,
                container_config=container_config,
                local_config=local_config,
                task_id=task_id,
                host_cwd=_resolve_task_host_cwd(config, raw_task_id),
            )

            with _env_lock:
                _active_environments[task_id] = terminal_env
                _last_activity[task_id] = time.time()

            _start_cleanup_thread()
            logger.info("%s environment ready for task %s", env_type, task_id[:8])

    # Build file_ops from the (guaranteed live) environment and cache it
    file_ops = ShellFileOperations(terminal_env)
    with _file_ops_lock:
        _file_ops_cache[task_id] = file_ops
    return file_ops


def clear_file_ops_cache(task_id: str = None):
    """Clear file-operation state for a finished task, or all tasks."""
    with _file_ops_lock:
        if task_id:
            _file_ops_cache.pop(task_id, None)
        else:
            _file_ops_cache.clear()

    with _read_tracker_lock:
        if task_id:
            _read_tracker.pop(task_id, None)
        else:
            _read_tracker.clear()

    with _patch_failure_lock:
        if task_id:
            _patch_failure_tracker.pop(task_id, None)
        else:
            _patch_failure_tracker.clear()

    if task_id:
        file_state.get_registry().forget_task(task_id)
    else:
        file_state.get_registry().clear()


def _special_file_kind(path) -> str | None:
    """Return a human name for non-regular file types that block reads.

    Stat-based sibling of the name-based ``_is_blocked_device`` guard: a
    FIFO at ``logs/live.pipe`` or a socket in a workspace hangs ``read_file``
    just as hard as ``/dev/zero``, but carries no recognizable name. Only
    called for host-visible filesystems (see ``_file_ops_uses_host_paths``);
    remote backends cannot be statted from here.

    Returns None for regular files, missing paths, and anything unstattable
    (those flow to the normal read path and its own error handling).
    """
    import stat as _stat

    try:
        st = os.stat(os.fspath(path))  # follows symlinks, matching a real read
    except OSError:
        return None
    mode = st.st_mode
    if _stat.S_ISREG(mode) or _stat.S_ISDIR(mode):
        return None
    if _stat.S_ISFIFO(mode):
        return "a FIFO (named pipe)"
    if _stat.S_ISSOCK(mode):
        return "a socket"
    if _stat.S_ISCHR(mode):
        return "a character device"
    if _stat.S_ISBLK(mode):
        return "a block device"
    return "a special (non-regular) file"


def read_file_tool(path: str, offset: int = 1, limit: int = 2000, task_id: str = "default") -> str:
    """Read a file with pagination and line numbers."""
    try:
        if not path or not isinstance(path, str) or not path.strip():
            return tool_error("Invalid path: path must be a non-empty string.")
        offset, limit = normalize_read_pagination(offset, limit)

        # ── Device path guard ─────────────────────────────────────────
        # Block paths that would hang the process (infinite output,
        # blocking on input).  Pure path check — no I/O.
        device_base = None if Path(path).expanduser().is_absolute() else _resolve_base_dir(task_id)
        if _is_blocked_device(path, base_dir=device_base):
            return tool_error(
                f"Cannot read '{path}': this is a device file that would "
                "block or produce infinite output."
            )

        _resolved = _resolve_path_for_task(path, task_id)

        # ── Special-file type guard (stat-based) ──────────────────────
        # The name blocklist above catches /dev/* and /proc/* aliases; this
        # catches the class — any FIFO/socket/device wherever it lives. A
        # read on a FIFO blocks until the exec timeout: a self-shipped DoS.
        if _file_ops_uses_host_paths(_get_file_ops(task_id)):
            kind = _special_file_kind(_resolved)
            if kind is not None:
                return json.dumps({
                    "success": False,
                    "note": (
                        f"'{path}' is {kind}, not a regular file — reading "
                        "it would block indefinitely, so no read was "
                        "attempted. Use terminal utilities if you need to "
                        "interact with it."
                    ),
                })

        # ── Structured-document extraction ────────────────────────────
        # Try before the binary-extension guard so .docx/.xlsx can render as text.
        # Malformed documents fall through to the normal path/binary guard.
        from tools.read_extract import (
            ANYDOC_EXTENSIONS,
            EXTRACTABLE_EXTENSIONS,
            MAX_DOCUMENT_BYTES,
            ExtractionError,
            extract_document_bytes,
            is_extractable_document,
        )

        if is_extractable_document(str(_resolved)):
            file_ops = _get_file_ops(task_id)
            try:
                binary = file_ops.read_file_bytes(
                    str(_resolved), max_bytes=MAX_DOCUMENT_BYTES
                )
                if binary.error or binary.base64_content is None:
                    raise ExtractionError(binary.error or "Document bytes unavailable")
                document_bytes = base64.b64decode(
                    binary.base64_content, validate=True
                )
                extracted_text = extract_document_bytes(
                    document_bytes, str(_resolved)
                )
            except (ExtractionError, ValueError, base64.binascii.Error) as exc:
                logger.debug("document extraction failed for %s", path, exc_info=True)
                # For binary document formats, surface the specific failure
                # (size cap, encrypted, malformed…) instead of falling through
                # — the fallthrough path can only produce a generic
                # binary-file error or garbage raw bytes, hiding the
                # actionable reason (e.g. "Document too large to convert").
                # .ipynb stays on the fallthrough path: it is plain JSON text
                # and a raw read is genuinely useful.  Byte-transport issues
                # (ValueError / binascii) keep the fallthrough too — only a
                # specific ExtractionError carries an actionable reason.
                _doc_ext = _resolved.suffix.lower()
                _binary_doc = _doc_ext in ANYDOC_EXTENSIONS or (
                    _doc_ext in EXTRACTABLE_EXTENSIONS and _doc_ext != ".ipynb"
                )
                if (
                    _binary_doc
                    and isinstance(exc, ExtractionError)
                    and not str(exc).startswith("Unsupported document type")
                ):
                    return tool_error(
                        f"Cannot read '{path}' ({_doc_ext}): document "
                        f"extraction failed — {exc}. Use terminal utilities "
                        "to inspect or convert the file."
                    )
            else:
                lines = extracted_text.splitlines()
                total_lines = len(lines)
                end_line = offset + limit - 1
                page_text = "\n".join(lines[offset - 1:end_line])
                result_dict = {
                    "content": file_ops._add_line_numbers(page_text, offset) if page_text else "",
                    "total_lines": total_lines,
                    "file_size": binary.file_size,
                    "truncated": total_lines > end_line,
                    "extracted_document": True,
                }
                if result_dict["truncated"]:
                    result_dict["hint"] = (
                        f"Use offset={end_line + 1} to continue reading "
                        f"(showing {offset}-{min(end_line, total_lines)} of {total_lines} lines)"
                    )
                content_len = len(result_dict["content"])
                max_chars = _get_max_read_chars()
                if content_len > max_chars:
                    # Graceful char-budget truncation (nearai/ironclaw#5029):
                    # trim to the last complete line that fits and offer a
                    # next_offset rather than rejecting the whole extraction.
                    trimmed, lines_kept, _ = _truncate_to_char_budget(
                        result_dict["content"], max_chars
                    )
                    next_offset = offset + lines_kept
                    shown_end = offset + lines_kept - 1
                    result_dict["content"] = trimmed
                    result_dict["truncated"] = True
                    result_dict["truncated_by"] = "bytes"
                    result_dict["next_offset"] = next_offset
                    result_dict["hint"] = (
                        f"Output truncated at the {max_chars:,}-char read budget "
                        f"after {lines_kept} line(s) (showing lines {offset}-"
                        f"{shown_end} of {total_lines}). Use offset={next_offset} "
                        "to continue."
                    )
                    if len(trimmed.split("\n", 1)[0]) >= max_chars:
                        result_dict["hint"] += (
                            " Note: the first line alone exceeded the budget and "
                            "was clamped mid-line; its remainder is not "
                            "retrievable via offset."
                        )
                if result_dict["content"]:
                    result_dict["content"] = redact_sensitive_text(result_dict["content"], file_read=True)
                return json.dumps(result_dict, ensure_ascii=False)

        # ── Binary file guard ─────────────────────────────────────────
        # Block binary files by extension (no I/O). Name what we know:
        # the extension is a claim, so keep this branch's message to the
        # extension itself — the content-sniffing path below names the
        # actual magic-byte type for extension-less/lying files.
        if has_binary_extension(str(_resolved)):
            _ext = _resolved.suffix.lower()
            return tool_error(
                f"Cannot read binary file '{path}' ({_ext}). "
                "Use vision_analyze for images, or terminal to inspect binary files."
            )

        # ── Hermes internal path guard ────────────────────────────────
        # Prevent prompt injection via catalog or hub metadata files,
        # and block credential stores under HERMES_HOME.  Pass the
        # already-resolved path so a relative-path read against
        # TERMINAL_CWD == HERMES_HOME (e.g. "auth.json") still hits the
        # denylist — get_read_block_error's own resolve() runs against
        # the Python process cwd, which can differ.
        block_error = get_read_block_error(str(_resolved))
        if block_error:
            return tool_error(block_error)

        # ── Negative-result cache ─────────────────────────────────────
        # If we already discovered this path doesn't exist (within TTL),
        # return the cached error without spawning the subprocess +
        # similar-files walk. Cleared by write_file/patch on the same path.
        resolved_str_for_neg = str(_resolved)
        cached_not_found = _check_not_found_cache("read", resolved_str_for_neg, task_id)
        if cached_not_found is not None:
            return cached_not_found

        # ── Dedup check ───────────────────────────────────────────────
        # If we already read this exact (path, offset, limit) and the
        # file hasn't been modified since, return a lightweight stub
        # instead of re-sending the same content.  Saves context tokens.
        resolved_str = str(_resolved)
        dedup_key = (resolved_str, offset, limit)
        with _read_tracker_lock:
            task_data = _read_tracker.setdefault(task_id, {
                "last_key": None, "consecutive": 0,
                "read_history": set(), "dedup": {},
                "dedup_hits": {}, "dedup_generation_reads": set(),
                "read_timestamps": {},
            })
            # Backward-compat for pre-existing tracker entries that predate
            # dedup_hits/read_timestamps (long-lived task or crossed an
            # upgrade boundary).
            if "dedup_hits" not in task_data:
                task_data["dedup_hits"] = {}
            if "read_timestamps" not in task_data:
                task_data["read_timestamps"] = {}
            generation_reads = task_data.setdefault("dedup_generation_reads", set())
            cached_mtime = task_data.get("dedup", {}).get(dedup_key)
            content_served_in_generation = dedup_key in generation_reads

        if cached_mtime is not None:
            try:
                current_mtime = os.path.getmtime(resolved_str)
                if current_mtime == cached_mtime and content_served_in_generation:
                    # Count repeated stub returns so weak tool-followers that
                    # ignore the "refer to earlier result" hint don't burn
                    # their iteration budget in an infinite read loop.  After
                    # 2 stubs for the same key we escalate to a hard block
                    # mirroring the count>=4 path on real reads.
                    with _read_tracker_lock:
                        hits = task_data["dedup_hits"].get(dedup_key, 0) + 1
                        task_data["dedup_hits"][dedup_key] = hits
                        _cap_read_tracker_data(task_data)

                    if hits >= 2:
                        return tool_error(
                            f"BLOCKED: You have called read_file on this "
                            f"exact region {hits + 1} times and the file "
                            "has NOT changed. STOP calling read_file for "
                            "this path — the content from your earlier "
                            "read_file result in this conversation is "
                            "still current. Proceed with your task using "
                            "the information you already have.",
                            path=path,
                            already_read=hits + 1,
                        )

                    return json.dumps({
                        "status": "unchanged",
                        "message": _READ_DEDUP_STATUS_MESSAGE,
                        "path": path,
                        "dedup": True,
                        "content_returned": False,
                    }, ensure_ascii=False)
            except OSError:
                pass  # stat failed — fall through to full read

        # ── Perform the read ──────────────────────────────────────────
        # Pass the RESOLVED path (str(_resolved)) to FileOperations.read_file
        # so the shell commands inside (wc -c, sed, head) use the fully-
        # qualified absolute path.  Passing the raw *path* here means a
        # relative path is resolved by the shell's cwd, which may differ
        # from the terminal env's tracked cwd — the root cause of the
        # read_file file-not-found spiral (#1044, #886, #970).
        file_ops = _get_file_ops(task_id)
        result = file_ops.read_file(str(_resolved), offset, limit)
        result_dict = result.to_dict()

        # ── Populate negative-result cache on not-found ───────────────
        # _suggest_similar_files returns ReadResult(error="File not found: ..").
        # Cache the JSON we'd return so a retry skips the parent-dir walk.
        # Deliberately NO early return: on upstream, error results flow
        # through the tracking block below (consecutive-loop detection,
        # dedup bookkeeping via the resolved path) and the normal exit —
        # short-circuiting here changes that behavior (and broke a real
        # test interaction). Serving from the cache (above) is the
        # optimization; recording must stay side-effect-identical.
        _err = result_dict.get("error") or ""
        if isinstance(_err, str) and _err.startswith("File not found:"):
            # #2411 — auto-repair: try to find an unambiguous valid path
            # (unicode-equivalent, case-corrected, workspace-root fallback,
            # prefix-stripped, or unique workspace file) before giving up.
            repaired_path, repair_note = _find_auto_repaired_path(
                Path(str(_resolved)), raw_path=path, task_id=task_id
            )
            if repaired_path is not None:
                repaired = file_ops.read_file(str(repaired_path), offset, limit)
                repaired_dict = repaired.to_dict()
                if not repaired_dict.get("error"):
                    existing_hint = repaired_dict.get("hint") or ""
                    repaired_dict["hint"] = (
                        f"{existing_hint} {repair_note}".strip()
                        if existing_hint
                        else repair_note
                    )
                    result = repaired
                    result_dict = repaired_dict
                    _err = ""
            if isinstance(_err, str) and _err.startswith("File not found:"):
                # #2293 — if the shell-based _suggest_similar_files found nothing
                # (no similar_files), fall back to the shared pure-Python module
                # so the agent still gets a nearby-files hint.
                if not result_dict.get("similar_files"):
                    _nearby = suggest_nearby_paths(str(_resolved))
                    _hint = format_nearby_hint(str(_resolved), _nearby)
                    if _hint:
                        result_dict["error"] = _err + "\n\n" + _hint
                _not_found_json = json.dumps(result_dict, ensure_ascii=False)
                _record_not_found("read", resolved_str_for_neg, task_id, _not_found_json)

        if result_dict.get("error"):
            with _read_tracker_lock:
                task_data = _read_tracker.setdefault(task_id, {
                    "consecutive": 0,
                    "last_key": None,
                    "read_history": set(),
                })
                task_data["total_failures"] = task_data.get("total_failures", 0) + 1
                fail_count = task_data["total_failures"]
                if fail_count >= 4:
                    result_dict["_rate_directive"] = (
                        f"read_file has failed {fail_count} times in this session. "
                        "Stop guessing file paths. Use search_files or repo_map to find existing files."
                    )

        # ── Character-count guard ─────────────────────────────────────
        # We're model-agnostic so we can't count tokens; characters are
        # the best proxy we have.  If the read produced an unreasonable
        # amount of content, reject it and tell the model to narrow down.
        # Note: we check the formatted content (with line-number prefixes),
        # not the raw file size, because that's what actually enters context.
        # Check BEFORE redaction to avoid expensive regex on huge content.
        content_len = len(result.content or "")
        file_size = result_dict.get("file_size", 0)
        max_chars = _get_max_read_chars()
        if content_len > max_chars:
            # Graceful char-budget truncation (ported from nearai/ironclaw#5029).
            # Instead of rejecting the whole read — which forces the model to
            # guess a smaller `limit` and wastes a round-trip returning nothing
            # — trim to the last complete line that fits and offer a
            # `next_offset` so the model can paginate forward. This rescues the
            # "few but very long lines" case (logs, wide CSVs, minified data)
            # that sails past the line-count `limit` but blows the char budget.
            total_lines = result_dict.get("total_lines", "unknown")
            trimmed, lines_kept, _ = _truncate_to_char_budget(
                result.content or "", max_chars
            )
            next_offset = offset + lines_kept
            shown_end = offset + lines_kept - 1
            result.content = trimmed
            result_dict["content"] = trimmed
            result_dict["truncated"] = True
            result_dict["truncated_by"] = "bytes"
            result_dict["next_offset"] = next_offset
            result_dict["hint"] = (
                f"Output truncated at the {max_chars:,}-char read budget after "
                f"{lines_kept} line(s) (showing lines {offset}-{shown_end} of "
                f"{total_lines}). Use offset={next_offset} to continue."
            )
            if len(trimmed.split("\n", 1)[0]) >= max_chars:
                result_dict["hint"] += (
                    " Note: the first line alone exceeded the budget and was "
                    "clamped mid-line; its remainder is not retrievable via "
                    "offset."
                )
            content_len = len(trimmed)

        # ── Redact secrets (after guard check to skip oversized content) ──
        if result.content:
            result.content = redact_sensitive_text(result.content, file_read=True)
            result_dict["content"] = result.content

        # Large-file hint: if the file is big and the caller didn't ask
        # for a narrow window, nudge toward targeted reads.
        if (file_size and file_size > _LARGE_FILE_HINT_BYTES
                and limit > 200
                and result_dict.get("truncated")):
            result_dict.setdefault("_hint", (
                f"This file is large ({file_size:,} bytes). "
                "Consider reading only the section you need with offset and limit "
                "to keep context usage efficient."
            ))

        # ── Track for consecutive-loop detection ──────────────────────
        read_key = ("read", path, offset, limit)
        with _read_tracker_lock:
            # Ensure "dedup" / "dedup_hits" keys exist (backward compat with
            # old tracker state from pre-dedup-guard sessions).
            if "dedup" not in task_data:
                task_data["dedup"] = {}
            if "dedup_hits" not in task_data:
                task_data["dedup_hits"] = {}
            # Real read succeeded — this key is no longer in a stub-loop, so
            # reset its hit counter.  (File either changed or stat failed
            # earlier and we fell through.)
            task_data["dedup_hits"].pop(dedup_key, None)
            task_data.setdefault("dedup_generation_reads", set()).add(dedup_key)
            task_data["read_history"].add((path, offset, limit))
            if task_data["last_key"] == read_key:
                task_data["consecutive"] += 1
            else:
                task_data["last_key"] = read_key
                task_data["consecutive"] = 1
            count = task_data["consecutive"]

            # Store mtime at read time for two purposes:
            # 1. Dedup: skip identical re-reads of unchanged files.
            # 2. Staleness: warn on write/patch if the file changed since
            #    the agent last read it (external edit, concurrent agent, etc.).
            try:
                _mtime_now = os.path.getmtime(resolved_str)
                task_data["dedup"][dedup_key] = _mtime_now
                task_data.setdefault("read_timestamps", {})[resolved_str] = _mtime_now
            except OSError:
                pass  # Can't stat — skip tracking for this entry

            # Bound the per-task containers so a long CLI session doesn't
            # accumulate megabytes of dict/set state.  See _cap_read_tracker_data.
            _cap_read_tracker_data(task_data)

        # Cross-agent file-state registry (separate from per-task read
        # tracker above): records that THIS agent has read this path so
        # write/patch can detect sibling-subagent writes that happened
        # after our read.  Partial read when offset>1 or the read was
        # truncated (large file with more content than limit covered).
        # Outside the _read_tracker_lock so the registry's own locking
        # isn't nested under ours.
        _partial = (offset > 1) or bool(result_dict.get("truncated"))
        try:
            file_state.record_read(task_id, resolved_str, partial=_partial)
        except Exception:
            logger.debug("file_state.record_read failed", exc_info=True)

        # Background-review read-before-write guard integration (#61521):
        # when the self-improvement review fork reads a skill file with
        # read_file (now whitelisted dispatch-side), register the read the
        # same way skill_view does, so a follow-up
        # skill_manage(action='patch') on the loaded file is accepted.
        # A partial read doesn't count — the guard requires the CURRENT
        # full content to have been seen. No-op outside review forks
        # (mark_background_review_skill_read gates on is_background_review).
        if not _partial:
            try:
                from tools.skill_manager_tool import mark_background_review_skill_read

                mark_background_review_skill_read(Path(resolved_str))
            except Exception:
                logger.debug(
                    "background-review read-mark failed", exc_info=True
                )

        if count >= 4:
            # Hard block: stop returning content to break the loop
            return tool_error(
                f"BLOCKED: You have read this exact file region {count} times in a row. "
                "The content has NOT changed. You already have this information. "
                "STOP re-reading and proceed with your task.",
                path=path,
                already_read=count,
            )
        elif count >= 3:
            result_dict["_warning"] = (
                f"You have read this exact file region {count} times consecutively. "
                "The content has not changed since your last read. Use the information you already have. "
                "If you are stuck in a loop, stop reading and proceed with writing or responding."
            )

        return json.dumps(result_dict, ensure_ascii=False)
    except Exception as e:
        return tool_error(str(e))




def reset_file_dedup(task_id: str = None):
    """Advance the read-dedup generation after context compression.

    Called after context compression.  The per-key ``dedup`` mtime map is
    preserved, but the generation-read set is cleared. The first unchanged
    read of each key after compaction therefore returns full content that may
    have been summarized away; later reads in the same generation return the
    lightweight stub. Stub-hit counters are also cleared so the hard block
    restarts fresh (issue #84857).

    Call with a task_id to reset just that task, or without to reset all.
    """
    with _read_tracker_lock:
        if task_id:
            task_data = _read_tracker.get(task_id)
            if task_data:
                if "dedup_hits" in task_data:
                    task_data["dedup_hits"].clear()
                task_data.setdefault("dedup_generation_reads", set()).clear()
        else:
            for task_data in _read_tracker.values():
                if "dedup_hits" in task_data:
                    task_data["dedup_hits"].clear()
                task_data.setdefault("dedup_generation_reads", set()).clear()


def notify_other_tool_call(task_id: str = "default"):
    """Reset consecutive read/search counter for a task.

    Called by the tool dispatcher (model_tools.py) whenever a tool OTHER
    than read_file / search_files is executed.  This ensures we only warn
    or block on *truly consecutive* repeated reads — if the agent does
    anything else in between (write, patch, terminal, etc.) the counter
    resets and the next read is treated as fresh.
    """
    with _read_tracker_lock:
        task_data = _read_tracker.get(task_id)
        if task_data:
            task_data["last_key"] = None
            task_data["consecutive"] = 0
            # An intervening non-read tool call breaks any stub-loop in
            # progress, so clear per-key dedup hit counters too.
            if "dedup_hits" in task_data:
                task_data["dedup_hits"].clear()
            # Any other tool (terminal, delegate, ...) may have created a
            # previously-missing path — a cached miss is no longer
            # trustworthy. The serve-side existence guard in
            # _check_not_found_cache already covers this, but clearing
            # here keeps the cache honest and covers exotic cases the
            # stat can't (e.g. permission flips).
            nf = task_data.get("not_found")
            if nf:
                nf.clear()


def _invalidate_dedup_for_path(filepath: str, task_id: str) -> None:
    """Remove all dedup cache entries whose resolved path matches *filepath*.

    Called after write_file and patch so that a subsequent read_file on
    the same path always returns fresh content instead of a stale
    "File unchanged" stub.  The dedup cache keys are tuples of
    ``(resolved_path, offset, limit)``; we must evict **all** offset/limit
    combinations for the written path because any cached range could now
    be stale.

    Must be called with ``_read_tracker_lock`` **not** held — acquires it
    internally.
    """
    try:
        resolved = str(_resolve_path(filepath, task_id))
    except (OSError, ValueError):
        return
    with _read_tracker_lock:
        task_data = _read_tracker.get(task_id)
        if task_data is None:
            return
        dedup = task_data.get("dedup")
        if dedup:
            # Collect keys to remove (can't mutate dict during iteration).
            stale_keys = [k for k in dedup if k[0] == resolved]
            for k in stale_keys:
                del dedup[k]
        # Also evict from the negative-result cache: a write_file that
        # creates the path means subsequent reads (or searches under it)
        # must hit disk.
        nf = task_data.get("not_found")
        if nf:
            nf.pop(("read", resolved), None)
            nf.pop(("search", resolved), None)


def _update_read_timestamp(filepath: str, task_id: str) -> None:
    """Record the file's current modification time after a successful write.

    Called after write_file and patch so that consecutive edits by the
    same task don't trigger false staleness warnings — each write
    refreshes the stored timestamp to match the file's new state.

    Also invalidates the dedup cache for the written path so that
    subsequent reads return fresh content (fixes #13144).
    """
    # Invalidate dedup first (before acquiring lock for timestamp update).
    _invalidate_dedup_for_path(filepath, task_id)
    try:
        resolved = str(_resolve_path_for_task(filepath, task_id))
        current_mtime = os.path.getmtime(resolved)
    except (OSError, ValueError):
        return
    with _read_tracker_lock:
        task_data = _read_tracker.get(task_id)
        if task_data is not None:
            task_data.setdefault("read_timestamps", {})[resolved] = current_mtime
            _cap_read_tracker_data(task_data)


def _check_file_staleness(filepath: str, task_id: str) -> str | None:
    """Check whether a file was modified since the agent last read it.

    Returns a warning string if the file is stale (mtime changed since
    the last read_file call for this task), or None if the file is fresh
    or was never read.  Does not block — the write still proceeds.
    """
    try:
        resolved = str(_resolve_path_for_task(filepath, task_id))
    except (OSError, ValueError):
        return None
    with _read_tracker_lock:
        task_data = _read_tracker.get(task_id)
        if not task_data:
            return None
        read_mtime = task_data.get("read_timestamps", {}).get(resolved)
    if read_mtime is None:
        return None  # File was never read — nothing to compare against
    try:
        current_mtime = os.path.getmtime(resolved)
    except OSError:
        return None  # Can't stat — file may have been deleted, let write handle it
    if current_mtime != read_mtime:
        return (
            f"Warning: {filepath} was modified since you last read it "
            "(external edit or concurrent agent). The content you read may be "
            "stale. Consider re-reading the file to verify before writing."
        )
    return None


def _mark_verification_stale(
    task_id: str,
    resolved_paths: list[str],
    session_id: str | None = None,
) -> None:
    """Best-effort note that successful edits made prior verification stale."""
    paths = [p for p in resolved_paths if p]
    if not paths:
        return
    try:
        from agent.coding_context import project_facts_for
        from agent.verification_evidence import mark_workspace_edited

        cwd = None
        for path in paths:
            try:
                candidate = str(Path(path).parent)
            except Exception:
                continue
            if project_facts_for(candidate):
                cwd = candidate
                break
        if cwd is None:
            cwd = _authoritative_workspace_root(task_id)
        if cwd is None:
            try:
                cwd = str(Path(paths[0]).parent)
            except Exception:
                cwd = None
        mark_workspace_edited(session_id=session_id or task_id, cwd=cwd, paths=paths)
    except Exception:
        logger.debug("verification stale marker failed", exc_info=True)


def _check_binary_document_write(filepath: str, task_id: str = "default") -> str | None:
    """Reject text-tool writes that would corrupt a binary document.

    ``read_file`` auto-extracts .docx/.xlsx/.pptx (and PDF, via anydoc) to
    readable text, so the model plausibly believes it holds the file's
    contents and tries to write the edited text back with write_file/patch.
    A plain-text write can never produce a valid OOXML/OLE/ODF container, so
    that write silently destroys the document (port of nearai/ironclaw#7109).

    Rules:
    - Opaque container formats (.doc/.docx/.xls/.xlsx/.ppt/.pptx/.odt/.ods/
      .odp): always rejected — text bytes are never a valid document, whether
      creating or overwriting.
    - .pdf: rejected only when OVERWRITING an existing regular file. Raw PDF
      syntax is text-authorable, so new-file creation stays allowed.
    """
    if has_opaque_document_extension(filepath):
        ext = filepath[filepath.rfind("."):].lower()
        return (
            f"Refusing to write plain text to binary document '{filepath}' ({ext}). "
            "A text write cannot produce a valid document container and would "
            "corrupt the file (read_file showed you EXTRACTED text, not the real "
            "bytes). Use the docx/xlsx/powerpoint skills or a library like "
            "python-docx/openpyxl/python-pptx via the terminal to create or edit "
            "this document."
        )
    if is_pdf_path(filepath):
        try:
            resolved = Path(_resolve_path_for_task(filepath, task_id))
        except Exception:
            resolved = Path(_expand_tilde(filepath))
        try:
            if resolved.is_file():
                return (
                    f"Refusing to overwrite existing PDF '{filepath}' with plain text. "
                    "read_file showed you EXTRACTED text, not the real bytes — writing "
                    "text back would destroy the document. Use the pdf skill or a PDF "
                    "library via the terminal to modify it. (Creating a NEW .pdf file "
                    "is allowed.)"
                )
        except OSError:
            pass
    return None


def write_file_tool(path: str, content: str, task_id: str = "default",
                    cross_profile: bool = False,
                    session_id: str | None = None) -> str:
    """Write content to a file.

    ``cross_profile`` bypasses the #32049 sandbox-mirror lost-write
    guards (writes the host process would never read). Unadvertised in
    the schema — the mirror rejection error teaches it. The cross-PROFILE
    guard this flag was named for is removed (profiles are not isolated).
    """
    sensitive_err = _check_sensitive_path(path, task_id)
    if sensitive_err:
        return tool_error(sensitive_err)
    binary_doc_err = _check_binary_document_write(path, task_id)
    if binary_doc_err:
        return tool_error(binary_doc_err)
    protected_err = _check_protected_instruction_write([path], task_id)
    if protected_err:
        return tool_error(protected_err)
    approval_err = _check_approval_required_write([path], task_id)
    if approval_err:
        return tool_error(approval_err)
    if not cross_profile:
        cross_warning = _check_cross_profile_path(path, task_id)
        if cross_warning:
            return tool_error(cross_warning)
    if _is_internal_file_tool_content(content):
        return tool_error(
            "Refusing to write internal read_file display text as file content. "
            "Strip read_file line-number prefixes or reconstruct the intended "
            "file contents before writing."
        )
    try:
        # Resolve once for the registry lock + stale check.  Failures here
        # fall back to the legacy path — write proceeds, per-task staleness
        # check below still runs.
        try:
            _resolved = str(_resolve_path_for_task(path, task_id))
        except Exception:
            _resolved = None

        if _resolved is None:
            stale_warning = _check_file_staleness(path, task_id)
            file_ops = _get_file_ops(task_id)
            result = file_ops.write_file(path, content)
            result_dict = result.to_dict()
            if stale_warning:
                result_dict["_warning"] = stale_warning
            if not result_dict.get("error"):
                _mark_verification_stale(task_id, [path], session_id=session_id)
            _update_read_timestamp(path, task_id)
            return json.dumps(result_dict, ensure_ascii=False)

        # Serialize the read→modify→write region per-path so concurrent
        # subagents can't interleave on the same file.  Different paths
        # remain fully parallel.
        with file_state.lock_path(_resolved):
            # Cross-agent staleness wins over per-task warning when both
            # fire — its message names the sibling subagent.
            cross_warning = file_state.check_stale(task_id, _resolved)
            stale_warning = _check_file_staleness(path, task_id)
            # Workspace-divergence warning: relative path resolving outside the
            # terminal's cwd (the worktree-cwd bug). Lowest priority of the three.
            cwd_warning = _path_resolution_warning(path, Path(_resolved), task_id)
            file_ops = _get_file_ops(task_id)
            result = file_ops.write_file(_resolved, content)
            result_dict = result.to_dict()
            effective_warning = cross_warning or stale_warning or cwd_warning
            if effective_warning:
                result_dict["_warning"] = effective_warning
            # Always report the ABSOLUTE path actually written, so a wrong-cwd
            # mismatch is visible in the response instead of silently routing
            # the edit to the wrong checkout.
            result_dict["resolved_path"] = _resolved
            if not result_dict.get("error"):
                result_dict["files_modified"] = [_resolved]
                _mark_verification_stale(task_id, [_resolved], session_id=session_id)
            # Refresh stamps after the successful write so consecutive
            # writes by this task don't trigger false staleness warnings.
            _update_read_timestamp(path, task_id)
            if not result_dict.get("error"):
                file_state.note_write(task_id, _resolved)
        return json.dumps(result_dict, ensure_ascii=False)
    except Exception as e:
        if _is_expected_write_exception(e):
            logger.debug("write_file expected denial: %s: %s", type(e).__name__, e)
        else:
            logger.error("write_file error: %s: %s", type(e).__name__, e, exc_info=True)
        return tool_error(str(e))



def _build_no_match_hint(
    path: str,
    pattern: str,
    target: str,
    resolved_path: "Path | PurePosixPath | None",
) -> "str | None":
    """Build a hint for the agent when search_files finds zero matches.

    Lists what *does* exist in the search directory so the agent can correct
    its pattern or verify the path instead of blind-retrying.  Returns None
    when no useful hint can be constructed (e.g. the directory doesn't exist
    or is empty), so the caller can skip adding an empty field.
    """
    search_dir = resolved_path if resolved_path else Path(path)

    try:
        search_dir = Path(os.path.expanduser(str(search_dir)))
        if not search_dir.is_dir():
            if search_dir.exists() and search_dir.is_file():
                return (
                    f"Pattern {pattern!r} matched nothing in {path!r}. "
                    "The path is a single file — verify the pattern or "
                    "read_file the file directly."
                )
            return None
    except (OSError, ValueError, RuntimeError):
        return None

    try:
        children = sorted(
            search_dir.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
        )
    except (OSError, PermissionError):
        return None

    _max = 10
    dirs = [c.name + "/" for c in children if c.is_dir() and not c.name.startswith(".")]
    files_list = [c.name for c in children if c.is_file() and not c.name.startswith(".")]
    entries = dirs[:_max]
    remaining = _max - len(entries)
    if remaining > 0:
        entries.extend(files_list[:remaining])

    if not entries:
        return (
            f"Pattern {pattern!r} matched nothing in {path!r}. "
            "The directory exists but contains no visible entries. "
            "Verify the path is correct."
        )

    mode_desc = "file search" if target == "files" else "content search"
    listing = ", ".join(entries)
    hint = (
        f"Pattern {pattern!r} matched nothing in {path!r} ({mode_desc}). "
        f"Entries that DO exist in this directory: {listing}"
    )
    if target == "files":
        hint += (
            ". Try a broader glob (e.g. '*.py' instead of a specific name), "
            "or use target='content' to search inside files."
        )
    else:
        hint += (
            ". Try a different regex pattern, or use target='files' with a "
            "glob like '*.py' to list files first."
        )
    return hint


def patch_tool(mode: str = "replace", path: str = None, old_string: str = None,
               new_string: str = None, replace_all: bool = False, patch: str = None,
               task_id: str = "default", cross_profile: bool = False,
               session_id: str | None = None) -> str:
    """Patch a file using replace mode or V4A patch format.

    ``cross_profile``: same semantics as ``write_file``'s flag (mirror-guard
    bypass only; unadvertised).
    """
    # Check sensitive paths for both replace (explicit path) and V4A patch (extract paths)
    _paths_to_check = []
    # Paths whose CONTENT will be text-written (Update/Add + explicit path).
    # V4A Delete/Move don't write text, so they skip the binary-document guard.
    _content_write_paths = []
    if path:
        _paths_to_check.append(path)
        _content_write_paths.append(path)
    if mode == "patch" and patch:
        import re as _re
        from tools.path_security import has_traversal_component
        def _reject_v4a_traversal(v4a_path: str) -> str | None:
            # V4A path headers come from patch CONTENT, not the explicit
            # ``path=`` arg — so they're more attacker-influenceable (skill
            # content, web extract, prompt injection). Reject ``..`` traversal
            # in V4A headers: a legitimate multi-file patch from a single cwd
            # can always emit absolute paths or paths relative to the agent's
            # cwd without ``..``. The explicit ``path=`` arg is unchanged
            # because the agent uses relative ``..`` paths legitimately
            # (e.g. ``patch path="../other_module/x.py"`` from a worktree).
            if has_traversal_component(v4a_path):
                return tool_error(
                    f"V4A patch header contains '..' traversal: {v4a_path!r}. "
                    "Use the agent's cwd-relative path (no '..') or an absolute "
                    "path in '*** Update File:' / '*** Add File:' / "
                    "'*** Delete File:' / '*** Move File:' headers."
                )
            return None

        # ``\s*`` (not ``\s+``) after ``***`` matches patch_parser leniency:
        # it accepts ``***Update File:`` with no space after the asterisks
        # (patch_parser.py uses ``\*\*\*\s*Update\s+File:``). Requiring a space
        # here let a no-space header parse + apply while skipping this check.
        for _m in _re.finditer(r'^\*\*\*\s*(Update|Add|Delete)\s+File:\s*(.+)$', patch, _re.MULTILINE):
            _op = _m.group(1)
            v4a_path = _m.group(2).strip()
            _err = _reject_v4a_traversal(v4a_path)
            if _err:
                return _err
            _paths_to_check.append(v4a_path)
            if _op in ("Update", "Add"):
                _content_write_paths.append(v4a_path)
        # ``*** Move File: src -> dst`` is a valid V4A op (patch_parser.py:114)
        # but was never extracted, so a Move targeting /etc/crontab skipped the
        # sensitive-path pre-check. Check BOTH endpoints, and run them through
        # the same ``..`` traversal rejection as the other headers.
        for _m in _re.finditer(r'^\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+)$', patch, _re.MULTILINE):
            for v4a_path in (_m.group(1).strip(), _m.group(2).strip()):
                _err = _reject_v4a_traversal(v4a_path)
                if _err:
                    return _err
                _paths_to_check.append(v4a_path)
    for _p in _paths_to_check:
        sensitive_err = _check_sensitive_path(_p, task_id)
        if sensitive_err:
            return tool_error(sensitive_err)
        if not cross_profile:
            cross_warning = _check_cross_profile_path(_p, task_id)
            if cross_warning:
                return tool_error(cross_warning)
    for _p in _content_write_paths:
        binary_doc_err = _check_binary_document_write(_p, task_id)
        if binary_doc_err:
            return tool_error(binary_doc_err)
    # One approval prompt for the whole patch: a single protected file gates
    # the ENTIRE patch (deny applies nothing — see the helper's docstring).
    protected_err = _check_protected_instruction_write(_paths_to_check, task_id)
    if protected_err:
        return tool_error(protected_err)
    approval_err = _check_approval_required_write(_paths_to_check, task_id)
    if approval_err:
        return tool_error(approval_err)
    try:
        # Resolve paths for locking.  Ordered + deduplicated so concurrent
        # callers lock in the same order — prevents deadlock on overlapping
        # multi-file V4A patches.
        _resolved_paths: list[str] = []
        _seen: set[str] = set()
        for _p in _paths_to_check:
            try:
                _r = str(_resolve_path_for_task(_p, task_id))
            except Exception:
                _r = None
            if _r and _r not in _seen:
                _resolved_paths.append(_r)
                _seen.add(_r)
        _resolved_paths.sort()

        # Acquire per-path locks in sorted order via ExitStack.  On single
        # path this degenerates to one lock; on empty list (unresolvable)
        # it's a no-op and execution falls through unchanged.
        from contextlib import ExitStack
        with ExitStack() as _locks:
            for _r in _resolved_paths:
                _locks.enter_context(file_state.lock_path(_r))

            # Collect warnings — cross-agent registry first (names sibling),
            # then per-task tracker as a fallback.
            stale_warnings: list[str] = []
            _path_to_resolved: dict[str, str] = {}
            for _p in _paths_to_check:
                try:
                    _r = str(_resolve_path_for_task(_p, task_id))
                except Exception:
                    _r = None
                _path_to_resolved[_p] = _r
                _cross = file_state.check_stale(task_id, _r) if _r else None
                _sw = _cross or _check_file_staleness(_p, task_id)
                if not _sw and _r:
                    # Workspace-divergence warning (worktree-cwd bug): relative
                    # path resolving outside the terminal's cwd.
                    _sw = _path_resolution_warning(_p, Path(_r), task_id)
                if _sw:
                    stale_warnings.append(_sw)

            file_ops = _get_file_ops(task_id)

            if mode == "replace":
                if not path:
                    return tool_error("path required")
                # #1703/#3238 — Preflight: reject empty old_string before
                # attempting the patch. Return a structured re_read_file
                # instruction so the model fixes the shape instead of blind-retrying.
                _old = (old_string or "").strip()
                if not _old:
                    return _patch_preflight_blocked_structured(
                        path, "empty old_string", task_id
                    )
                _reset_empty_old_string(task_id, path)
                if new_string is None:
                    return tool_error("old_string and new_string required")
                # Pass the resolved ABSOLUTE path to the shell layer so it
                # operates on the exact file the tool layer resolved — the
                # shell's own cwd may differ (worktree-cwd bug), and a relative
                # path would let the two layers disagree about which file is
                # being edited.
                _replace_target = _path_to_resolved.get(path) or path
                result = file_ops.patch_replace(_replace_target, old_string, new_string, replace_all)
            elif mode == "patch":
                if not patch:
                    return tool_error("patch content required")
                # Rewrite V4A headers to the resolved absolute paths so the
                # shell layer patches the exact files the tool layer resolved
                # (locked/reported). Without this a relative header re-resolves
                # against the shell's cwd, which can differ from the workspace
                # (git-worktree cwd bug) — landing the edit elsewhere.
                patch_for_ops = _rewrite_v4a_patch_paths_for_host(
                    patch, _path_to_resolved, file_ops
                )
                result = file_ops.patch_v4a(patch_for_ops)
            else:
                return tool_error(f"Unknown mode: {mode}")

            result_dict = result.to_dict()
            # #2242 Slice B — when the patch targets a non-existent file,
            # surface nearby paths (mirrors read_file #2293 / search_files).
            # Covers both replace-mode ("File not found:") and V4A-mode
            # ("file not found") errors; suppressed when the impl already
            # attached similar_files (shell-based suggestion found something).
            _patch_nf_err = result_dict.get("error") or ""
            if (
                isinstance(_patch_nf_err, str)
                and "not found" in _patch_nf_err.lower()
                and not result_dict.get("similar_files")
            ):
                _nf_path = path or ""
                if mode != "replace" and _paths_to_check:
                    _nf_path = _paths_to_check[0]
                _nf_resolved = _path_to_resolved.get(_nf_path) or _nf_path or ""
                if _nf_resolved:
                    _nearby = suggest_nearby_paths(_nf_resolved)
                    _hint = format_nearby_hint(_nf_resolved, _nearby)
                    if _hint:
                        result_dict["error"] = _patch_nf_err + "\n\n" + _hint
            if stale_warnings:
                result_dict["_warning"] = stale_warnings[0] if len(stale_warnings) == 1 else " | ".join(stale_warnings)
            # Report the ABSOLUTE path(s) actually patched so a wrong-cwd
            # mismatch (e.g. a worktree session editing the main checkout) is
            # visible in the response instead of silently landing elsewhere.
            _resolved_modified = [
                _path_to_resolved.get(_p) or _p for _p in _paths_to_check
            ]
            # Refresh stored timestamps for all successfully-patched paths so
            # consecutive edits by this task don't trigger false warnings.
            if not result_dict.get("error"):
                result_dict["files_modified"] = _resolved_modified
                if len(_resolved_modified) == 1:
                    result_dict["resolved_path"] = _resolved_modified[0]
                _mark_verification_stale(task_id, _resolved_modified, session_id=session_id)
                for _p in _paths_to_check:
                    _update_read_timestamp(_p, task_id)
                    _r = _path_to_resolved.get(_p)
                    if _r:
                        file_state.note_write(task_id, _r)
                # Successful patch: clear any prior consecutive-failure
                # counters for the touched paths so a future failure on
                # the same path starts the escalation cycle fresh.
                _reset_patch_failures(task_id, [
                    _r for _r in (_path_to_resolved.get(_p) for _p in _paths_to_check) if _r
                ])
        # Hint when old_string not found — saves iterations where the agent
        # retries with stale content instead of re-reading the file.
        # Suppressed when patch_replace already attached a rich "Did you mean?"
        # snippet (which is strictly more useful than the generic hint).
        if result_dict.get("error"):
            # Track per-file consecutive failures for replace mode.
            failure_count = 0
            if mode == "replace" and path:
                resolved = _path_to_resolved.get(path) or path
                failure_count = _record_patch_failure(task_id, resolved)

            if failure_count > 3:
                # 4th failure onwards: Hard stop / PATCH REFUSED (#1037)
                from tools.fuzzy_match import suggest_closest_match
                content = ""
                try:
                    file_ops = _get_file_ops(task_id)
                    read_res = file_ops.read_file_raw(str(resolved))
                    content = read_res.content or ""
                except Exception:
                    pass
                closest = suggest_closest_match(old_string, content) if (content and old_string) else ""
                refusal_msg = f"PATCH REFUSED: 3 consecutive patch attempts failed on {path}."
                if closest:
                    refusal_msg += f" Closest matching content in file:\n{closest}"
                refusal_msg += " Use read_file to view the current file content, or write_file to overwrite."
                result_dict["error"] = refusal_msg
                result_dict["_hint"] = "PATCH REFUSED. Stop retrying; switch to write_file or re-read the file."
            elif failure_count == 3:
                # 3rd consecutive failure: PERMANENT FAILURE escalation (#507)
                result_dict["_hint"] = (
                    f"This is failure #3 (PERMANENT FAILURE) patching {path!r}. "
                    "Stop retrying with variations of the same old_string. "
                    "Either: (1) re-read the file fresh to verify current content, "
                    "(2) use a longer / more unique old_string with surrounding context lines, "
                    "or (3) use write_file to replace the entire file if the targeted region is hard to anchor."
                )
            elif failure_count == 2:
                # 2nd consecutive failure: softer write_file nudge (#1537)
                result_dict["_hint"] = (
                    f"This is failure #2 patching {path!r}. "
                    "Consider switching to write_file if the exact snippet cannot be located."
                )
            elif "Did you mean one of these sections?" not in str(result_dict.get("error", "")) and "Could not find" in str(result_dict.get("error", "")):
                result_dict["_hint"] = (
                    "old_string not found. Use read_file to verify the current "
                    "content, or search_files to locate the text."
                )
        return json.dumps(result_dict, ensure_ascii=False)
    except Exception as e:
        return tool_error(str(e))


def search_tool(pattern: str, target: str = "content", path: str = ".",
                file_glob: str = None, limit: int = 50, offset: int = 0,
                output_mode: str = "content", context: int = 0,
                order: str = "discovery",
                task_id: str = "default") -> str:
    """Search for content or files."""
    try:
        offset, limit = normalize_search_pagination(offset, limit)

        # Track searches to detect *consecutive* repeated search loops.
        # Include pagination args so users can page through truncated
        # results without tripping the repeated-search guard.
        search_key = (
            "search",
            pattern,
            target,
            str(path),
            file_glob or "",
            limit,
            offset,
            order,
        )
        with _read_tracker_lock:
            task_data = _read_tracker.setdefault(task_id, {
                "last_key": None, "consecutive": 0, "read_history": set(),
            })
            if task_data["last_key"] == search_key:
                task_data["consecutive"] += 1
            else:
                task_data["last_key"] = search_key
                task_data["consecutive"] = 1
            count = task_data["consecutive"]

        if count >= 4:
            return tool_error(
                f"BLOCKED: You have run this exact search {count} times in a row. "
                "The results have NOT changed. You already have this information. "
                "STOP re-searching and proceed with your task.",
                pattern=pattern,
                already_searched=count,
            )

        try:
            resolved_path = _resolve_path_for_task(path, task_id)
        except (OSError, ValueError, RuntimeError):
            resolved_path = None
        block_error = get_read_block_error(str(resolved_path) if resolved_path else path)
        if block_error:
            return tool_error(block_error)

        # ── Negative-result cache ─────────────────────────────────────
        # Search returns "Path not found: <path>" when the search root
        # doesn't exist. The error path also lists the parent directory
        # (file_operations.py:1402) — expensive to repeat. Cache so the
        # next call to a known-missing root skips both shells.
        try:
            resolved_search_path = str(_resolve_path_for_task(path, task_id))
        except (OSError, ValueError):
            resolved_search_path = path
        cached_search_nf = _check_not_found_cache("search", resolved_search_path, task_id)
        if cached_search_nf is not None:
            return cached_search_nf

        file_ops = _get_file_ops(task_id)
        result = file_ops.search(
            pattern=pattern, path=path, target=target, file_glob=file_glob,
            limit=limit, offset=offset, output_mode=output_mode, context=context,
            order=order,
        )
        omitted = _filter_read_blocked_search_results(result, task_id)
        if hasattr(result, 'matches'):
            for m in result.matches:
                if hasattr(m, 'content') and m.content:
                    m.content = redact_sensitive_text(m.content, file_read=True)
        result_dict = result.to_dict(densify=True)

        if omitted:
            result_dict["_omitted"] = (
                f"{omitted} result(s) omitted because they target credential, "
                "token, cache, or secret-bearing environment files."
            )

        # Populate negative cache when search root was missing. No early
        # return — same rationale as the read path: error results keep
        # flowing through the consecutive-search bookkeeping below.
        _search_err = result_dict.get("error") or ""
        if isinstance(_search_err, str) and _search_err.startswith("Path not found:"):
            # #2242 Slice B — mirror read_file's nearby-hint fallback: when
            # the search root doesn't exist, surface nearby paths so the
            # agent can correct itself instead of blind-retrying. The hint
            # is APPENDED (not replacing the error) so the "Path not found:"
            # prefix the negative cache keys on stays intact.
            _nearby = suggest_nearby_paths(resolved_search_path)
            _hint = format_nearby_hint(resolved_search_path, _nearby)
            if _hint:
                result_dict["error"] = _search_err + "\n\n" + _hint
            _search_nf_json = json.dumps(result_dict, ensure_ascii=False)
            _record_not_found("search", resolved_search_path, task_id, _search_nf_json)

        if count >= 3:
            result_dict["_warning"] = (
                f"You have run this exact search {count} times consecutively. "
                "The results have not changed. Use the information you already have."
            )

        #1486 — when the pattern matches nothing the agent has no hint about
        # what *does* exist, so it blind-retries or falls back to a terminal
        # directory listing, burning extra turns (95 file-not-found events/7d,
        # 27-deep spirals observed). The FIRST empty result is enriched with
        # what actually exists in the search path.
        #
        # #1372/#1149 — empty results are tracked per session; at 3 an
        # advisory directive is injected so the agent switches strategy.
        # Council 2026-08-31: a well-formed search with zero hits is NOT a
        # failure — do not escalate to an ``error`` key (that made
        # spiral_failure_cap abort legitimate exploration). Advisory only.
        if result_dict.get("total_count", 0) == 0 and not result_dict.get("error"):
            with _read_tracker_lock:
                td = _read_tracker.setdefault(
                    task_id,
                    {"last_key": None, "consecutive": 0, "read_history": set()},
                )
                td["empty_searches"] = td.get("empty_searches", 0) + 1
                _es = td["empty_searches"]

            # ── #1486: immediate no-match hint on the FIRST empty result ─
            # List what *does* exist in the search directory so the agent can
            # correct the pattern instead of blind-retrying.  Runs before the
            # spiral directive so even the first miss gets actionable info.
            if _es == 1:
                _hint = _build_no_match_hint(path, pattern, target, resolved_path)
                if _hint:
                    result_dict["_no_match_hint"] = _hint

            if _es >= 3:
                result_dict.setdefault(
                    "_search_directive",
                    (
                        f"search_files has returned 0 results {_es} times. "
                        "Your queries are not matching anything. SWITCH STRATEGY: "
                        "(a) use search_files target='files' with a glob like '*.py', "
                        "(b) call repo_map for a structural overview, or "
                        "(c) read_file on a known path instead of searching."
                    ),
                )
        elif result_dict.get("total_count", 0) > 0:
            # Successful search with results — reset the empty counter.
            with _read_tracker_lock:
                td = _read_tracker.get(task_id)
                if td is not None:
                    td["empty_searches"] = 0

        result_json = json.dumps(result_dict, ensure_ascii=False)
        # Hint when results were truncated — explicit next offset is clearer
        # than relying on the model to infer it from total_count vs match count.
        if result_dict.get("truncated"):
            next_offset = offset + limit
            result_json += f"\n\n[Hint: Results truncated. Use offset={next_offset} to see more, or narrow with a more specific pattern or file_glob.]"
        return result_json
    except Exception as e:
        return tool_error(str(e))




# ---------------------------------------------------------------------------
# Schemas + Registry
# ---------------------------------------------------------------------------
from tools.registry import registry, tool_error


def _check_file_reqs():
    """Lazy wrapper to avoid circular import with tools/__init__.py."""
    from tools import check_file_requirements
    return check_file_requirements()

READ_FILE_SCHEMA = {
    "name": "read_file",
    # Document formats are stated unconditionally: firecrawl-anydoc is a
    # core dependency (bundled), so its absence is a broken install, not a
    # configuration — the teaching error in read_extract handles that rare
    # case with the pip-install fix. The ONE dynamic word: "PDF (text
    # layer)" upgrades to "PDF (scanned or text)" when hosted OCR has a
    # route we trust (_read_file_schema_overrides). Scanned-page coverage
    # teaching lives in the response-time NEEDS-OCR warning
    # (read_extract.py); the schema doesn't pre-teach it.
    "description": "Read a text file with line numbers and pagination. Use this instead of cat/head/tail in terminal. Output format: 'LINE_NUM|CONTENT'. Suggests similar filenames if not found. Use offset and limit for large files. Reads exceeding ~100K characters are truncated on a line boundary and return a next_offset; continue with offset to read the rest. Documents auto-extract to readable text: .ipynb, Office (.docx/.xlsx/.pptx and legacy .doc/.ppt/.xls), PDF (text layer), OpenDocument, RTF, EPUB. Cannot read images/binary — use vision_analyze for images.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": ["string", "array"],
                "items": {"type": "string"},
                "description": "Path to the file to read (absolute, relative, or ~/path), or a list of up to 10 paths for batch reading",
            },
            "offset": {"type": "integer", "description": "Line number to start reading from (1-indexed, default: 1)", "default": 1, "minimum": 1},
            "limit": {"type": "integer", "description": "Maximum number of lines to read (default: 2000, max: 2000). Reads are additionally capped at a ~100K-character budget with a next_offset continuation.", "default": 2000, "maximum": 2000}
        },
        "required": ["path"]
    }
}

WRITE_FILE_SCHEMA = {
    "name": "write_file",
    "description": "Write content to a file, completely replacing existing content. Use this instead of echo/cat heredoc in terminal. Creates parent directories automatically. OVERWRITES the entire file — use 'patch' for targeted edits. Auto-runs syntax checks on .py/.json/.yaml/.toml and other linted languages; only NEW errors introduced by this write are surfaced (pre-existing errors are filtered out). The result's verified:true means the on-disk content hash was confirmed — do NOT re-read the file to check the write landed.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to write (will be created if it doesn't exist, overwritten if it does)"},
            "content": {"type": "string", "description": "Complete content to write to the file"},
            # NOTE: the handler still accepts `cross_profile` (bool) — it now
            # bypasses only the #32049 sandbox-mirror lost-write guards, whose
            # rejection error teaches it. Unadvertised: the cross-PROFILE
            # guard it was named for was removed (profiles are not isolated,
            # maintainer decision), and mirror hits are rare + self-teaching.
        },
        "required": ["path", "content"]
    }
}

PATCH_SCHEMA = {
    "name": "patch",
    # BASE = replace-only (what nearly every model family was trained on).
    # The V4A patch mode (mode + patch params, dual-mode description) is
    # LAYERED ON dynamically for OpenAI-family mains only — V4A is the
    # OpenAI apply_patch dialect their models emit natively; advertising
    # it to everyone cost every other session ~148 tok/call
    # (_patch_schema_overrides below). The handler accepts BOTH shapes
    # from any model regardless (replay compat + strong models that know
    # V4A anyway): mode defaults to 'replace' when omitted.
    "description": (
        "Targeted find-and-replace edits in files. Use this instead of sed/awk in terminal. "
        "Uses fuzzy matching (9 strategies) so minor whitespace/indentation differences won't break it. "
        "Returns a unified diff. Auto-runs syntax checks after editing. "
        "Finds a unique string and replaces it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path to edit.",
            },
            "old_string": {
                "type": "string",
                "description": "Exact text to find and replace. Must be unique in the file unless replace_all=true. Include surrounding context lines to ensure uniqueness.",
            },
            "new_string": {
                "type": "string",
                "description": "Changed replacement text; it must differ from old_string. Pass empty string '' to delete the matched text.",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace all occurrences instead of requiring a unique match (default: false)",
                "default": False,
            },
            # NOTE: handler still accepts `cross_profile` — see write_file's
            # NOTE (mirror-guard bypass only; unadvertised by design).
            # NOTE: handler still accepts `mode` + `patch` (V4A) from ANY
            # model — the schema just doesn't advertise them off-family.
        },
        "required": ["path", "old_string", "new_string"],
    },
}


# V4A layer, rendered only for OpenAI-family main models (see PATCH_SCHEMA
# comment). Kept as data so the override composes it deterministically.
_PATCH_V4A_DESCRIPTION = (
    "Targeted find-and-replace edits in files. Use this instead of sed/awk in terminal. "
    "Uses fuzzy matching (9 strategies) so minor whitespace/indentation differences won't break it. "
    "Returns a unified diff. Auto-runs syntax checks after editing.\n\n"
    "REPLACE MODE (mode='replace', default): find a unique string and replace it. "
    "REQUIRED PARAMETERS: mode, path, old_string, new_string.\n"
    "PATCH MODE (mode='patch'): apply V4A multi-file patches for bulk changes. "
    "REQUIRED PARAMETERS: mode, patch."
)

_PATCH_V4A_PARAMS = {
    "mode": {
        "type": "string",
        "enum": ["replace", "patch"],
        "description": "Edit mode. 'replace' (default): requires path + old_string + new_string. 'patch': requires patch content only.",
        "default": "replace",
    },
    "patch": {
        "type": "string",
        "description": "REQUIRED when mode='patch'. V4A format patch content. Format:\n*** Begin Patch\n*** Update File: path/to/file\n@@ context hint @@\n context line\n-removed line\n+added line\n*** End Patch",
    },
}


def _is_openai_family_main() -> bool:
    """Whether the active main provider/model is the OpenAI/codex family —
    the population trained on the V4A apply_patch dialect.

    Provider-family-coarse on purpose (no per-model training-diet table to
    go stale): direct OpenAI providers always qualify; on aggregators
    (openrouter/nous/azure...) the MODEL slug decides (gpt-*/o-series/
    codex). Fail-closed to the universal replace-only schema.
    """
    try:
        from agent.auxiliary_client import _read_main_model, _read_main_provider

        provider = (_read_main_provider() or "").strip().lower()
        model = (_read_main_model() or "").strip().lower()
    except Exception:  # noqa: BLE001
        return False
    if provider in {"openai", "openai-chat", "openai-codex", "azure-openai", "codex"}:
        return True
    # Aggregators: the model slug carries the family.
    slug = model.split("/", 1)[-1]
    if slug.startswith(("gpt-", "gpt.", "chatgpt", "codex", "o1", "o3", "o4", "o5")):
        return True
    return "openai/" in model


SEARCH_FILES_SCHEMA = {
    "name": "search_files",
    "description": "Search file contents or find files by name. Use this instead of grep/rg/find/ls in terminal. Ripgrep-backed, faster than shell equivalents. On macOS, broad searches above the user home automatically skip TCC-protected folders (Desktop, Documents, Downloads, Library, Movies, Music, Pictures); target one directly when access is intentional.\n\nContent search (target='content'): Regex search inside files. Output modes: full matches with line numbers, file paths only, or match counts.\n\nFile search (target='files'): Find files by glob pattern (e.g., '*.py', '*config*'). Also use this instead of ls. Discovery order is the fast bounded default; exact global newest-first order is an explicit opt-in and may scan the full tree.",
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern for content search, or glob pattern (e.g., '*.py') for file search"},
            "target": {"type": "string", "enum": ["content", "files"], "description": "'content' searches inside file contents, 'files' searches for files by name", "default": "content"},
            "path": {"type": "string", "description": "Directory or file to search in (default: current working directory)", "default": "."},
            "file_glob": {"type": "string", "description": "Filter files by pattern in grep mode (e.g., '*.py' to only search Python files)"},
            "limit": {"type": "integer", "description": "Maximum number of results to return (default: 50)", "default": 50},
            "offset": {"type": "integer", "description": "Skip first N results for pagination (default: 0)", "default": 0},
            "order": {"type": "string", "enum": ["discovery", "modified"], "description": "File-search order: 'discovery' is fast bounded traversal order; 'modified' is exact global newest-first and may scan the full tree; ignored for content", "default": "discovery"},
            "output_mode": {"type": "string", "enum": ["content", "files_only", "count"], "description": "Output format for grep mode: 'content' shows matching lines with line numbers, 'files_only' lists file paths, 'count' shows match counts per file", "default": "content"},
            "context": {"type": "integer", "description": "Number of context lines before and after each match (grep mode only)", "default": 0}
        },
        "required": ["pattern"]
    }
}


def _handle_read_file(args, **kw):
    tid = kw.get("task_id") or "default"
    path = args.get("path", "")
    # #757/#784 — batch mode: read multiple files in one tool call.
    if isinstance(path, list):
        return _handle_read_file_batch(path, args, tid)
    return read_file_tool(path=path, offset=args.get("offset", 1), limit=args.get("limit", 500), task_id=tid)


_BATCH_READ_MAX_FILES = 10


def _handle_read_file_batch(paths: list, args: dict, tid: str) -> str:
    """Read multiple files in one call. Returns JSON with per-file results."""
    if len(paths) > _BATCH_READ_MAX_FILES:
        return json.dumps({
            "error": (
                f"Batch read supports at most {_BATCH_READ_MAX_FILES} files per call; "
                f"got {len(paths)}. Split into smaller batches."
            ),
        })
    offset = args.get("offset", 1)
    limit = args.get("limit", 500)
    files = []
    for p in paths:
        if not isinstance(p, str):
            files.append({"path": str(p), "error": "Invalid path type: expected string"})
            continue
        raw = read_file_tool(path=p, offset=offset, limit=limit, task_id=tid)
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            parsed = {"raw": raw}
        entry = {"path": p}
        if "error" in parsed:
            entry["error"] = parsed["error"]
        else:
            entry.update(parsed)
        files.append(entry)
    return json.dumps({"batch": True, "files": files}, ensure_ascii=False)


def _handle_write_file(args, **kw):
    tid = kw.get("task_id") or "default"
    if not args.get("path") or not isinstance(args.get("path"), str):
        return tool_error(
            "write_file: missing required field 'path'. Re-emit the tool call with "
            "both 'path' and 'content' set."
        )
    if "content" not in args:
        return tool_error(
            "write_file: missing required field 'content'. The tool call included a "
            "path but no content argument — this is almost always a dropped-arg bug "
            "under context pressure. Re-emit the tool call with the full content "
            "payload, or use execute_code with hermes_tools.write_file() for very "
            "large files."
        )
    if not isinstance(args["content"], str):
        return tool_error(
            f"write_file: 'content' must be a string, got "
            f"{type(args['content']).__name__}."
        )
    return write_file_tool(
        path=args["path"], content=args["content"], task_id=tid,
        cross_profile=bool(args.get("cross_profile", False)),
        session_id=kw.get("session_id"),
    )


def _handle_patch(args, **kw):
    tid = kw.get("task_id") or "default"
    return patch_tool(
        mode=args.get("mode", "replace"), path=args.get("path"),
        old_string=args.get("old_string"), new_string=args.get("new_string"),
        replace_all=args.get("replace_all", False), patch=args.get("patch"), task_id=tid,
        cross_profile=bool(args.get("cross_profile", False)),
        session_id=kw.get("session_id"),
    )


def _looks_like_glob(pattern: str) -> bool:
    """Heuristic: does *pattern* look like a shell glob rather than a regex?

    Globs use ``*``, ``?``, and ``[...]`` as wildcards without regex escaping.
    In a regex those same characters are metacharacters, but a bare ``*.py`` or
    ``*config*`` is almost certainly a glob the model intended as a filename
    pattern, not a regex. We flag it so the caller can auto-redirect.
    """
    if not pattern:
        return False
    # A real regex would escape these as \*, \?, \[ — so an unescaped
    # wildcard is the signal.  ``**/`` (recursive glob) is also glob-only.
    # Exception: ``.*`` and ``.+`` etc. are common regex idioms where the
    # ``*``/``+`` quantifier follows a regex metacharacter — those should
    # NOT be flagged as globs.  The distinguishing signal: in a glob, ``*``
    # is typically preceded by a literal character (``*.py``, ``*config*``)
    # or a ``/`` (``**/*.py``), not by a regex metacharacter like ``.``.
    for i, ch in enumerate(pattern):
        if ch == "*":
            if i > 0 and pattern[i - 1] == "\\":
                continue  # escaped — regex literal
            if i > 0 and pattern[i - 1] in ".+?^$":
                # ``.*``, ``+*`` (unusual but not a glob) — ``*`` is a
                # regex quantifier following a metacharacter, not a glob.
                # But ``?*`` is ambiguous — treat as regex here since
                # ``?*`` as a glob is extremely rare.
                continue
            if i > 0 and pattern[i - 1] == "*" and i >= 2 and pattern[i - 2] == "*":
                # ``**`` (recursive glob) — ``**/`` is glob-only
                if i + 1 < len(pattern) and pattern[i + 1] == "/":
                    return True
                continue
            return True
        if ch == "?":
            if i > 0 and pattern[i - 1] == "\\":
                continue
            # ``?`` in regex means "zero or one" — preceded by a
            # metacharacter it's a quantifier, not a glob wildcard.
            # ``(`` is included so lookarounds ``(?!…)``, ``(?<=…)``,
            # ``(?:…)`` and other ``(?…)`` groups are NOT misclassified
            # as glob ``?`` wildcards (#1484 — caused 59 retries/7d).
            if i > 0 and pattern[i - 1] in ".+*^$[(":
                continue
            return True
    return False


def _is_valid_regex(pattern: str) -> bool:
    """Return True if *pattern* compiles as a valid Python regex.

    Used to short-circuit the glob-vs-regex heuristic in
    :func:`_handle_search_files`: the guard's only purpose (#887) is to catch
    patterns that would cause a ripgrep *regex parse error*, and a pattern that
    compiles cannot cause one.  Treating such a pattern as a glob is a
    false-positive redirect (e.g. ``"verdict":\\s*null`` — the heuristic sees
    ``*`` preceded by ``s`` because it does not track the ``\\s`` escape span,
    yet the pattern compiles and is exactly what the caller wanted to search for).
    """
    try:
        re.compile(pattern)
        return True
    except re.error:
        return False


def _glob_to_regex(glob: str) -> str:
    """Convert a shell glob pattern to an equivalent regex (#1788).

    Translates ``*`` → ``.*``, ``?`` → ``.``, and passes ``[...]`` character
    classes through (they share syntax between glob and regex).  All other
    regex metacharacters (``.``, ``+``, ``(``, ``)``, ``{``, ``}``, ``|``,
    ``^``, ``$``, ``\\``) are escaped so the result is a literal-matching
    regex.  The output is **not anchored** — callers match substrings.
    """
    _REGEX_META = set(".^$+{}\\|()")
    i = 0
    n = len(glob)
    out: list[str] = []
    while i < n:
        c = glob[i]
        if c == "*":
            out.append(".*")
        elif c == "?":
            out.append(".")
        elif c == "[":
            j = i + 1
            if j < n and glob[j] == "!":
                j += 1
            if j < n and glob[j] == "]":
                j += 1
            while j < n and glob[j] != "]":
                j += 1
            if j < n:
                out.append(glob[i : j + 1])
                i = j
            else:
                out.append("\\[")
        elif c in _REGEX_META:
            out.append("\\" + c)
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _classify_regex_error(pattern: str, exc) -> tuple:
    """#2308 — decompose a regex compile failure into a sub-cause + recovery.

    The pre-validation in ``_handle_search_files`` catches patterns that fail
    ``re.compile`` before ripgrep ever sees them, but the returned error was
    a single generic bucket with no sub-cause and no recovery directive —
    so the agent blind-retried with near-identical patterns (74/7d, 17-deep
    spirals). This classifies the ``re.error`` message into a structured
    reason with a corrected-pattern suggestion.

    Returns ``(reason, recovery)`` where ``reason`` is one of:
      - ``invalid_regex_syntax`` — malformed regex (unclosed bracket/group,
        bad escape, dangling quantifier)
      - ``glob_as_regex`` — a shell glob that slipped past the auto-convert
        guard (e.g. a ``[!...]`` negation or a pattern the heuristic missed)
      - ``unsupported_feature`` — a regex feature ripgrep/Python rejects
      - ``other`` — unclassified compile failure
    """
    if exc is None:
        return (
            "other",
            "The regex failed to compile. Read the error text, fix the "
            "pattern, and re-run — do NOT retry the same pattern unchanged.",
        )
    low = str(exc).lower()
    # #2308 — glob_as_regex is checked BEFORE invalid_regex_syntax because
    # it is more specific: a pattern containing ``[!`` (glob negation
    # syntax, which is invalid in Python regex) is almost certainly a
    # shell glob the model intended as a filename pattern. We key on the
    # pattern-level signal ``[!`` rather than the error message text,
    # because "unterminated character set" fires for ANY unclosed ``[``,
    # including plain malformed regex like ``[unclosed`` that has no glob
    # intent.
    if "[!" in pattern:
        return (
            "glob_as_regex",
            "This looks like a shell glob (filename pattern) passed as a "
            "regex — the '[!' glob negation syntax is not valid regex. Use "
            "target='files' or move it to the file_glob parameter instead of "
            "the regex pattern.",
        )
    # Unclosed character class / group / dangling quantifier — the classic
    # malformed-regex family.
    if any(tok in low for tok in (
        "unterminated", "unclosed", "missing ), unterminated subpattern",
        "nothing to repeat", "multiple repeat", "unexpected end of pattern",
        "bad escape", "invalid escape", "trailing backslash",
    )):
        return (
            "invalid_regex_syntax",
            "The regex is malformed (unclosed bracket/group, bad escape, or "
            "dangling quantifier). Fix the syntax — e.g. close the '[' or '(' "
            "and escape literal metacharacters with '\\'. Do NOT retry the "
            "same malformed pattern.",
        )
    # Lookbehind/lookahead or other engine-specific feature rejection.
    if any(tok in low for tok in (
        "look-behind", "lookbehind", "fixed-width", "variable-length",
        "not supported", "unsupported", "invalid group",
    )):
        return (
            "unsupported_feature",
            "The regex uses a feature the search engine does not support "
            "(e.g. variable-width lookbehind). Rewrite it with a supported "
            "construct — do NOT retry the same pattern.",
        )
    return (
        "other",
        "The regex failed to compile for an unclassified reason. Read the "
        "error text, fix the pattern, and re-run — do NOT retry the same "
        "pattern unchanged.",
    )


def _handle_search_files(args, **kw):
    tid = kw.get("task_id") or "default"
    target_map = {"grep": "content", "find": "files"}
    raw_target = args.get("target", "content")
    target = target_map.get(raw_target, raw_target)
    pattern = args.get("pattern", "")
    # Issue #887 / #1788: when the model passes a glob pattern (e.g. ``*.py``)
    # as the regex ``pattern`` in content-search mode, ripgrep fails with a
    # regex parse error.  Instead of returning an error and forcing a retry
    # (#1788 — 227 failures / 300 sessions, 70% glob-as-regex), we now
    # transparently convert the glob to an equivalent regex and proceed with
    # the search.  This prevents the error entirely and saves a round-trip.
    #
    # The conversion is ONLY applied when the pattern would actually fail
    # ``re.compile`` — a pattern that compiles is a legitimate regex, even if
    # it contains glob-like metacharacters (see _is_valid_regex above for the
    # false-positive rationale).
    if (
        target == "content"
        and not args.get("file_glob")
        and _looks_like_glob(pattern)
        and not _is_valid_regex(pattern)
    ):
        pattern = _glob_to_regex(pattern)

    # #1588 — when a pattern that does NOT look like a glob still fails to
    # compile as a regex, ripgrep returns a bare parse error with no guidance,
    # causing 59/week parse-error spirals. Pre-validate and surface the exact
    # compile-failure reason plus a glob-vs-regex hint so the agent can fix
    # the pattern instead of blind-retrying with a near-identical one.
    # Guard ``file_glob``: when it is set, the caller is intentionally
    # combining a filename filter with their pattern, so the pattern should
    # pass through even if it happens to be a bare glob.
    if (
        target == "content"
        and not args.get("file_glob")
        and not _is_valid_regex(pattern)
    ):
        try:
            re.compile(pattern)
            compile_reason = ""
            # Defensive default — this branch is only reached when the
            # pattern fails _is_valid_regex, so compile always raises, but
            # keep the classifier result bound for the type checker.
            reason, recovery = _classify_regex_error(pattern, None)
        except re.error as exc:
            compile_reason = str(exc)
            # #2308 — decompose the compile failure into a structured
            # sub-cause + recovery directive so the agent fixes the pattern
            # instead of blind-retrying with a near-identical one.
            reason, recovery = _classify_regex_error(pattern, exc)
        return json.dumps(
            {
                "error": (
                    f"Invalid regex pattern {pattern!r}: {compile_reason}.\n\n"
                    "To fix:\n"
                    "  - If you meant a literal string, escape regex metacharacters "
                    "(e.g. replace '[' with '\\[', '*' with '\\*').\n"
                    "  - If you meant a filename pattern (like '*.py'), use target='files' "
                    "or move it to the file_glob parameter instead of the regex pattern.\n"
                    "  - Re-run search_files with a corrected regex pattern."
                ),
                # #2308 — structured reason + recovery.
                "reason": reason,
                "recovery": recovery,
            },
            ensure_ascii=False,
        )

    return search_tool(
        pattern=pattern, target=target, path=args.get("path", "."),
        file_glob=args.get("file_glob"), limit=args.get("limit", 50), offset=args.get("offset", 0),
        output_mode=args.get("output_mode", "content"), context=args.get("context", 0),
        order=args.get("order", "discovery"), task_id=tid)


def _read_file_schema_overrides():
    """One-word capability upgrade: "PDF (text layer)" → "PDF (scanned or
    text)" when hosted OCR has a trusted route (see
    read_extract.hosted_ocr_available). Config/env probe only — no
    network at schema-build time. Compaction's tool refresh (#97073)
    picks up a key added mid-session.
    """
    try:
        from tools.read_extract import hosted_ocr_available

        if hosted_ocr_available():
            return {
                "description": READ_FILE_SCHEMA["description"].replace(
                    "PDF (text layer)", "PDF (scanned or text)"
                )
            }
    except Exception:  # noqa: BLE001
        pass
    return {}


registry.register(name="read_file", toolset="file", schema=READ_FILE_SCHEMA, handler=_handle_read_file, check_fn=_check_file_reqs, emoji="📖", max_result_size_chars=100_000, dynamic_schema_overrides=_read_file_schema_overrides)
registry.register(name="write_file", toolset="file", schema=WRITE_FILE_SCHEMA, handler=_handle_write_file, check_fn=_check_file_reqs, emoji="✍️", max_result_size_chars=100_000)
def _patch_schema_overrides():
    """Layer the V4A patch mode onto the base replace-only schema for
    OpenAI-family mains (see PATCH_SCHEMA comment). Config/context probe
    only — no I/O at schema-build time; compaction's tool refresh
    (#97073) re-evaluates on model switches."""
    try:
        if not _is_openai_family_main():
            return {}
        params = {
            "type": "object",
            "properties": {
                "mode": _PATCH_V4A_PARAMS["mode"],
                **PATCH_SCHEMA["parameters"]["properties"],
                "patch": _PATCH_V4A_PARAMS["patch"],
            },
            "required": ["mode"],
        }
        return {"description": _PATCH_V4A_DESCRIPTION, "parameters": params}
    except Exception:  # noqa: BLE001
        return {}


registry.register(name="patch", toolset="file", schema=PATCH_SCHEMA, handler=_handle_patch, check_fn=_check_file_reqs, emoji="🔧", max_result_size_chars=100_000, dynamic_schema_overrides=_patch_schema_overrides)
registry.register(name="search_files", toolset="file", schema=SEARCH_FILES_SCHEMA, handler=_handle_search_files, check_fn=_check_file_reqs, emoji="🔎", max_result_size_chars=100_000)
