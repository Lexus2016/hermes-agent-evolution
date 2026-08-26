# Read-only tool tier for exploratory subagent tool calls.
#
# HERMES-SUBAGENT-ATTRIBUTION subagent_id=sa-0-5295fac9 parent=root task_index=0 spawned_at=2026-08-26T00:06:28+00:00
#
# Implements evo-2026-08-26-01: a typed least-privilege execution mode so
# exploratory subagents can probe tools without destructive operations or
# human approval round-trips (mirrors NemoClaw's `call-read-only TOOL --json`).
#
# Design:
# - An allowlist of read-only tool names (READ_ONLY_TOOLS) plus argument-level
#   guards for dual-mode tools (terminal/read_file variants).
# - Enforcement is opt-in per process/agent: either the environment variable
#   ``HERMES_SUBAGENT_READ_ONLY=1`` or an agent attribute ``read_only_mode``
#   set by the delegation layer when spawning an exploratory subagent.
# - Fail-closed for explicitly non-read-only calls; fail-open on internal
#   errors of the classifier itself would defeat the tier, so unknown tools
#   are DENIED (the subagent can still use the known read-only surface).
"""Read-only tool-tier enforcement helpers."""

from __future__ import annotations

import os
import re
from typing import Any, Optional

ENV_READ_ONLY = "HERMES_SUBAGENT_READ_ONLY"

#: Tools whose whole surface is observational and side-effect free.
READ_ONLY_TOOLS = frozenset({
    "read_file",
    "search_files",
    "repo_map",
    "list_directory",
    "list_dir",
    "glob",
    "grep",
    "web_search",
    "web_fetch",
    "browser_navigate",
    "get_tweet",
    "get_user_by_username",
    "list_issues",
    "get_repo",
    "get_issue",
    "tool_describe",
    "tool_search",
})

#: Dual-mode tools allowed only when the arguments are purely observational.
_DUAL_MODE = {
    "terminal",
    "shell",
    "bash",
    "execute_command",
    "run_command",
    "execute_code",
}

# Commands that mutate state even inside a dual-mode tool.
_WRITE_CMD_PATTERNS = (
    r"\brm\b",
    r"\bmv\b",
    r"\bcp\b",
    r"\bmkdir\b",
    r"\btouch\b",
    r"\bchmod\b",
    r"\bchown\b",
    r"\btee\b",
    # Any shell redirection ('>' covers '>', ' >', '2>', and '>>') writes state.
    r">",
    r"\bgit\s+(commit|push|reset|rebase|merge|checkout|clean|apply|stash)\b",
    r"\bpip\s+install\b",
    r"\bnpm\s+(install|i)\b",
    r"\bcurl\b",
    r"\bwget\b",
    r"\bkill\b",
    r"\bsudo\b",
    r"\bdocker\s+(run|rm|rmi|build)\b",
    r"\bsystemctl\b",
)
_WRITE_RE = re.compile("|".join(_WRITE_CMD_PATTERNS))


def read_only_mode_enabled(agent: Any = None) -> bool:
    """Return True when this agent/process runs under the read-only tier."""
    if getattr(agent, "read_only_mode", False):
        return True
    return os.environ.get(ENV_READ_ONLY, "").strip().lower() in ("1", "true", "yes")


def block_reason(function_name: str, function_args: Any) -> Optional[str]:
    """Return a denial reason when the call is not read-only-safe, else None."""
    name = str(function_name or "").strip().lower()
    args = function_args if isinstance(function_args, dict) else {}

    if name in READ_ONLY_TOOLS:
        return None

    if name in _DUAL_MODE:
        cmd = str(args.get("command") or args.get("cmd") or "")
        if cmd and not _WRITE_RE.search(cmd):
            return None
        return (
            f"tool '{name}' is not permitted in read-only mode "
            "(mutating command); re-run the subagent without the read-only "
            "tier to execute state-changing commands"
        )

    # Unknown / unclassified tool: fail closed.
    return f"tool '{name}' is not on the read-only allowlist"


def blocked_tool_result(reason: str) -> str:
    """JSON tool-result payload used when the tier denies a call."""
    import json

    return json.dumps(
        {
            "error": f"Tool execution blocked by read-only tier: {reason}",
            "status": "blocked",
            "tier": "read_only",
        },
        ensure_ascii=False,
    )
