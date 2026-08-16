# -*- coding: utf-8 -*-
"""Harness configuration disclosure for evaluations (#2481).

Source: arXiv:2605.23950v1 ("Stop Comparing LLM Agents Without Disclosing
the Harness", May 2026). Every benchmark score is jointly produced by a
model and a harness, yet the harness is rarely disclosed and almost never
held constant across comparisons. Holding the model fixed while changing
only the harness can raise Terminal-Bench scores by 7+ percentage points
and produce ~10x differences in coding benchmark accuracy.

This module provides a deterministic, structured harness-configuration
snapshot that Hermes can report alongside any benchmark or evaluation
result it publishes, making self-evaluation trustworthy and comparable
across evolution cycles.

The snapshot captures:
- system prompt version
- skill versions
- tool definitions
- model provider / model
- context management settings
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "HarnessConfigSnapshot",
    "capture_harness_config",
    "write_harness_config",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class HarnessConfigSnapshot:
    """A structured snapshot of the harness configuration."""

    system_prompt_version: str = ""
    skill_versions: Dict[str, str] = field(default_factory=dict)
    tool_definitions: List[str] = field(default_factory=list)
    model_provider: str = ""
    model: str = ""
    context_management: Dict[str, Any] = field(default_factory=dict)
    captured_at: str = ""

    def __post_init__(self) -> None:
        if not self.captured_at:
            self.captured_at = _now_iso()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "HarnessConfigSnapshot":
        return cls(
            system_prompt_version=str(d.get("system_prompt_version", "")),
            skill_versions=dict(d.get("skill_versions", {}) or {}),
            tool_definitions=list(d.get("tool_definitions", []) or []),
            model_provider=str(d.get("model_provider", "")),
            model=str(d.get("model", "")),
            context_management=dict(d.get("context_management", {}) or {}),
            captured_at=str(d.get("captured_at", "")),
        )


def _read_skill_versions(skills_dir: Path) -> Dict[str, str]:
    """Read skill versions from SKILL.md frontmatter (best-effort)."""
    versions: Dict[str, str] = {}
    if not skills_dir.exists():
        return versions
    for skill_md in skills_dir.rglob("SKILL.md"):
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        name = ""
        version = ""
        in_frontmatter = False
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped == "---":
                if in_frontmatter:
                    break
                in_frontmatter = True
                continue
            if in_frontmatter:
                if stripped.startswith("name:"):
                    name = stripped.split(":", 1)[1].strip().strip("\"'")
                elif stripped.startswith("version:"):
                    version = stripped.split(":", 1)[1].strip().strip("\"'")
        if name and version:
            versions[name] = version
    return versions


def capture_harness_config(
    *,
    system_prompt_version: str = "",
    model_provider: str = "",
    model: str = "",
    skills_dir: Optional[Path | str] = None,
    tool_names: Optional[List[str]] = None,
    context_management: Optional[Dict[str, Any]] = None,
) -> HarnessConfigSnapshot:
    """Capture a structured harness-configuration snapshot.

    All fields are optional and best-effort — a missing value is recorded
    as an empty string / empty dict rather than guessed. This keeps the
    snapshot honest: an undisclosed field is visibly absent, not silently
    filled in.
    """
    skill_versions: Dict[str, str] = {}
    if skills_dir is not None:
        skill_versions = _read_skill_versions(Path(skills_dir))

    return HarnessConfigSnapshot(
        system_prompt_version=system_prompt_version,
        skill_versions=skill_versions,
        tool_definitions=sorted(tool_names or []),
        model_provider=model_provider,
        model=model,
        context_management=dict(context_management or {}),
    )


def write_harness_config(
    snapshot: HarnessConfigSnapshot,
    path: Path | str,
) -> Path:
    """Write the harness-config snapshot to *path* (atomic).

    Returns the written path. The snapshot is written as pretty-printed
    JSON so it is human-readable alongside the evaluation result it
    describes.
    """
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".harness_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(snapshot.to_dict(), f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return dest
