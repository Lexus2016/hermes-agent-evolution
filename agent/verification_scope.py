# -*- coding: utf-8 -*-
"""Verification-scope boundary enforcement for Hermes execution path (issue #2436).

Enforces the 'least agency' principle across delegated tasks and evolution pipeline
stages to prevent out-of-scope actions and sandbox escapes (addressing UK AISI
and OWASP Top 10 for Agentic Applications recommendations).

Features:
- Structured VerificationScope defining allowed paths, denied paths, allowed commands,
  denied commands, read-only constraints, and network access policies.
- Fine-grained checkers for file access, shell command execution, and network requests.
- Preset scope profiles for evolution pipeline stages (research: read-only,
  analysis: read-only, implementation: workspace-scoped write).
- Structured violation auditing and escalation logging.
"""

from __future__ import annotations

import fnmatch
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

__all__ = [
    "VerificationScope",
    "ScopeViolation",
    "VerificationScopeEnforcer",
    "get_stage_verification_scope",
]


@dataclass
class VerificationScope:
    """Explicit capability boundaries for a delegated task or pipeline stage."""

    allowed_paths: List[str] = field(default_factory=list)
    denied_paths: List[str] = field(default_factory=list)
    allowed_commands: List[str] = field(default_factory=list)
    denied_commands: List[str] = field(default_factory=list)
    allow_network: bool = True
    allowed_hosts: List[str] = field(default_factory=list)
    read_only: bool = False
    name: str = "default_scope"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> VerificationScope:
        return cls(
            allowed_paths=list(d.get("allowed_paths", []) or []),
            denied_paths=list(d.get("denied_paths", []) or []),
            allowed_commands=list(d.get("allowed_commands", []) or []),
            denied_commands=list(d.get("denied_commands", []) or []),
            allow_network=bool(d.get("allow_network", True)),
            allowed_hosts=list(d.get("allowed_hosts", []) or []),
            read_only=bool(d.get("read_only", False)),
            name=str(d.get("name", "default_scope")),
        )


@dataclass
class ScopeViolation:
    """Structured record of an out-of-scope operation attempt."""

    action_type: str  # "file_read", "file_write", "command_exec", "network_request"
    target: str
    reason: str
    scope_name: str = "default_scope"
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class VerificationScopeEnforcer:
    """Enforces execution scope boundaries against attempted actions."""

    def __init__(self, scope: Optional[VerificationScope] = None) -> None:
        self.scope = scope or VerificationScope()
        self.violations: List[ScopeViolation] = []

    def check_file_access(
        self,
        target_path: str | Path,
        mode: str = "read",  # "read" | "write"
    ) -> Tuple[bool, Optional[ScopeViolation]]:
        """Validate whether a file read/write is within scope boundaries."""
        path_str = str(Path(target_path).resolve())

        # 1. Read-only gate
        if mode == "write" and self.scope.read_only:
            violation = ScopeViolation(
                action_type="file_write",
                target=path_str,
                reason="Scope is configured as strictly read-only",
                scope_name=self.scope.name,
            )
            self.violations.append(violation)
            return False, violation

        # 2. Denied paths check
        for denied in self.scope.denied_paths:
            denied_resolved = str(Path(denied).resolve())
            if path_str == denied_resolved or path_str.startswith(
                denied_resolved + os.sep
            ):
                violation = ScopeViolation(
                    action_type=f"file_{mode}",
                    target=path_str,
                    reason=f"Path matches explicitly denied boundary: {denied}",
                    scope_name=self.scope.name,
                )
                self.violations.append(violation)
                return False, violation

        # 3. Allowed paths check (if non-empty, path must be inside one of allowed_paths)
        if self.scope.allowed_paths:
            is_allowed = False
            for allowed in self.scope.allowed_paths:
                allowed_resolved = str(Path(allowed).resolve())
                if path_str == allowed_resolved or path_str.startswith(
                    allowed_resolved + os.sep
                ):
                    is_allowed = True
                    break
            if not is_allowed:
                violation = ScopeViolation(
                    action_type=f"file_{mode}",
                    target=path_str,
                    reason=f"Path is outside explicitly allowed boundaries: {self.scope.allowed_paths}",
                    scope_name=self.scope.name,
                )
                self.violations.append(violation)
                return False, violation

        return True, None

    def check_command_execution(
        self,
        command_line: str,
    ) -> Tuple[bool, Optional[ScopeViolation]]:
        """Validate whether a terminal command is allowed under the current scope."""
        cmd = command_line.strip()
        if not cmd:
            return True, None

        # 1. Check denied commands
        for pattern in self.scope.denied_commands:
            if fnmatch.fnmatch(cmd, pattern) or re.search(
                r"\b" + re.escape(pattern) + r"\b", cmd
            ):
                violation = ScopeViolation(
                    action_type="command_exec",
                    target=cmd,
                    reason=f"Command matches denied pattern: {pattern}",
                    scope_name=self.scope.name,
                )
                self.violations.append(violation)
                return False, violation

        # 2. Check allowed commands (if specified)
        if self.scope.allowed_commands:
            matched_allowed = False
            for pattern in self.scope.allowed_commands:
                if fnmatch.fnmatch(cmd, pattern) or any(
                    cmd.startswith(prefix.strip())
                    for prefix in self.scope.allowed_commands
                ):
                    matched_allowed = True
                    break
            if not matched_allowed:
                violation = ScopeViolation(
                    action_type="command_exec",
                    target=cmd,
                    reason=f"Command is not in allowed command list: {self.scope.allowed_commands}",
                    scope_name=self.scope.name,
                )
                self.violations.append(violation)
                return False, violation

        return True, None

    def check_network_access(
        self,
        host_or_url: str,
    ) -> Tuple[bool, Optional[ScopeViolation]]:
        """Validate network outbound request against scope."""
        if not self.scope.allow_network:
            violation = ScopeViolation(
                action_type="network_request",
                target=host_or_url,
                reason="Network access is disabled for this scope",
                scope_name=self.scope.name,
            )
            self.violations.append(violation)
            return False, violation

        if self.scope.allowed_hosts:
            host = host_or_url
            if "://" in host_or_url:
                parsed = urlparse(host_or_url)
                host = parsed.hostname or host_or_url

            host_clean = host.lower().strip()
            is_allowed = any(
                host_clean == h.lower() or host_clean.endswith("." + h.lower())
                for h in self.scope.allowed_hosts
            )
            if not is_allowed:
                violation = ScopeViolation(
                    action_type="network_request",
                    target=host_or_url,
                    reason=f"Host '{host}' is not in allowed hosts: {self.scope.allowed_hosts}",
                    scope_name=self.scope.name,
                )
                self.violations.append(violation)
                return False, violation

        return True, None


def get_stage_verification_scope(
    stage_name: str,
    workspace_root: str | Path = ".",
) -> VerificationScope:
    """Pre-configured least-agency verification scope profiles for pipeline stages."""
    root = str(Path(workspace_root).resolve())
    stage = stage_name.lower().strip()

    if stage in ("research", "discovery"):
        return VerificationScope(
            name="research_stage",
            allowed_paths=[root],
            read_only=True,
            allow_network=True,
            allowed_hosts=[
                "arxiv.org",
                "api.semanticscholar.org",
                "github.com",
                "api.github.com",
            ],
            allowed_commands=["git", "grep", "ripgrep", "rg", "cat", "find", "ls"],
            denied_commands=[
                "rm",
                "git push",
                "git commit",
                "curl -X POST",
                "curl -X PUT",
            ],
        )

    if stage in ("analysis", "triage", "audit"):
        return VerificationScope(
            name="analysis_stage",
            allowed_paths=[root],
            read_only=True,
            allow_network=False,
            allowed_commands=[
                "git",
                "grep",
                "rg",
                "pytest",
                "ruff",
                "python",
                "python3",
            ],
            denied_commands=["rm -rf", "git push", "curl", "wget"],
        )

    if stage in ("implementation", "synthesis", "fix"):
        return VerificationScope(
            name="implementation_stage",
            allowed_paths=[root],
            denied_paths=[
                "/etc",
                os.path.expanduser("~/.ssh"),
                os.path.expanduser("~/.aws"),
            ],
            read_only=False,
            allow_network=True,
            allowed_hosts=[
                "github.com",
                "api.github.com",
                "pypi.org",
                "files.pythonhosted.org",
            ],
            denied_commands=["sudo", "rm -rf /", "chmod -R 777", "mkfs"],
        )

    return VerificationScope(
        name=f"{stage}_scope",
        allowed_paths=[root],
        read_only=False,
        allow_network=True,
    )
