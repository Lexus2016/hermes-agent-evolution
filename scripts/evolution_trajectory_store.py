#!/usr/bin/env python3
"""Successful-trajectory store + positive-pattern extractor (#321).

The evolution pipeline already has a NEGATIVE-signal path: ``introspection_extract``
reads the agent's own traces and ``evolution_trace_miner`` turns recurring
*failures* into weakness records the issues stage consumes. What was missing is
the POSITIVE half of the same loop — a store over the agent's *successful*
trajectories, indexed by task type, plus an extractor that turns recurring
successful patterns into improvement proposals (AgentTrek arXiv:2412.09605:
synthesized successful trajectories bootstrap new capabilities).

This module is that positive-signal core. It does NOT capture or replay anything
live — per-step replay already exists (``agent.agent_judge.replay_trace_steps`` /
``score_replayed_trace``, PR #324) and negative mining already exists
(``evolution_trace_miner``, #248). The net-new slice is: index successful
trajectories by task type, query by type, and emit structured proposals from
recurring successful tool patterns.

Two sources (#1474)
-------------------
It was written against ``trajectory_samples.jsonl`` from
``agent.trajectory.save_trajectory``. **Nothing writes that file**:
``save_trajectories`` defaults to ``False`` and lands in the process CWD, so
this module read an empty path and had no production callers — dead in both
directions at once.

Since #1363 the live source is ``~/.hermes/evolution/trajectories/*.jsonl``,
written by ``agent/tool_call_capture.py``: one JSON object per turn, holding
redacted call metadata plus a task-level ``completed`` outcome.
:meth:`TrajectoryStore.from_capture_dir` reads that, and the CLI now defaults to
it; the legacy path still works when named explicitly.

The two sources classify differently by necessity. The capture stores **no
prompt text** — the task descriptor is user prose — so :func:`classify_task_type`
has nothing to read there and :func:`classify_by_tools` groups by what the
trajectory actually did instead. Arguably the better signal: a run that edits
files and executes code is coding work whether or not the request said "code".

Legacy schema, reused verbatim from ``save_trajectory``::

    {"conversations": [ {"from": "system", "value": ...},
                        {"from": "human",  "value": <task prompt>},
                        {"from": "gpt",    "value": "...<tool_call>{name,args}</tool_call>..."},
                        {"from": "tool",   "value": ...}, ... ],
     "timestamp": ..., "model": ..., "completed": true}

Tool calls are embedded as ``<tool_call>{...}</tool_call>`` XML inside ``gpt``
turns (the trajectory format produced by ``convert_to_trajectory_format``), so
this module's per-trajectory step view mirrors ``replay_trace_steps`` but reads
the on-disk ShareGPT shape rather than the in-memory ``role``/``content`` shape.

Design mirrors the sibling ``scripts/evolution_*.py`` helpers: pure functions +
a thin CLI, import-safe and unit-testable, deterministic, no LLM. Proposals use
the same envelope contract the issues stage already ingests (``title`` + ``body``
+ ``source``), with ``source="trajectory-success"`` to distinguish them from
research-generated and self-harness (negative) proposals.

Output: JSON proposals to stdout, and (in main) a sidecar
``<EVOLUTION_PROFILE_DIR>/success-patterns-latest.json`` the issues stage reads,
exactly mirroring ``evolution_trace_miner``'s ``weaknesses-latest.json``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

# A recurring successful tool pattern must appear in at least this many distinct
# successful trajectories of a task type to become a proposal.
DEFAULT_MIN_SUPPORT = 3

# Keyword → task type. First matching bucket wins (checked in this order). Kept
# deliberately small and obvious; the evolution loop can extend the vocabulary
# without touching the store/extract logic.
_TASK_KEYWORDS: List[Tuple[str, Tuple[str, ...]]] = [
    ("coding", ("code", "function", "bug", "implement", "refactor", "test",
                "compile", "python", "javascript", "class", "method", "patch",
                "write", "fix", "edit")),
    ("research", ("research", "paper", "papers", "arxiv", "investigate",
                  "compare", "survey", "literature", "study")),
    ("deployment", ("deploy", "production", "release", "rollout", "ship",
                    "kubernetes", "docker", "pipeline")),
]

_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def classify_task_type(prompt: Optional[str]) -> str:
    """Map a task prompt to a coarse task type. Pure + deterministic.

    Matching is on whole words (``\\b`` boundaries), not raw substrings, so a
    keyword like ``test`` does not spuriously fire on ``latest``. Returns
    ``"general"`` for an empty/None prompt or one matching no bucket, so every
    successful trajectory lands in exactly one index bucket.
    """
    if not prompt or not isinstance(prompt, str):
        return "general"
    low = prompt.lower()
    for task_type, keywords in _TASK_KEYWORDS:
        if any(re.search(r"\b" + re.escape(kw) + r"\b", low) for kw in keywords):
            return task_type
    return "general"


def _first_human_prompt(conversations: List[Dict[str, Any]]) -> str:
    """The first human/user turn's text from a ShareGPT conversation list.

    Tolerates both the on-disk ``from``/``value`` shape and a defensive
    ``role``/``content`` fallback. Never raises.
    """
    if not isinstance(conversations, list):
        return ""
    for msg in conversations:
        if not isinstance(msg, dict):
            continue
        role = msg.get("from") or msg.get("role")
        if role in {"human", "user"}:
            val = msg.get("value")
            if not isinstance(val, str):
                val = msg.get("content")
            return val if isinstance(val, str) else ""
    return ""


def _tool_names_in_value(value: Any) -> List[str]:
    """Extract tool names from the ``<tool_call>{...}</tool_call>`` blocks in a
    ``gpt`` turn's ``value``. Tolerates malformed JSON inside a block (skipped).
    """
    if not isinstance(value, str) or "<tool_call>" not in value:
        return []
    names: List[str] = []
    for raw in _TOOL_CALL_RE.findall(value):
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            continue
        name = obj.get("name") if isinstance(obj, dict) else None
        if isinstance(name, str) and name:
            names.append(name)
    return names


def trajectory_tools(conversations: List[Dict[str, Any]]) -> List[str]:
    """All tool names invoked across a trajectory's assistant turns, in order.

    This is the on-disk ShareGPT analogue of iterating ``replay_trace_steps`` and
    reading each step's ``tool_calls`` — same decision-step view, different stored
    shape (``from``/``value`` with embedded ``<tool_call>`` XML).
    """
    tools: List[str] = []
    if not isinstance(conversations, list):
        return tools
    for msg in conversations:
        if isinstance(msg, dict) and (msg.get("from") or msg.get("role")) == "gpt":
            tools.extend(_tool_names_in_value(msg.get("value")))
    return tools


def classify_by_tools(tools: List[str]) -> str:
    """Classify a trajectory by WHAT IT DID, not by how the task was worded.

    The #1363 capture stores no prompt text — deliberately, since the task
    descriptor is user prose — so :func:`classify_task_type` has nothing to read
    there. Classifying by the tools actually used is both possible and arguably
    better: a task that edits files and runs tests is coding work whether or not
    the person said the word "code".

    Ordered most specific first: deployment tooling implies deployment even
    when files were also edited, whereas an edit plus a test run without any
    deployment tooling is coding.
    """
    used = {t.lower() for t in tools}

    def _any(*names: str) -> bool:
        return any(n in used for n in names)

    if _any("browser_navigate", "web_search", "web_extract") and not _any(
        "patch", "write_file"
    ):
        return "research"
    if _any("patch", "write_file", "execute_code"):
        return "coding"
    if _any("read_file", "search_files", "repo_map"):
        return "exploration"
    if _any("terminal"):
        return "operations"
    return "general"


class TrajectoryStore:
    """In-memory index of successful trajectories, keyed by task type.

    Two sources, one index:

    * :meth:`from_jsonl` — the legacy ``trajectory_samples.jsonl`` ShareGPT
      format written by ``agent.save_trajectories``, classified by prompt text.
    * :meth:`from_capture_dir` — the ``*.jsonl`` records written by the #1363
      capture, classified by tools used (see :func:`classify_by_tools`), since
      those records carry no prompt text by design.

    Each indexed record is a small projection — ``task_type`` and the
    de-duplicated set of tools — so no raw prompt/answer text is retained
    beyond what classification needs.
    """

    def __init__(self) -> None:
        # task_type -> list of {"tools": List[str], "tool_set": frozenset}
        self._by_type: Dict[str, List[Dict[str, Any]]] = {}

    @classmethod
    def from_jsonl(cls, path: Any) -> "TrajectoryStore":
        """Build a store from a ``trajectory_samples.jsonl`` path.

        Missing file → empty store. Malformed lines (not JSON, missing
        ``conversations``, blank) are skipped, never raised. Entries explicitly
        flagged ``completed=False`` are skipped defensively even though the
        samples file is successful-only by contract.
        """
        store = cls()
        p = Path(path)
        if not p.exists():
            return store
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            return store
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(entry, dict):
                continue
            if entry.get("completed") is False:
                continue
            conversations = entry.get("conversations")
            if not isinstance(conversations, list):
                continue
            store._add(conversations)
        return store

    @classmethod
    def from_capture_dir(cls, directory: Any) -> "TrajectoryStore":
        """Build a store from the #1363 capture directory.

        Reads ``*.jsonl`` — one JSON object per turn — and indexes only turns
        recorded as successful. ``completed`` is tri-state there: ``True`` and
        ``False`` are recorded outcomes, and ABSENT means "not recorded" (the
        pre-#1363 cron-stage trajectories). Absent is skipped rather than
        assumed successful; a store of successful trajectories must not be
        padded with runs whose outcome nobody wrote down.
        """
        store = cls()
        d = Path(directory)
        if not d.is_dir():
            return store
        for path in sorted(d.glob("*.jsonl")):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(rec, dict) or rec.get("completed") is not True:
                    continue
                entries = rec.get("entries")
                if not isinstance(entries, list):
                    continue
                tools = [
                    str(e.get("tool"))
                    for e in entries
                    if isinstance(e, dict) and e.get("tool")
                ]
                if tools:
                    store._add_tools(tools, classify_by_tools(tools))
        return store

    def _add(self, conversations: List[Dict[str, Any]]) -> None:
        task_type = classify_task_type(_first_human_prompt(conversations))
        self._add_tools(trajectory_tools(conversations), task_type)

    def _add_tools(self, tools: List[str], task_type: str) -> None:
        self._by_type.setdefault(task_type, []).append(
            {"tools": tools, "tool_set": frozenset(tools)}
        )

    def count(self) -> int:
        """Total successful trajectories indexed."""
        return sum(len(v) for v in self._by_type.values())

    def task_types(self) -> List[str]:
        """Sorted list of task types present in the store."""
        return sorted(self._by_type)

    def by_type(self, task_type: str) -> List[Dict[str, Any]]:
        """All indexed records for ``task_type`` (empty list if none)."""
        return list(self._by_type.get(task_type, []))


def extract_success_patterns(
    store: TrajectoryStore, min_support: int = DEFAULT_MIN_SUPPORT
) -> List[Dict[str, Any]]:
    """Emit improvement proposals from recurring successful tool patterns.

    For each task type, a tool that appears in at least ``min_support`` distinct
    successful trajectories becomes one proposal: "in N/M successful <type> tasks
    the agent used `<tool>` — consider making this a default step / skill".
    This is the positive-signal analogue of ``mine_weaknesses``: pure +
    deterministic, operating only on the indexed projection (task type + tool
    sets), never raw trajectory content.

    Proposals use the issues-stage envelope contract (``title`` + ``body`` +
    ``source``) and are sorted by support descending so the issues stage triages
    the strongest patterns first.
    """
    proposals: List[Dict[str, Any]] = []
    for task_type in store.task_types():
        records = store.by_type(task_type)
        total = len(records)
        if total == 0:
            continue
        # Count distinct trajectories each tool appears in (per-trajectory
        # support, not raw call frequency — a retry loop must not inflate it).
        support: Dict[str, int] = {}
        for rec in records:
            for tool in rec["tool_set"]:
                support[tool] = support.get(tool, 0) + 1
        for tool, n in support.items():
            if n < min_support:
                continue
            fraction = round(n / total, 4)
            proposals.append({
                "source": "trajectory-success",
                "type": "default_step",
                "task_type": task_type,
                "tool": tool,
                "support": n,
                "total": total,
                "fraction": fraction,
                "title": f"[SUCCESS] {task_type} tasks consistently call `{tool}`",
                "body": (
                    f"In {n}/{total} successful `{task_type}` trajectories the agent "
                    f"used `{tool}`. Consider promoting this into a default step, a "
                    f"skill, or a prompt hint for `{task_type}` tasks so the winning "
                    f"pattern is reinforced rather than rediscovered each run."
                ),
            })
    # Strongest patterns first; stable tie-break on (task_type, tool) for
    # deterministic output across runs.
    proposals.sort(key=lambda p: (-p["support"], p["task_type"], p["tool"]))
    return proposals


def format_proposals(proposals: List[Dict[str, Any]]) -> str:
    """One-line-per-proposal human summary for the issues-stage prompt / watchdog."""
    if not proposals:
        return "[evolution-trajectory-store] no recurring success patterns"
    lines = [f"[evolution-trajectory-store] {len(proposals)} success pattern(s):"]
    for p in proposals:
        lines.append(
            f"  - [{p['task_type']}] `{p['tool']}` in {p['support']}/{p['total']} "
            f"successful trajectories"
        )
    return "\n".join(lines)


def _iter_records(store: TrajectoryStore) -> Iterator[Dict[str, Any]]:  # pragma: no cover - convenience
    for task_type in store.task_types():
        for rec in store.by_type(task_type):
            yield {"task_type": task_type, **rec}


def _default_capture_dir() -> Path:
    """Where the #1363 capture writes, matching evolution_trajectory_logger."""
    import os

    env = os.environ.get("EVOLUTION_PROFILE_DIR", "").strip()
    if env:
        return Path(env) / "trajectories"
    hh = os.environ.get("HERMES_HOME", "").strip()
    return (
        Path(hh) / "evolution" / "trajectories"
        if hh
        else Path.home() / ".hermes" / "evolution" / "trajectories"
    )


def main(argv: List[str]) -> int:
    path = ""
    min_support = DEFAULT_MIN_SUPPORT
    for a in argv[1:]:
        if a.startswith("--min-support="):
            try:
                min_support = int(a.split("=", 1)[1])
            except ValueError:
                pass
        elif not a.startswith("--"):
            path = a

    # Default to the #1363 capture directory, which is what actually gets
    # written. The legacy trajectory_samples.jsonl is still accepted when named
    # explicitly, but nothing writes it: agent.save_trajectories defaults off
    # and lands in the process CWD. Reading it by default is why this module had
    # no live source (#1474).
    if path:
        store = TrajectoryStore.from_jsonl(path)
    else:
        capture_dir = _default_capture_dir()
        path = str(capture_dir)
        store = TrajectoryStore.from_capture_dir(capture_dir)
    proposals = extract_success_patterns(store, min_support=min_support)

    payload = {
        "source_file": path,
        "min_support": min_support,
        "trajectories_indexed": store.count(),
        "task_types": store.task_types(),
        "proposals": proposals,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))

    # Sidecar for the issues stage: <EVOLUTION_PROFILE_DIR>/success-patterns-latest.json,
    # mirroring evolution_trace_miner's weaknesses-latest.json. Best-effort.
    try:
        import os

        prof = os.environ.get("EVOLUTION_PROFILE_DIR")
        if prof:
            out = Path(prof) / "success-patterns-latest.json"
            out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:  # pragma: no cover - environment dependent
        pass
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
