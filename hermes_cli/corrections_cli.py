"""CLI surface for the lean Phase-1 correction-learning store.

Makes ``CorrectionLearner.unlearn`` a real runtime surface rather than a
library-only API, so the "reversible by construction" property is usable:

  * ``hermes corrections list`` — show the durable learned corrections (the
    provenance ledger): id, origin signal kind, promotion reason, and a short
    preview of what was learned.
  * ``hermes corrections unlearn <provenance_id>`` — reverse one durable
    correction: remove it from the per-profile memory store (so it stops
    re-injecting next session), drop its ledger entry, and reset the
    signature's recurrence evidence (so it does not snap straight back to
    durable on the next sighting).

The heavy lifting lives in ``agent.correction_learning.CorrectionLearner``;
this module is a thin, testable wrapper. ``run_list`` / ``run_unlearn`` take an
optional ``store_dir`` / ``memory_sink`` for isolation under test; the CLI
handlers resolve the real per-profile store and a live ``MemoryStore`` sink.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Optional


def register_cli(parser: argparse.ArgumentParser) -> None:
    """Attach ``list`` / ``unlearn`` subcommands to ``parser``."""
    sub = parser.add_subparsers(dest="corrections_command")

    lst = sub.add_parser("list", help="List durable learned corrections")
    lst.set_defaults(func=cmd_list)

    un = sub.add_parser(
        "unlearn",
        help="Reverse a durable learned correction by its provenance id",
    )
    un.add_argument(
        "provenance_id",
        help="Provenance id shown by `hermes corrections list`",
    )
    un.set_defaults(func=cmd_unlearn)


def _make_learner(store_dir: Optional[Path], memory_sink: Any):
    from agent.correction_learning import CorrectionLearner

    return CorrectionLearner(store_dir=store_dir, memory_sink=memory_sink)


def _default_memory_sink():
    """A live ``MemoryStore`` bound to the per-profile MEMORY.md.

    Needed so an unlearn actually removes the durable line from disk (stops
    re-injection), not just the provenance ledger. Best-effort: if the memory
    subsystem is unavailable, returns None and unlearn degrades to ledger +
    recurrence reset only.
    """
    try:
        from tools.memory_tool import MemoryStore

        store = MemoryStore()
        store.load_from_disk()
        return store
    except Exception:
        return None


def run_list(store_dir: Optional[Path] = None) -> int:
    """Print the durable provenance ledger. Returns a process exit code."""
    learner = _make_learner(store_dir, None)
    durable = learner.list_durable()
    if not durable:
        print("No durable learned corrections.")
        return 0
    print(f"{len(durable)} durable learned correction(s):")
    for e in durable:
        ctx = str(e.get("context", "")).replace("\n", " ")
        if len(ctx) > 80:
            ctx = ctx[:77] + "..."
        print(
            f"  {e.get('provenance_id')}  "
            f"[{e.get('origin_kind')}/{e.get('reason')}]  {ctx}"
        )
    return 0


def run_unlearn(
    provenance_id: str,
    *,
    store_dir: Optional[Path] = None,
    memory_sink: Any = None,
) -> int:
    """Reverse one durable correction. Returns 0 on success, 1 if unknown."""
    sink = memory_sink if memory_sink is not None else _default_memory_sink()
    learner = _make_learner(store_dir, sink)
    ok = learner.unlearn(provenance_id)
    if ok:
        print(
            f"Unlearned {provenance_id}: removed from the durable memory "
            "store, provenance ledger entry dropped, and recurrence evidence "
            "reset (it must re-accumulate fresh cross-session evidence to "
            "become durable again)."
        )
        return 0
    print(f"No durable correction with id {provenance_id!r}.")
    return 1


def cmd_list(args: argparse.Namespace) -> int:
    return run_list(store_dir=getattr(args, "store_dir", None))


def cmd_unlearn(args: argparse.Namespace) -> int:
    return run_unlearn(
        args.provenance_id,
        store_dir=getattr(args, "store_dir", None),
    )
