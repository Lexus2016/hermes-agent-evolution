#!/usr/bin/env python3
"""Checked lowering for memory-bounded agent tool execution (#77, SkillEffect).

S1 of the #77 decomposition: a PURE, pre-execution cap checker for declared
tool invocations. Before a tool call is trusted, its DECLARED inputs are
checked against per-tool resource caps — reject-early on oversized or
over-arity invocations instead of discovering the problem mid-execution.

Scope is deliberately OFF-CORE: this module does NOT touch the live tool
invocation path (that is the S3 bounded-execution slice, which needs its own
design review). It is the deterministic gate the evolution pipeline applies to
skills it generates/adopts — see ``evolution_skill_lint.py`` (the S2 call
site), which extracts ``declared_inputs:`` blocks from SKILL.md frontmatter
and runs them through :func:`check_input_caps` before a skill is trusted for
reuse. This mirrors SkillEffect's checked lowering: rebuild each proposed
invocation from the declared (immutable) inputs and reject it if it exceeds
capacity — without trusting the model's claim about what it will do.

Caps are CONFIG DATA in ``scripts/evolution_tool_caps.json`` (overridable via
the ``EVOLUTION_TOOL_CAPS`` env var pointing at another JSON file); the
builtin table below is the code fallback so the gate never silently weakens
to "no caps".

Pure functions + explicit IO boundary (JSON caps file + CLI) so it is
import-safe and unit-testable.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Code fallback for the caps table — the shipped JSON
# (``scripts/evolution_tool_caps.json``) is the editable source of truth and
# overrides this when both exist.
BUILTIN_CAPS: Dict[str, Dict[str, int]] = {
    "default": {"max_bytes": 1_048_576, "max_args": 16},
    "read_file": {"max_bytes": 262_144, "max_args": 4},
    "write_file": {"max_bytes": 524_288, "max_args": 6},
    "search_files": {"max_bytes": 131_072, "max_args": 8},
    "terminal": {"max_bytes": 65_536, "max_args": 8},
    "patch": {"max_bytes": 1_048_576, "max_args": 6},
    "web_extract": {"max_bytes": 262_144, "max_args": 5},
    "web_search": {"max_bytes": 8_192, "max_args": 5},
    "skill_view": {"max_bytes": 131_072, "max_args": 4},
    "delegate_task": {"max_bytes": 262_144, "max_args": 12},
}

DEFAULT_CAPS_PATH = Path(__file__).resolve().parent / "evolution_tool_caps.json"
ENV_CAPS_OVERRIDE = "EVOLUTION_TOOL_CAPS"  # path to an alternate caps JSON


def load_caps(path: Optional[str] = None) -> Dict[str, Dict[str, int]]:
    """Load the per-tool caps table.

    Resolution order: explicit ``path`` argument > ``EVOLUTION_TOOL_CAPS``
    env var > the shipped ``scripts/evolution_tool_caps.json`` > builtin
    table. Never raises: any load/parse failure degrades to ``BUILTIN_CAPS``
    so a malformed caps file can never disable the gate (it would be better
    to fail loudly, but a broken gate must not crash the pipeline — the CLI
    and lint both surface the fallback via their normal output paths).
    """
    candidates: List[Optional[str]] = [path]
    if path is None:
        env = os.environ.get(ENV_CAPS_OVERRIDE)
        if env:
            candidates.append(env)
        candidates.append(str(DEFAULT_CAPS_PATH))
    for cand in candidates:
        if not cand:
            continue
        try:
            data = json.loads(Path(cand).read_text(encoding="utf-8-sig"))
            if isinstance(data, dict) and data:
                return {str(k): dict(v) for k, v in data.items()}
        except (OSError, ValueError, TypeError):
            continue
    return dict(BUILTIN_CAPS)


def declared_input_size(args: Optional[Dict[str, Any]]) -> int:
    """Serialized size of the DECLARED input values, in characters.

    Strings/bytes count their length directly; everything else counts its
    ``str()`` length. This is a heuristic over what the invoker DECLARED it
    will pass — the point of checked lowering is to bound the claim before
    execution, not to measure the real payload (that is S3's job).
    """
    total = 0
    for key, value in (args or {}).items():
        total += len(key)
        if isinstance(value, (str, bytes)):
            total += len(value)
        else:
            total += len(str(value))
    return total


def check_input_caps(
    tool_name: str,
    args: Optional[Dict[str, Any]],
    caps: Optional[Dict[str, Dict[str, int]]] = None,
) -> List[Dict[str, str]]:
    """Reject-early: do the DECLARED inputs exceed the per-tool caps?

    ``caps`` is a ``{tool_name: {"max_bytes": int, "max_args": int}}`` table
    (see :func:`load_caps`). A tool with no explicit entry is checked against
    the ``"default"`` entry — unknown tools are not themselves violations
    (the cap table is a curated allow-list of known tools; the skill-lint
    layer decides whether an unknown tool is suspicious).

    Returns a list of violation dicts (``[]`` = OK):
      - ``kind: "too_many_args"``   — arg count exceeds ``max_args``
      - ``kind: "input_too_large"`` — declared input size exceeds ``max_bytes``
    Each dict carries ``tool``, ``detail``, ``declared`` and ``cap`` for
    deterministic reporting.
    """
    cap_table = caps if caps is not None else load_caps()
    entry = cap_table.get(tool_name) or cap_table.get("default") or {}
    argv = dict(args or {})
    out: List[Dict[str, str]] = []

    try:
        max_args = int(entry.get("max_args") or 0)
    except (TypeError, ValueError):
        max_args = 0
    try:
        max_bytes = int(entry.get("max_bytes") or 0)
    except (TypeError, ValueError):
        max_bytes = 0

    if max_args and len(argv) > max_args:
        out.append({
            "tool": tool_name,
            "kind": "too_many_args",
            "detail": f"{len(argv)} declared args exceed the cap of {max_args}",
            "declared": str(len(argv)),
            "cap": str(max_args),
        })
    if max_bytes:
        size = declared_input_size(argv)
        if size > max_bytes:
            out.append({
                "tool": tool_name,
                "kind": "input_too_large",
                "detail": (
                    f"declared input size {size} chars exceeds the cap of "
                    f"{max_bytes} for {tool_name}"
                ),
                "declared": str(size),
                "cap": str(max_bytes),
            })
    return out


def main(argv: List[str]) -> int:
    args = argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(
            "usage: evolution_tool_cap_check.py <tool_name> <args-json> "
            "[--caps <caps-file.json>]",
            file=sys.stderr,
        )
        return 2
    tool = args[0]
    caps_path: Optional[str] = None
    if "--caps" in args:
        idx = args.index("--caps")
        if idx + 1 < len(args):
            caps_path = args[idx + 1]
    try:
        args_json = (
            json.loads(args[1])
            if len(args) > 1 and not args[1].startswith("--")
            else {}
        )
    except json.JSONDecodeError as exc:
        print(f"[evolution-tool-cap-check] malformed args JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(args_json, dict):
        print("[evolution-tool-cap-check] args JSON must be an object", file=sys.stderr)
        return 2

    violations = check_input_caps(tool, args_json, caps=load_caps(caps_path))
    if not violations:
        print(f"[evolution-tool-cap-check] OK — {tool} declared inputs within caps")
        return 0
    print(
        f"[evolution-tool-cap-check] {len(violations)} violation(s):", file=sys.stderr
    )
    for v in violations:
        print(f"  - [{v['kind']}] {v['tool']}: {v['detail']}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
