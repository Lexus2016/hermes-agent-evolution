"""Adaptive handoff format selection and graph-context extraction for subagent delegation.

Implements #3283: Routed Graph Handoff — adaptively routes between typed dependency graph
and natural-language/collapsed-summary handoff formats to cut context token costs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


_FILE_PATH_RE = re.compile(
    r"(?:[\w\-\./]+\.(?:py|js|ts|tsx|jsx|json|yaml|yml|md|toml|sh|html|css|sql|go|rs|c|cpp|h))",
    re.IGNORECASE,
)
_CODE_KEYWORDS = frozenset({
    "test",
    "pytest",
    "bug",
    "fix",
    "refactor",
    "patch",
    "git",
    "commit",
    "file",
    "path",
    "build",
    "ci",
    "error",
    "traceback",
    "exception",
    "function",
    "class",
    "module",
    "endpoint",
    "api",
    "schema",
})


@dataclass
class DependencyGraphContext:
    """Typed summary of files, tool executions, and key context constraints."""

    files_referenced: set[str] = field(default_factory=set)
    tools_executed: list[str] = field(default_factory=list)
    recent_errors: list[str] = field(default_factory=list)
    key_facts: list[str] = field(default_factory=list)

    def render_markdown(self) -> str:
        """Render compact dependency graph markdown."""
        lines = ["[HANDOFF DEPENDENCY GRAPH — background reference only]"]
        if self.files_referenced:
            sorted_files = sorted(self.files_referenced)[:15]
            lines.append(f"• Referenced Files: {', '.join(sorted_files)}")
        if self.tools_executed:
            # Deduplicate sequential identical actions
            deduped_actions: list[str] = []
            for act in self.tools_executed:
                if not deduped_actions or deduped_actions[-1] != act:
                    deduped_actions.append(act)
            lines.append(f"• Key Actions: {' -> '.join(deduped_actions[-6:])}")
        if self.recent_errors:
            lines.append(f"• Prior Errors: {'; '.join(self.recent_errors[-3:])}")
        if self.key_facts:
            lines.append("• Notes:")
            for fact in self.key_facts[-4:]:
                lines.append(f"  - {fact}")
        return "\n".join(lines)


def extract_dependency_graph(
    turns: list[dict[str, Any]],
) -> DependencyGraphContext:
    """Extract files, actions, errors, and key facts from turn history without LLM calls."""
    graph = DependencyGraphContext()
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        content = turn.get("content") or ""

        # Extract file paths from string content
        if isinstance(content, str) and content:
            for match in _FILE_PATH_RE.findall(content):
                if "/" in match or "." in match:
                    cleaned = match.strip(".,;:()[]{}'\"")
                    if len(cleaned) > 2:
                        graph.files_referenced.add(cleaned)

        # Extract tool calls
        tool_calls = turn.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if isinstance(tc, dict):
                    fn = tc.get("function", {})
                    name = fn.get("name") if isinstance(fn, dict) else tc.get("name")
                    if name:
                        graph.tools_executed.append(str(name))

        # Extract tool errors
        if role == "tool" and isinstance(content, str):
            lower_c = content.lower()
            if "error" in lower_c or "failed" in lower_c or "exception" in lower_c:
                first_line = content.strip().split("\n")[0][:120]
                graph.recent_errors.append(first_line)

        # Extract key user facts
        if role == "user" and isinstance(content, str) and len(content) < 300:
            clean_fact = content.strip().replace("\n", " ")
            if clean_fact and clean_fact not in graph.key_facts:
                graph.key_facts.append(clean_fact)

    return graph


def select_handoff_format(
    turns: list[dict[str, Any]],
    goal: Optional[str] = None,
    requested_mode: Optional[str] = "auto",
) -> str:
    """Select the optimal handoff format ('graph' vs 'collapsed_summary').

    If requested_mode is 'graph' or 'collapsed_summary', returns that mode.
    If 'auto', evaluates the goal and turn contents:
    - Code/technical/file-heavy tasks -> 'graph' (saves 40-60% tokens)
    - Open-ended conversation / reasoning -> 'collapsed_summary'
    """
    mode = (requested_mode or "auto").strip().lower()
    if mode in {"graph", "collapsed_summary"}:
        return mode

    text_to_score = (goal or "") + " "
    for turn in turns[-3:]:
        c = turn.get("content")
        if isinstance(c, str):
            text_to_score += c[:200] + " "

    tokens = set(re.findall(r"\w+", text_to_score.lower()))
    code_matches = len(tokens.intersection(_CODE_KEYWORDS))

    if code_matches >= 2 or _FILE_PATH_RE.search(text_to_score):
        return "graph"

    return "collapsed_summary"
