#!/usr/bin/env python3
"""Versioned thought-memory store — persist/retrieve the agent's own reasoning (issue #2900).

Thought-Retriever (arXiv:2604.12231): the pipeline's memory layer stores notes
and observations, but not the *intermediate reasoning* that produced them.
When the memory→skill promotion gate evaluates a candidate, it sees only the
final outcome — the reasoning that led there is gone. This module is Slice 1
of that pattern: a capture hook that snapshots intermediate reasoning at the
end of task cycles, a filter/dedup pass, and a versioned store plus a
deterministic retrieval path that later slices wire into the promotion gate.

Deterministic: no LLM, no network. Pure functions plus a thin CLI, matching
the ``evolution_*.py`` idiom (JSON to stdout, distinct exit codes).

Scope of this slice (honest): capture + store + retrieval. The retrieval
results are NOT yet fed into the promotion gate — that wiring (retrieval
extension + promotion-gate evidence) is the next increment, and this store is
the substrate it consumes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = "1"

#: A captured "thought" shorter than this is almost certainly noise (a bare
#: tool name, a single word) — filtering meaningless thoughts is step 2 of
#: the Thought-Retriever pattern.
MIN_THOUGHT_CHARS = 20

#: Upper bound per captured thought; giant reasoning dumps are not thoughts.
MAX_THOUGHT_CHARS = 4000

#: Store growth cap. Versioned and append-only, but unbounded history would
#: disperse retrieval signal — beyond this many entries the oldest are
#: archived (see :func:`_archive_overflow`).
MAX_STORE_ENTRIES = 500

#: Default number of thoughts returned by :func:`retrieve_thoughts`.
DEFAULT_RETRIEVE_K = 5

#: Distinctive prefix so thought files never collide with trajectory files.
_FILE_PREFIX = "thoughts-"


def _default_store_dir() -> Path:
    env = os.environ.get("EVOLUTION_PROFILE_DIR", "").strip()
    if env:
        return Path(env) / "thought_memory"
    hh = os.environ.get("HERMES_HOME", "").strip()
    return (
        Path(hh) / "evolution" / "thought_memory"
        if hh
        else Path.home() / ".hermes" / "evolution" / "thought_memory"
    )


_WS_RE = re.compile(r"\s+")


def normalize_thought(text: str) -> str:
    """Collapse whitespace and strip — the dedup basis for filtering."""
    return _WS_RE.sub(" ", text.strip())


def thought_dedup_key(text: str) -> str:
    """Opaque hash of the normalized text; two identical thoughts share it."""
    return hashlib.sha256(normalize_thought(text).encode("utf-8", "replace")).hexdigest()[:16]


@dataclass
class Thought:
    """One captured reasoning fragment, versioned across the store."""

    text: str
    captured_at: str = ""
    task_key: str = ""
    source: str = "task-cycle"
    version: int = 1
    dedup_key: str = ""

    def __post_init__(self) -> None:
        if not self.captured_at:
            self.captured_at = datetime.now(timezone.utc).isoformat()
        if not self.dedup_key:
            self.dedup_key = thought_dedup_key(self.text)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "text": self.text,
            "captured_at": self.captured_at,
            "task_key": self.task_key,
            "source": self.source,
            "version": self.version,
            "dedup_key": self.dedup_key,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Thought":
        return cls(
            text=str(data.get("text", "")),
            captured_at=str(data.get("captured_at", "")),
            task_key=str(data.get("task_key", "")),
            source=str(data.get("source", "task-cycle")),
            version=int(data.get("version", 1) or 1),
            dedup_key=str(data.get("dedup_key", "")),
        )


def _store_path(store_dir: Optional[Path] = None) -> Path:
    base = store_dir or _default_store_dir()
    return base / f"{_FILE_PREFIX}store.jsonl"


def _archive_path(store_dir: Optional[Path] = None) -> Path:
    base = store_dir or _default_store_dir()
    return base / f"{_FILE_PREFIX}archive.jsonl"


def load_thoughts(store_dir: Optional[Path] = None) -> List[Thought]:
    """Every thought in the store, oldest first. Corrupt lines are skipped."""
    path = _store_path(store_dir)
    if not path.exists():
        return []
    out: List[Thought] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("text"):
            out.append(Thought.from_dict(data))
    return out


def _next_version(thoughts: List[Thought]) -> int:
    return (max((t.version for t in thoughts), default=0) or 0) + 1


def _append(path: Path, thought: Thought) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(thought.to_dict(), sort_keys=True) + "\n")


def _archive_overflow(store_dir: Path) -> None:
    """Move the oldest half beyond MAX_STORE_ENTRIES into the archive file.

    The store stays bounded (retrieval signal is not dispersed across
    unbounded history); the archive keeps the history itself for audit.
    """
    thoughts = load_thoughts(store_dir)
    if len(thoughts) <= MAX_STORE_ENTRIES:
        return
    overflow = thoughts[: len(thoughts) - MAX_STORE_ENTRIES]
    keep = thoughts[len(thoughts) - MAX_STORE_ENTRIES :]
    archive = _archive_path(store_dir)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with open(archive, "a", encoding="utf-8") as fh:
        for t in overflow:
            fh.write(json.dumps(t.to_dict(), sort_keys=True) + "\n")
    path = _store_path(store_dir)
    path.write_text(
        "".join(json.dumps(t.to_dict(), sort_keys=True) + "\n" for t in keep),
        encoding="utf-8",
    )


def capture_thought(
    text: str,
    *,
    task_key: str = "",
    source: str = "task-cycle",
    store_dir: Optional[Path] = None,
) -> Optional[Thought]:
    """Capture one thought: filter meaningless, dedup identical, version, persist.

    Returns the stored :class:`Thought`, or ``None`` when the text is too
    short/long or is an exact duplicate of an already-stored thought.
    """
    normalized = normalize_thought(text)
    if len(normalized) < MIN_THOUGHT_CHARS or len(normalized) > MAX_THOUGHT_CHARS:
        return None
    base = store_dir or _default_store_dir()
    thoughts = load_thoughts(base)
    dedup_key = thought_dedup_key(normalized)
    if any(t.dedup_key == dedup_key for t in thoughts):
        return None
    thought = Thought(
        text=normalized,
        task_key=task_key,
        source=source,
        version=_next_version(thoughts),
        dedup_key=dedup_key,
    )
    _append(_store_path(base), thought)
    _archive_overflow(base)
    return thought


def retrieve_thoughts(
    query: str,
    *,
    k: int = DEFAULT_RETRIEVE_K,
    store_dir: Optional[Path] = None,
) -> List[Thought]:
    """Return the top-k thoughts most relevant to ``query``.

    Deterministic token-overlap scoring over the normalized text — no LLM, no
    embeddings. This is the retrieval substrate; later slices feed it to the
    promotion gate as reasoning evidence.
    """
    q_tokens = set(normalize_thought(query).lower().split())
    if not q_tokens:
        return []
    scored: List[tuple] = []
    for t in load_thoughts(store_dir):
        t_tokens = set(normalize_thought(t.text).lower().split())
        if not t_tokens:
            continue
        overlap = len(q_tokens & t_tokens) / len(q_tokens | t_tokens)
        if overlap > 0:
            scored.append((overlap, t))
    scored.sort(key=lambda x: (-x[0], x[1].version))
    return [t for _, t in scored[:k]]


def stats(store_dir: Optional[Path] = None) -> Dict[str, Any]:
    thoughts = load_thoughts(store_dir)
    versions = [t.version for t in thoughts]
    return {
        "schema_version": SCHEMA_VERSION,
        "count": len(thoughts),
        "max_version": max(versions, default=0),
        "sources": sorted({t.source for t in thoughts}),
    }


def _usage() -> str:
    return (
        "usage: evolution_thought_memory.py <command> [args]\n"
        "  capture <text> [--task-key K] [--source S]\n"
        "  retrieve <query> [--k N]\n"
        "  stats\n"
        "  Exit 0 ok, 2 bad input, 1 nothing to report."
    )


def _opt(args: List[str], name: str, default: str = "") -> str:
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return default


def main(argv: Optional[List[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or "--help" in args or "-h" in args:
        print(_usage())
        return 0 if args else 2

    cmd = args[0]

    if cmd == "capture":
        if len(args) < 2:
            print(_usage(), file=sys.stderr)
            return 2
        thought = capture_thought(
            args[1],
            task_key=_opt(args, "--task-key"),
            source=_opt(args, "--source", "task-cycle"),
        )
        if thought is None:
            print("captured: none (filtered or duplicate)", file=sys.stderr)
            return 1
        print(json.dumps(thought.to_dict(), indent=2, sort_keys=True))
        return 0

    if cmd == "retrieve":
        if len(args) < 2:
            print(_usage(), file=sys.stderr)
            return 2
        k = DEFAULT_RETRIEVE_K
        if "--k" in args:
            try:
                k = int(_opt(args, "--k"))
            except ValueError:
                k = DEFAULT_RETRIEVE_K
        hits = retrieve_thoughts(args[1], k=k)
        print(json.dumps([t.to_dict() for t in hits], indent=2, sort_keys=True))
        return 0 if hits else 1

    if cmd == "stats":
        print(json.dumps(stats(), indent=2, sort_keys=True))
        return 0

    print(_usage(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
