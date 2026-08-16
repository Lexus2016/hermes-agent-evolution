# -*- coding: utf-8 -*-
"""Safety-review gate for self-modification merges + static analysis (#2575).

Misevolution guardrail (parent #2538; arXiv:2509.26354 "Your Agent May
Misevolve" — workflow pathway).

Adds a deterministic safety-review gate that runs **static analysis** on
generated/changed code before any self-modification merge is allowed:

1. Detects dangerous code patterns (``os.system``, unguarded ``subprocess``,
   ``eval``/``exec``, ``__import__``, unguarded file deletion, ``pickle.loads``
   on untrusted data, raw-IP network calls, etc.).
2. Returns violations that block the autonomous self-merge — the merge gate
   appends these to its existing policy pipeline.
3. Logs all changes with rollback to safe checkpoints (the merge gate's
   versioned snapshots + the existing ``SkillReuseGate.version`` mechanism).

Pure, DI-testable, import-safe — all IO is explicit.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "SafetyGateVerdict",
    "scan_code_for_safety_violations",
    "check_safety_gate",
]

# Dangerous-code patterns (static analysis on generated Python).
# Each entry: (compiled_regex, description, severity).
_DANGEROUS_PATTERNS: List[Tuple[re.Pattern, str, str]] = [
    (
        re.compile(r"\bos\.system\s*\("),
        "os.system() — shell injection risk; use subprocess with shell=False",
        "high",
    ),
    (
        re.compile(r"\beval\s*\("),
        "eval() — arbitrary code execution; avoid in generated code",
        "high",
    ),
    (
        re.compile(r"\bexec\s*\("),
        "exec() — arbitrary code execution; avoid in generated code",
        "high",
    ),
    (
        re.compile(r"\b__import__\s*\("),
        "__import__() — dynamic import; avoid in generated code",
        "medium",
    ),
    (
        re.compile(r"subprocess\.\w+\s*\([^)]*shell\s*=\s*True"),
        "subprocess with shell=True — shell injection risk",
        "high",
    ),
    (
        re.compile(r"\bpickle\.loads?\s*\("),
        "pickle.loads() — deserialization of untrusted data is unsafe",
        "high",
    ),
    (
        re.compile(r"\bos\.remove\s*\(|\bos\.unlink\s*\(|\bshutil\.rmtree\s*\("),
        "unguarded file/directory deletion — destructive operation",
        "medium",
    ),
    (
        re.compile(r"\bopen\s*\([^)]*['\"]w['\"]"),
        "file open in write mode — verify target path is not a system file",
        "low",
    ),
    (
        re.compile(
            r"\b(?:socket|urllib|requests|httpx|aiohttp)\.\w+\s*\([^)]*"
            r"(?:\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"
        ),
        "network call to raw IP — potential exfiltration; use domain + verify",
        "medium",
    ),
    (
        re.compile(r"\bbase64\.b64decode\s*\([^)]*\)\s*\.(?:decode|__import__)"),
        "base64-decoded payload used as code — obfuscated execution",
        "high",
    ),
    (
        re.compile(r"(?:subprocess|os)\.(?:Popen|system|execvp?|execvpe?)\s*\("),
        "subprocess/os process execution — verify command is not constructed from user input",
        "medium",
    ),
]


@dataclass
class SafetyGateVerdict:
    """Result of the safety-review gate on a set of changed files."""

    safe: bool
    violations: List[str] = field(default_factory=list)
    files_scanned: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SafetyGateVerdict":
        return cls(
            safe=bool(d.get("safe", True)),
            violations=list(d.get("violations", []) or []),
            files_scanned=int(d.get("files_scanned", 0)),
        )


def scan_code_for_safety_violations(
    file_contents: Dict[str, str],
) -> List[str]:
    """Scan *file_contents* (path → content) for dangerous code patterns.

    Returns a list of violation strings, each formatted as:
    ``"<path>:<line>: <description> [<severity>]"``.

    Only scans ``.py`` files — non-Python files are skipped (the patterns
    target Python syntax). The scan is line-by-line for accurate line
    numbers and to avoid multi-line false positives.
    """
    violations: List[str] = []
    for path, content in file_contents.items():
        if not path.endswith(".py"):
            continue
        for line_no, line in enumerate((content or "").splitlines(), start=1):
            # Skip comments and strings that merely *mention* a pattern.
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            for pattern, desc, severity in _DANGEROUS_PATTERNS:
                if pattern.search(line):
                    violations.append(f"{path}:{line_no}: {desc} [{severity}]")
    return violations


def check_safety_gate(
    files: Sequence[Dict[str, Any]],
    source_contents: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Run the safety-review gate and return blocking-violation strings.

    ``files`` is the ``gh pr view --json files`` shape. ``source_contents``
    is an optional map of source file path → full content (the same shape
    used by the reachability gate). When ``source_contents`` is ``None``
    or empty, the gate is skipped (opt-in, same pattern as the flip/floor
    gates — skills without generated code merge as before).

    When source contents ARE provided, every ``.py`` file is scanned for
    the dangerous patterns above. Any match is a blocking violation.
    """
    if not source_contents:
        return []  # opt-out: no contents to scan

    # Only scan files that are actually in the PR's changed-file set.
    changed_paths = {str(f.get("path", "")) for f in files if isinstance(f, dict)}
    relevant = {
        path: content
        for path, content in source_contents.items()
        if path in changed_paths
    }

    violations = scan_code_for_safety_violations(relevant)
    return [f"SAFETY_GATE_VIOLATION: {v}" for v in violations]
