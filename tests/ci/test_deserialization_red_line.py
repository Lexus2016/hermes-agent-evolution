"""Deserialization red-line regression guard (issue #2876).

Security audit context (2026-08-19, Check Point framework-vulns wave):
CVE-2026-28277 (msgpack deserialization RCE in LangGraph), CVE-2025-67644
(SQLite injection in langgraph get_state_history), CVE-2026-27022 (Redis
injection). None of those libraries is a direct dependency of this project
(pyproject.toml carries no langgraph / langgraph-checkpoint-sqlite / redis
pins), so the mechanical pin part of the issue does not apply here. The
durable deliverable the issue names is the **deserialization red-line
guard**: untrusted-content deserialization must never enter the trusted
codebase, because it is equivalent to arbitrary code execution.

Audit findings this guard encodes:
- ``agent/skill_utils.py`` parses YAML via CSafeLoader/SafeLoader (safe).
- ``hermes_cli/xai_retirement.py`` uses ruamel ``YAML(typ="rt")`` — the
  round-trip loader does NOT construct arbitrary Python objects (safe).
- ``utils.fast_safe_load`` is a libyaml-safe drop-in (see
  tests/test_fast_safe_load.py).
- The ONLY actual ``pickle.loads`` call in the tree is
  ``optional-skills/research/darwinian-evolver/scripts/show_snapshot.py``,
  explicitly gated behind a ``--i-trust-this-file`` CLI flag with an S301
  suppression — a deliberate, user-gated exception, allowlisted here.

The scan is AST-based (not regex) so string literals and comments that merely
DESCRIBE these APIs — e.g. ``evolution/lib/safety_review_gate.py`` and
``plugins/security-guidance/patterns.py``, which exist to flag them — do not
trigger it. Only real ``Call`` nodes are flagged. It is the same insurance
shape as tests/ci/test_deprecated_openai_assistants.py (#2879).
"""

from __future__ import annotations

import ast
from pathlib import Path

#: (module, attribute) pairs that are unambiguously dangerous when called.
#: ``yaml.load`` is deliberately NOT here: safe-loader forms (explicit
#: SafeLoader/CSafeLoader, ruamel round-trip) are legitimate, and the safe
#: path is already pinned by tests/test_fast_safe_load.py.
_UNSAFE_CALLS = {
    ("pickle", "load"),
    ("pickle", "loads"),
    ("pickle", "Unpickler"),
    ("marshal", "load"),
    ("marshal", "loads"),
    ("shelve", "open"),
    ("yaml", "unsafe_load"),
}

#: Files with a deliberate, reviewed, user-gated exception. Every entry must
#: name the gate that makes the call safe.
_ALLOWLISTED_FILES = {
    # Trust-gated by --i-trust-this-file CLI flag + explicit S301 suppression.
    "optional-skills/research/darwinian-evolver/scripts/show_snapshot.py",
}

_EXCLUDED_DIRS = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "docs",
    "website",
    "mcp-research-data",
    "mcp-research",
    "research",
}
_EXCLUDED_FILES = {"test_deserialization_red_line.py"}

_EXTENSIONS = {".py"}

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Top-level code roots scanned by this guard.
_CODE_ROOTS = (
    "agent",
    "acp_adapter",
    "cron",
    "evolution",
    "gateway",
    "hermes",
    "hermes_cli",
    "plugins",
    "providers",
    "scripts",
    "tools",
)


def _iter_scan_targets() -> list[Path]:
    """Yield the files this guard scans (trusted code roots only)."""
    targets: list[Path] = []
    for root_name in _CODE_ROOTS:
        root = _REPO_ROOT / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in _EXTENSIONS:
                continue
            if path.name in _EXCLUDED_FILES:
                continue
            if any(part in _EXCLUDED_DIRS for part in path.parts):
                continue
            rel = path.relative_to(_REPO_ROOT).as_posix()
            if rel in _ALLOWLISTED_FILES:
                continue
            targets.append(path)
    return targets


def _call_name(node: ast.Call) -> tuple[str, str] | None:
    """Return ``(module, attr)`` for a call like ``pickle.loads(...)``."""
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return (func.value.id, func.attr)
    return None


def _scan_file(path: Path) -> list[tuple[int, str]]:
    """Return ``(line_no, source_line)`` for unsafe-deserialization calls."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        # Unparseable/undecodable files are out of the trusted-code contract;
        # a syntax error in a committed .py is caught by normal lint/CI.
        return []
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name in _UNSAFE_CALLS:
            hits.append((
                node.lineno,
                source.splitlines()[node.lineno - 1].strip()[:200],
            ))
    return hits


def _scan_targets(targets: list[Path]) -> list[tuple[Path, str]]:
    """Return ``(path, matched_line)`` for every unsafe-deserialization call."""
    hits: list[tuple[Path, str]] = []
    for path in targets:
        for line_no, line in _scan_file(path):
            hits.append((path, f"{path}:{line_no}: {line}"))
    return hits


def test_no_unguarded_unsafe_deserialization() -> None:
    """No trusted code may deserialize untrusted content unsafely (#2876).

    pickle/marshal/shelve/yaml.unsafe_load are arbitrary-code-execution
    surfaces; an evolved skill or plugin reintroducing them unguarded is a
    red line. Deliberate user-gated exceptions live in
    :data:`_ALLOWLISTED_FILES` with the gate named.
    """
    hits = _scan_targets(_iter_scan_targets())
    assert not hits, (
        "Unsafe deserialization call found in trusted code (CVE-2026-28277 / "
        "CVE-2025-67644 / CVE-2026-27022 family — see issue #2876). "
        "Use yaml.safe_load / fast_safe_load instead, or add a reviewed, "
        "user-gated allowlist entry with the gate named:\n"
        + "\n".join(line for _path, line in hits)
    )


def test_guard_is_wired_to_a_nonempty_scan() -> None:
    """The guard must actually scan something — a silently-empty scan is a
    guard that never fires."""
    targets = _iter_scan_targets()
    assert len(targets) >= 50, (
        f"scan unexpectedly small ({len(targets)} files); the guard may be "
        "mis-rooted and silently vacuous"
    )
