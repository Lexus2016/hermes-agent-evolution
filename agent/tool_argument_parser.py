"""Validate model tool arguments and return structured corrective diagnostics."""
from __future__ import annotations
import json
import logging
from typing import Any, Optional
logger = logging.getLogger("agent.tool_executor")

def _parse_tool_arguments(
    raw_arguments: Any,
    function_name: str = "",
    *,
    agent: Any = None,
) -> tuple[dict, Optional[str]]:
    """Parse model-emitted arguments without repairing or coercing them.

    When parsing fails, the error message identifies *why* it failed so the
    model can correct the malformed field on retry instead of blind-retrying
    the same payload (issue #1647 — 285 terminal parse-error failures / 7d).

    For the terminal tool specifically, if the raw payload is a bare string
    (a common model failure mode — emitting ``ls -la`` instead of
    ``{"command": "ls -la"}``), we extract it as the ``command`` argument
    so the command executes instead of producing a parse error.
    """
    if raw_arguments is None:
        return {}, json.dumps(
            {
                "error": "Missing tool arguments",
                "message": (
                    "The tool was not executed. No arguments were provided. "
                    "Check the tool's parameter schema and provide a valid "
                    "JSON object with all required fields."
                ),
            },
            ensure_ascii=False,
        )

    # Bare-string fallback for terminal (issue #1647): models sometimes emit
    # a bare command string instead of a JSON object.  Extract it so the
    # command runs instead of producing a parse-error spiral.
    if function_name == "terminal" and isinstance(raw_arguments, str):
        stripped = raw_arguments.strip()
        if stripped and not stripped.startswith("{"):
            logger.info(
                "terminal tool received bare-string arguments — "
                "extracting as command (len=%d)",
                len(stripped),
            )
            return {"command": stripped}, None

    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        return {}, json.dumps(
            {
                "error": "Invalid tool arguments (JSON parse error)",
                "message": (
                    f"The tool was not executed. Its arguments could not be "
                    f"parsed as JSON: {exc.msg} at position {exc.pos}. "
                    f"Provide a valid JSON object with the correct field "
                    f"names and types for this tool."
                ),
            },
            ensure_ascii=False,
        )
    except TypeError:
        return {}, json.dumps(
            {
                "error": "Invalid tool arguments (wrong type)",
                "message": (
                    f"The tool was not executed. Its arguments must be a "
                    f"JSON string, got {type(raw_arguments).__name__}. "
                    f"Provide a valid JSON object with the correct field "
                    f"names for this tool."
                ),
            },
            ensure_ascii=False,
        )

    if isinstance(arguments, dict):
        # search_files preflight: a missing `pattern` argument is a frequent
        # model failure mode. Return a correction hint immediately so the agent
        # fixes the call on the same turn instead of blind-retrying.
        if function_name == "search_files" and "pattern" not in arguments:
            count = 0
            if agent is not None:
                count = getattr(agent, "_search_files_missing_pattern_count", 0) + 1
                agent._search_files_missing_pattern_count = count
            return {}, json.dumps(
                {
                    "error": "Missing required argument: pattern",
                    "tool": "search_files",
                    "missing": ["pattern"],
                    "correction": {
                        "pattern": "function_name_or_regex",
                        "target": "content",
                        "path": ".",
                    },
                    "message": (
                        "`search_files` requires a `pattern` argument. "
                        "Provide a regex/glob pattern and optionally "
                        "`target='files'` for filename searches. "
                        "Example: `search_files(pattern='*.py', target='files', path='.')`."
                    ),
                    "search_files_missing_pattern_count": count,
                    "argument_shape_spiral": count >= 3,
                },
                ensure_ascii=False,
            )
        return arguments, None

    # Parsed successfully but not a dict (e.g. a JSON list or scalar).
    return {}, json.dumps(
        {
            "error": "Invalid tool arguments (not a JSON object)",
            "message": (
                f"The tool was not executed. Its arguments parsed as "
                f"{type(arguments).__name__}, but a JSON object is required. "
                f'Wrap the arguments in curly braces: {{"field": value}}.'
            ),
        },
        ensure_ascii=False,
    )
