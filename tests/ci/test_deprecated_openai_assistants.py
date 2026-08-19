"""Regression guard: OpenAI Assistants API deprecated surface (hard shutdown).

OpenAI shut down the Assistants API — ``/v1/assistants``, ``/v1/threads``,
``/v1/threads/runs`` — on 2026-08-26. No degraded mode, no grace period, and
no automated Threads→Conversations migration. The Responses API is the
replacement surface.

This test scans the codebase and the shipped/optional skills for the
deprecated endpoint and SDK-resource surface and fails CI if any evolved
skill or code reintroduces it. It is the cheap, durable insurance that the
hard deadline (issue #2879) is never silently crossed again: a regression is
caught at CI time instead of breaking every OpenAI integration after the
shutdown.

The scan is intentionally conservative: it only flags strings that can only
mean the Assistants API — the URL paths themselves and OpenAI SDK beta
resource access (``client.beta.assistants`` / ``client.beta.threads``).
Generic words like "threads" or "runs" are not flagged, so this guard does
not generate false positives on unrelated code.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

#: Regexes that can only match the deprecated OpenAI Assistants surface.
_DEPRECATED_SURFACE = [
    re.compile(r"/v1/assistants\b"),
    re.compile(r"/v1/threads\b"),
    re.compile(r"/v1/threads/runs\b"),
    re.compile(r"beta\.assistants\b"),
    re.compile(r"beta\.threads\b"),
]

#: Repo subdirectories that may legitimately mention the deprecated surface
#: without being a live integration: documentation, research data, and the
#: guard's own test file.
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
_EXCLUDED_FILES = {"test_deprecated_openai_assistants.py"}

#: Extensions scanned everywhere under the code roots.
_CODE_EXTENSIONS = {".py", ".yaml", ".yml", ".json", ".sh"}
#: Skill files are mostly Markdown, so scan those roots for all text-ish
#: extensions (skills carry runnable instructions in prose + code fences).
_SKILL_EXTENSIONS = {".md", ".py", ".yaml", ".yml", ".json", ".sh", ".txt"}

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Top-level code roots to scan with :data:`_CODE_EXTENSIONS`.
_CODE_ROOTS = (
    "agent",
    "acp_adapter",
    "batch_runner.py",
    "cron",
    "evolution",
    "gateway",
    "hermes",
    "hermes_cli",
    "mcp_serve.py",
    "plugins",
    "providers",
    "run_agent.py",
    "scripts",
    "tools",
    "toolset_distributions.py",
    "toolsets.py",
)
#: Skill roots to scan with the broader :data:`_SKILL_EXTENSIONS`.
_SKILL_ROOTS = ("skills", "optional-skills")


def _iter_scan_targets() -> list[Path]:
    """Yield the files this guard scans, rooted at the repo checkout."""
    targets: list[Path] = []

    def _walk(root: Path, extensions: set[str]) -> None:
        if not root.is_dir():
            return
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in extensions:
                continue
            if path.name in _EXCLUDED_FILES:
                continue
            if any(part in _EXCLUDED_DIRS for part in path.parts):
                continue
            targets.append(path)

    for root_name in _CODE_ROOTS:
        root = _REPO_ROOT / root_name
        if root.is_file():
            if root.suffix.lower() in _CODE_EXTENSIONS:
                targets.append(root)
        else:
            _walk(root, _CODE_EXTENSIONS)
    for root_name in _SKILL_ROOTS:
        _walk(_REPO_ROOT / root_name, _SKILL_EXTENSIONS)
    return targets


def _scan_targets(targets: list[Path]) -> list[tuple[Path, str]]:
    """Return ``(path, matched_line)`` for every deprecated-surface hit."""
    hits: list[tuple[Path, str]] = []
    for path in targets:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if any(pattern.search(line) for pattern in _DEPRECATED_SURFACE):
                hits.append((path, f"{path}:{line_no}: {line.strip()[:200]}"))
    return hits


def test_no_deprecated_openai_assistants_surface() -> None:
    """No code or skill may use the OpenAI Assistants API (dead 2026-08-26)."""
    hits = _scan_targets(_iter_scan_targets())
    assert not hits, (
        "Deprecated OpenAI Assistants API surface found (shutdown 2026-08-26, "
        "see issue #2879). Migrate to the Responses/Conversations API:\n"
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
