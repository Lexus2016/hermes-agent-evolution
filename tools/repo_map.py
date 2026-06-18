#!/usr/bin/env python3
"""Repo Map Tool - structured bird's-eye Python codebase overview.

Issue #320, minimal first slice.  Instead of the agent navigating reactively
(``search_files`` then ``read_file`` one file at a time, building a mental
model incrementally), ``repo_map`` gives a single ranked overview of the
functions / classes / methods in a directory tree, with ``file:line``
locations, a reference-count rank (hot symbols first), and context-budget
truncation.

SCOPE — deliberately Python-only via the stdlib ``ast`` module.  ZERO new
dependencies.  No tree-sitter, no JS/TS/Go/Rust grammars, no centrality graph
ranker, no cache.  The full multi-language tree-sitter repo map (5 grammars +
PageRank-style centrality + invalidation cache) described in the issue is the
follow-up epic; this slice is the realistic, additive first step that ships
value today.

ADDITIVE: a new read-only tool.  It does not change ``read_file`` /
``search_files`` / ``write_file`` / ``patch`` — they keep working unchanged.
Registration mirrors ``tools/file_tools.py`` exactly (module-level
``registry.register(...)`` with ``toolset="file"``), so it is auto-discovered
by ``registry.discover_builtin_tools()`` at startup with no manual import wiring.
"""

import ast
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Directory names never scanned — mirrors the spirit of the file-tool path
# safety (ripgrep excludes hidden dirs + .gitignore; here we hard-exclude the
# usual heavy / generated / VCS trees).  Any path segment matching one of these
# (case-sensitive for the literal names, plus any dot-prefixed dir) is skipped.
_EXCLUDED_DIR_NAMES = frozenset({
    "node_modules",
    "venv",
    "__pycache__",
    "site-packages",
    "dist",
    "build",
    ".egg-info",
    "vendor",
})

# Default ranked-symbol budget.  Each symbol line is short (name + kind +
# file:line + ref_count), so a few hundred fit comfortably in a context
# window — large enough for a useful overview, small enough to stay cheap.
_DEFAULT_MAX_SYMBOLS = 200


def _is_excluded_dir(name: str) -> bool:
    """Return True for dot-dirs (``.git``, ``.venv``, …) and known heavy dirs."""
    if name.startswith("."):
        return True
    if name.endswith(".egg-info"):
        return True
    return name in _EXCLUDED_DIR_NAMES


def _iter_python_files(root: Path):
    """Yield ``*.py`` files under *root*, pruning excluded directories.

    Uses ``os.walk`` with in-place ``dirnames`` pruning so excluded trees
    (``.git``, ``venv``, ``node_modules``, ``__pycache__``, …) are never
    descended into — both cheaper and safer than walking then filtering.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune excluded dirs in place so os.walk does not descend into them.
        dirnames[:] = [d for d in dirnames if not _is_excluded_dir(d)]
        for fn in filenames:
            if fn.endswith(".py"):
                yield Path(dirpath) / fn


def extract_symbols(file_path: Path) -> list[dict]:
    """Extract top-level functions/classes and one level of methods.

    Returns a list of ``{"name", "kind", "line"}`` dicts.  ``kind`` is one of
    ``"function"`` / ``"class"`` / ``"method"``.  Methods are namespaced as
    ``ClassName.method``.

    A file that cannot be read or parsed (syntax error, encoding error) yields
    an empty list — it is skipped, never fatal.  This keeps the whole-repo
    build robust against a single broken file.
    """
    try:
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(file_path))
    except (OSError, SyntaxError, ValueError):
        # SyntaxError: malformed Python.  ValueError: e.g. null bytes.
        # OSError: unreadable file.  All non-fatal — skip this file.
        return []

    symbols: list[dict] = []
    func_types = (ast.FunctionDef, ast.AsyncFunctionDef)

    for node in tree.body:
        if isinstance(node, func_types):
            symbols.append(
                {"name": node.name, "kind": "function", "line": node.lineno}
            )
        elif isinstance(node, ast.ClassDef):
            symbols.append(
                {"name": node.name, "kind": "class", "line": node.lineno}
            )
            for child in node.body:
                if isinstance(child, func_types):
                    symbols.append({
                        "name": f"{node.name}.{child.name}",
                        "kind": "method",
                        "line": child.lineno,
                    })
    return symbols


def _count_references(file_path: Path) -> dict[str, int]:
    """Count bare-name usages in *file_path*, keyed by identifier.

    A lightweight, dependency-free proxy for symbol centrality: every
    ``ast.Name`` load and the attribute part of an ``ast.Attribute`` access is
    tallied.  This is intentionally simple (no scope/type resolution) — it
    just surfaces which short names are "hot" across the tree, which is enough
    to float frequently-used symbols to the top of the overview.

    A broken / unreadable file contributes nothing (skipped, not fatal).
    """
    try:
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(file_path))
    except (OSError, SyntaxError, ValueError):
        return {}

    counts: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            counts[node.id] = counts.get(node.id, 0) + 1
        elif isinstance(node, ast.Attribute):
            counts[node.attr] = counts.get(node.attr, 0) + 1
    return counts


def _short_name(qualified: str) -> str:
    """Return the trailing identifier of a possibly-qualified symbol name.

    ``"Widget.method_a" -> "method_a"``; ``"hot" -> "hot"``.  Reference counts
    are keyed by the bare identifier, so methods rank by their own name.
    """
    return qualified.rsplit(".", 1)[-1]


def build_repo_map(
    path: str,
    max_symbols: int = _DEFAULT_MAX_SYMBOLS,
) -> dict:
    """Build a ranked, budget-truncated symbol overview of *path*.

    Returns a dict::

        {
          "root": <abs path>,
          "files_scanned": int,
          "total_symbols": int,        # before truncation
          "truncated": bool,
          "symbols": [                 # ranked, capped to max_symbols
            {"name", "kind", "file", "line", "ref_count"}, ...
          ],
        }

    or ``{"error": "..."}`` when *path* does not exist / is not a directory.

    Ranking: descending ``ref_count`` (how often the bare name is used across
    the tree), then by file + line for a stable, deterministic order.
    Truncation: keep the top *max_symbols* after ranking.
    """
    root = Path(os.path.expanduser(path)).resolve()
    if not root.exists():
        return {"error": f"Path does not exist: {path}"}
    if not root.is_dir():
        return {"error": f"Path is not a directory: {path}"}

    files = list(_iter_python_files(root))

    # First pass: aggregate reference counts across every Python file.
    ref_counts: dict[str, int] = {}
    for f in files:
        for name, n in _count_references(f).items():
            ref_counts[name] = ref_counts.get(name, 0) + n

    # Second pass: collect symbol definitions with their locations.
    all_symbols: list[dict] = []
    for f in files:
        try:
            rel = str(f.relative_to(root))
        except ValueError:
            rel = str(f)
        for sym in extract_symbols(f):
            short = _short_name(sym["name"])
            # A definition is itself a Name(Store) which we did NOT count, so
            # ref_count reflects usages only — exactly what we want for ranking.
            all_symbols.append({
                "name": sym["name"],
                "kind": sym["kind"],
                "file": rel,
                "line": sym["line"],
                "ref_count": ref_counts.get(short, 0),
            })

    total = len(all_symbols)
    # Rank: hot symbols first; deterministic tie-break by file then line.
    all_symbols.sort(key=lambda s: (-s["ref_count"], s["file"], s["line"]))

    truncated = total > max_symbols
    ranked = all_symbols[:max_symbols] if max_symbols >= 0 else all_symbols

    return {
        "root": str(root),
        "files_scanned": len(files),
        "total_symbols": total,
        "truncated": truncated,
        "symbols": ranked,
    }


# ---------------------------------------------------------------------------
# Schema + Registry  (mirrors tools/file_tools.py wiring)
# ---------------------------------------------------------------------------
from tools.registry import registry, tool_error, tool_result


def _check_file_reqs():
    """Lazy wrapper to avoid circular import with tools/__init__.py.

    Identical idiom to ``tools/file_tools.py:_check_file_reqs`` so repo_map
    shares the same availability gate as the other ``file`` toolset tools.
    """
    from tools import check_file_requirements
    return check_file_requirements()


REPO_MAP_SCHEMA = {
    "name": "repo_map",
    "description": (
        "Get a structured bird's-eye overview of a Python codebase: the "
        "functions, classes, and methods in a directory tree, each with its "
        "file:line location and a reference-count rank (most-used symbols "
        "first). Use this at the START of a coding task to understand code "
        "structure before editing — it is far cheaper than repeatedly calling "
        "search_files / read_file to discover where things live. Output is "
        "ranked and truncated to a symbol budget so it fits the context "
        "window. Python files only (.py); non-Python and broken-syntax files "
        "are skipped. .git, venvs, node_modules and __pycache__ are excluded."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory to map (absolute, relative, or ~/path). Defaults to the current working directory.",
                "default": ".",
            },
            "max_symbols": {
                "type": "integer",
                "description": "Maximum number of ranked symbols to return (context budget). Default 200.",
                "default": _DEFAULT_MAX_SYMBOLS,
                "minimum": 1,
            },
        },
        "required": [],
    },
}


def _handle_repo_map(args, **kw):
    """Dispatch handler — same ``(args, **kw)`` signature as file_tools handlers."""
    path = args.get("path", ".") or "."
    raw_max = args.get("max_symbols", _DEFAULT_MAX_SYMBOLS)
    try:
        max_symbols = int(raw_max)
    except (TypeError, ValueError):
        max_symbols = _DEFAULT_MAX_SYMBOLS
    if max_symbols < 1:
        max_symbols = _DEFAULT_MAX_SYMBOLS

    try:
        result = build_repo_map(path, max_symbols=max_symbols)
    except Exception as e:  # defensive — a handler must never raise to the loop
        logger.warning("repo_map failed for %s: %s", path, e)
        return tool_error(f"repo_map failed: {e}")

    if "error" in result:
        return tool_error(result["error"])
    return tool_result(result)


registry.register(
    name="repo_map",
    toolset="file",
    schema=REPO_MAP_SCHEMA,
    handler=_handle_repo_map,
    check_fn=_check_file_reqs,
    emoji="🗺️",
    max_result_size_chars=100_000,
)
