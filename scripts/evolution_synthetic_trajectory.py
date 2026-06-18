#!/usr/bin/env python3
"""Synthetic trajectory generator for skill evolution (#317).

Hermes already captures *live* conversation trajectories
(``agent/trajectory.py`` -> ``trajectory_samples.jsonl``), but the evolution
pipeline pays for an expensive live tool call behind every training signal.
This module produces *labeled synthetic episodes* deterministically — no model,
no live tool calls — that ROUND-TRIP the exact JSONL schema
``agent.trajectory.save_trajectory`` writes, so downstream consumers (extract /
dedup / evaluator) treat synthetic and live episodes uniformly.

Schema contract (must match ``agent.trajectory`` + ``agent_runtime_helpers``):
  * A persisted entry is ``{conversations, timestamp, model, completed}``.
  * ``conversations`` is a ShareGPT list of ``{"from", "value"}`` messages with
    ``from`` in {system, human, gpt, tool}.
  * ``system`` carries the function-calling preamble with ``<tools>``.
  * every ``gpt`` turn contains a ``<think>...</think>`` block; tool calls are
    wrapped in ``<tool_call>\n{json}\n</tool_call>``.
  * a ``tool`` turn wraps each result in ``<tool_response>\n{json}\n</tool_response>``
    where the json is ``{tool_call_id, name, content}``.

The episode dict additionally carries a ``labels`` field (the supervised signal
the evolution loop keys on). ``labels`` is NOT part of the persisted trajectory
schema — callers strip it (or pass only ``conversations``/``model``/``completed``
into ``save_trajectory``), keeping the on-disk format byte-identical to live.

Framework:
  * ``generate_episode(scenario, seed_params)`` — dispatches on the SCENARIOS
    registry; deterministic given ``seed_params``.
  * ``validate_episode_schema(episode)`` — gate before feeding the pipeline.
  * ``generate_batch(scenario, n, base_seed)`` — N distinct deterministic episodes.

SCOPE (minimal slice, #317): framework + ONE deterministic scenario,
``tool_augmented_planning``. Further scenario types (hierarchical decomposition,
multi-constraint scheduling, long-horizon execution) are a follow-up; they only
need to register a generator function in ``SCENARIOS``.

CLI (so an evolution skill can call it from the terminal tool):
    evolution_synthetic_trajectory.py scenarios
    evolution_synthetic_trajectory.py gen <scenario> [--seed N]
    evolution_synthetic_trajectory.py batch <scenario> --n N [--base-seed N] [--out FILE]
Pure functions are import-safe for unit tests.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime
from typing import Any, Callable, Dict, List

# --- schema constants (kept in sync with agent.agent_runtime_helpers) -------
_VALID_FROMS = {"system", "human", "gpt", "tool"}
_REQUIRED_ENTRY_KEYS = {"conversations", "timestamp", "model", "completed"}
_SYNTHETIC_MODEL = "synthetic/deterministic-v1"

# The function-calling preamble agent_runtime_helpers prepends to every
# trajectory. Tools are injected per-episode at the {tools} marker.
_SYSTEM_PREAMBLE = (
    "You are a function calling AI model. You are provided with function "
    "signatures within <tools> </tools> XML tags. You may call one or more "
    "functions to assist with the user query. If available tools are not "
    "relevant in assisting with user query, just respond in natural "
    "conversational language. Don't make assumptions about what values to plug "
    "into functions. After calling & executing the functions, you will be "
    "provided with function results within <tool_response> </tool_response> "
    "XML tags. Here are the available tools:\n"
    "<tools>\n{tools}\n</tools>\n"
    "For each function call return a JSON object, with the following pydantic "
    "model json schema for each:\n"
    "{{'title': 'FunctionCall', 'type': 'object', 'properties': {{'name': "
    "{{'title': 'Name', 'type': 'string'}}, 'arguments': {{'title': "
    "'Arguments', 'type': 'object'}}}}, 'required': ['name', 'arguments']}}\n"
    "Each function call should be enclosed within <tool_call> </tool_call> XML "
    "tags.\nExample:\n<tool_call>\n{{'name': <function-name>,'arguments': "
    "<args-dict>}}\n</tool_call>"
)


# --- ShareGPT message builders (match agent_runtime_helpers wire format) -----
def _system_msg(tools: List[Dict[str, Any]]) -> Dict[str, str]:
    tools_json = "\n".join(json.dumps(t, ensure_ascii=False) for t in tools)
    return {"from": "system", "value": _SYSTEM_PREAMBLE.format(tools=tools_json)}


def _human_msg(text: str) -> Dict[str, str]:
    return {"from": "human", "value": text}


def _gpt_msg(think: str, tool_calls: List[Dict[str, Any]] | None = None,
             content: str = "") -> Dict[str, str]:
    """A ``gpt`` turn. Always carries a <think> block (matching the live
    converter, which injects an empty one if reasoning is absent)."""
    parts: List[str] = [f"<think>\n{think}\n</think>"]
    if content:
        parts.append(content)
    for call in (tool_calls or []):
        parts.append(
            "<tool_call>\n"
            + json.dumps({"name": call["name"], "arguments": call["arguments"]},
                         ensure_ascii=False)
            + "\n</tool_call>"
        )
    return {"from": "gpt", "value": "\n".join(parts).rstrip()}


def _tool_msg(results: List[Dict[str, Any]]) -> Dict[str, str]:
    """A ``tool`` turn wrapping each result, mirroring the live converter's
    ``{tool_call_id, name, content}`` payload."""
    blocks: List[str] = []
    for r in results:
        payload = {
            "tool_call_id": r.get("tool_call_id", ""),
            "name": r["name"],
            "content": r["content"],
        }
        blocks.append("<tool_response>\n"
                      + json.dumps(payload, ensure_ascii=False)
                      + "\n</tool_response>")
    return {"from": "tool", "value": "\n".join(blocks)}


# --- scenario generators ----------------------------------------------------
# A scenario generator is ``(rng, params) -> (conversations, labels, completed)``.
# It must use ONLY ``rng`` (a seeded random.Random) for any variation so the
# episode is reproducible from ``seed_params``.

# Deterministic content pools — small, self-contained, no external data.
_PROJECTS = ["billing-service", "auth-gateway", "search-index", "media-pipeline",
             "notification-hub", "report-engine"]
_GOALS = [
    "find every TODO older than 30 days and group them by owner",
    "locate the function that builds the cache key and add a unit test for it",
    "produce a dependency report for the payment module",
    "find the slowest endpoint and propose one optimization",
    "audit the repo for hard-coded credentials and list the files",
]
_TOOLS_LIBRARY = {
    "list_files": {
        "name": "list_files",
        "description": "List files under a directory, optionally filtered by glob.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "glob": {"type": "string"},
            },
            "required": ["path"],
        },
    },
    "search_code": {
        "name": "search_code",
        "description": "Search file contents for a regex pattern.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["pattern"],
        },
    },
    "read_file": {
        "name": "read_file",
        "description": "Read a file's contents.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}


def _scenario_tool_augmented_planning(rng: random.Random, params: Dict[str, Any]):
    """A tool-augmented planning episode: the agent decomposes a goal into an
    explicit plan, then executes it with simulated tool calls/responses.

    Deterministic given ``rng``. Emits a labeled, schema-valid conversation
    exercising system -> human -> (gpt plan) -> gpt+tool turns -> final gpt.
    """
    project = rng.choice(_PROJECTS)
    goal = rng.choice(_GOALS)

    tools = [_TOOLS_LIBRARY["list_files"], _TOOLS_LIBRARY["search_code"],
             _TOOLS_LIBRARY["read_file"]]

    # 1. explicit hierarchical plan the model commits to up front.
    plan_steps = [
        f"List the files in the {project} repository to map the structure.",
        "Search the code for the relevant pattern.",
        "Read the most relevant file to confirm the finding.",
        "Summarize the result for the user.",
    ]

    convs: List[Dict[str, str]] = [_system_msg(tools)]
    convs.append(_human_msg(f"In the {project} repo, {goal}."))

    # gpt turn 1: state the plan (no tool call yet) — the "planning" signal.
    plan_text = "Plan:\n" + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(plan_steps))
    convs.append(_gpt_msg(
        think=(f"The user wants me to {goal} in {project}. I'll decompose this "
               "into list -> search -> read -> summarize and execute each step "
               "with the available tools."),
        content=plan_text,
    ))

    # A deterministic pattern derived from the goal (no NLP, just a stable token).
    pattern = "TODO" if "TODO" in goal else goal.split()[0].rstrip(".,").upper()

    tools_used: List[str] = []

    # step 1: list_files
    convs.append(_gpt_msg(
        think="Step 1: map the repository layout.",
        tool_calls=[{"name": "list_files",
                     "arguments": {"path": f"src/{project}", "glob": "**/*.py"}}],
    ))
    listing = [f"src/{project}/{n}.py" for n in ("handlers", "cache", "models", "utils")]
    convs.append(_tool_msg([{"tool_call_id": "call_1", "name": "list_files",
                             "content": {"files": listing}}]))
    tools_used.append("list_files")

    # step 2: search_code
    convs.append(_gpt_msg(
        think=f"Step 2: search for '{pattern}' across the listed files.",
        tool_calls=[{"name": "search_code",
                     "arguments": {"pattern": pattern, "path": f"src/{project}"}}],
    ))
    hit_file = listing[rng.randrange(len(listing))]
    hit_line = 10 + rng.randrange(90)
    convs.append(_tool_msg([{"tool_call_id": "call_2", "name": "search_code",
                             "content": {"matches": [{"file": hit_file,
                                                       "line": hit_line,
                                                       "text": f"# {pattern}: follow up"}]}}]))
    tools_used.append("search_code")

    # step 3: read_file
    convs.append(_gpt_msg(
        think=f"Step 3: read {hit_file} to confirm the match in context.",
        tool_calls=[{"name": "read_file", "arguments": {"path": hit_file}}],
    ))
    convs.append(_tool_msg([{"tool_call_id": "call_3", "name": "read_file",
                             "content": {"path": hit_file,
                                         "excerpt": f"...\n# {pattern}: follow up\n..."}}]))
    tools_used.append("read_file")

    # final gpt turn: the answer (no tool call) — closes the episode.
    convs.append(_gpt_msg(
        think="All steps executed; I can now answer the user.",
        content=(f"Done. I found the relevant {pattern} reference at "
                 f"{hit_file}:{hit_line} in {project} and confirmed it by "
                 "reading the file."),
    ))

    labels = {
        "scenario": "tool_augmented_planning",
        "success": True,
        "plan_steps": plan_steps,
        "tools_used": tools_used,
        "goal": goal,
        "project": project,
        "synthetic": True,
    }
    return convs, labels, True


# --- registry + framework ---------------------------------------------------
ScenarioFn = Callable[[random.Random, Dict[str, Any]],
                      "tuple[List[Dict[str, str]], Dict[str, Any], bool]"]

SCENARIOS: Dict[str, ScenarioFn] = {
    "tool_augmented_planning": _scenario_tool_augmented_planning,
}


def generate_episode(scenario: str, seed_params: Dict[str, Any]) -> Dict[str, Any]:
    """Generate one labeled synthetic episode.

    ``seed_params`` must contain ``seed`` (int); the same params yield the same
    episode (timestamp excepted — it is wall-clock and intentionally non-seeded,
    matching the live writer). Raises ``KeyError`` for an unknown scenario.
    """
    if scenario not in SCENARIOS:
        raise KeyError(f"unknown scenario: {scenario!r}; known: {sorted(SCENARIOS)}")
    seed = seed_params.get("seed", 0)
    rng = random.Random(f"{scenario}:{seed}")
    convs, labels, completed = SCENARIOS[scenario](rng, seed_params)
    return {
        "conversations": convs,
        "timestamp": datetime.now().isoformat(),
        "model": _SYNTHETIC_MODEL,
        "completed": completed,
        "labels": labels,
    }


def validate_episode_schema(episode: Dict[str, Any]) -> bool:
    """Return True iff ``episode`` round-trips the persisted trajectory schema.

    Checks the canonical entry keys plus the ShareGPT message shape. The extra
    ``labels`` key is permitted (it is stripped before persistence)."""
    if not isinstance(episode, dict):
        return False
    if not _REQUIRED_ENTRY_KEYS.issubset(episode.keys()):
        return False
    if not isinstance(episode["model"], str) or not episode["model"]:
        return False
    if not isinstance(episode["timestamp"], str) or not episode["timestamp"]:
        return False
    if not isinstance(episode["completed"], bool):
        return False
    convs = episode["conversations"]
    if not isinstance(convs, list) or not convs:
        return False
    for msg in convs:
        if not isinstance(msg, dict) or set(msg.keys()) != {"from", "value"}:
            return False
        if msg["from"] not in _VALID_FROMS or not isinstance(msg["value"], str):
            return False
    # First turn is the system preamble, by convention of the live converter.
    if convs[0]["from"] != "system":
        return False
    return True


def generate_batch(scenario: str, n: int, base_seed: int = 0) -> List[Dict[str, Any]]:
    """Generate ``n`` distinct deterministic episodes for ``scenario``.

    Episode ``i`` uses ``seed = base_seed + i``, so the batch is reproducible
    and the episodes differ from one another."""
    if n < 0:
        raise ValueError("n must be >= 0")
    return [generate_episode(scenario, {"seed": base_seed + i}) for i in range(n)]


def to_trajectory_entry(episode: Dict[str, Any]) -> Dict[str, Any]:
    """Strip the synthetic-only ``labels`` field, leaving the byte-identical
    persisted trajectory entry (``conversations``/``timestamp``/``model``/
    ``completed``)."""
    return {k: v for k, v in episode.items() if k != "labels"}


# --- CLI --------------------------------------------------------------------
def _cmd_scenarios(_args: argparse.Namespace) -> int:
    print(json.dumps(sorted(SCENARIOS), ensure_ascii=False))
    return 0


def _cmd_gen(args: argparse.Namespace) -> int:
    try:
        ep = generate_episode(args.scenario, {"seed": args.seed})
    except KeyError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(json.dumps(ep, ensure_ascii=False))
    return 0


def _cmd_batch(args: argparse.Namespace) -> int:
    try:
        eps = generate_batch(args.scenario, n=args.n, base_seed=args.base_seed)
    except KeyError as e:
        print(str(e), file=sys.stderr)
        return 2
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    # Persist the trajectory entries (labels stripped) so the output file is a
    # drop-in for the live trajectory_samples.jsonl pipeline.
    lines = [json.dumps(to_trajectory_entry(ep), ensure_ascii=False) for ep in eps]
    if args.out:
        with open(args.out, "a", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
        print(f"wrote {len(lines)} episodes to {args.out}")
    else:
        for line in lines:
            print(line)
    return 0


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="evolution_synthetic_trajectory.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("scenarios", help="list registered scenario names")

    p_gen = sub.add_parser("gen", help="generate one episode to stdout")
    p_gen.add_argument("scenario")
    p_gen.add_argument("--seed", type=int, default=0)

    p_batch = sub.add_parser("batch", help="generate N episodes")
    p_batch.add_argument("scenario")
    p_batch.add_argument("--n", type=int, required=True)
    p_batch.add_argument("--base-seed", type=int, default=0)
    p_batch.add_argument("--out", default=None,
                         help="append to FILE (trajectory_samples.jsonl-compatible)")

    args = parser.parse_args(argv[1:])
    if args.cmd == "scenarios":
        return _cmd_scenarios(args)
    if args.cmd == "gen":
        return _cmd_gen(args)
    if args.cmd == "batch":
        return _cmd_batch(args)
    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
