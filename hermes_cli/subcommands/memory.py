"""``hermes memory`` subcommand parser.

Extracted from ``hermes_cli/main.py:main()`` (god-file Phase 2 follow-up).
Handler injected to avoid importing ``main``.
"""

from __future__ import annotations

from typing import Callable


def build_memory_parser(subparsers, *, cmd_memory: Callable) -> None:
    """Attach the ``memory`` subcommand to ``subparsers``."""
    memory_parser = subparsers.add_parser(
        "memory",
        help="Configure external memory provider",
        description=(
            "Set up and manage external memory provider plugins.\n\n"
            "Available providers: honcho, openviking, mem0, hindsight,\n"
            "holographic, retaindb, byterover.\n\n"
            "Only one external provider can be active at a time.\n"
            "Built-in memory (MEMORY.md/USER.md) is always active."
        ),
    )
    memory_sub = memory_parser.add_subparsers(dest="memory_command")
    _setup_parser = memory_sub.add_parser(
        "setup", help="Interactive provider selection and configuration"
    )
    _setup_parser.add_argument(
        "provider",
        nargs="?",
        default=None,
        help="Provider to configure directly (e.g. honcho), skipping the picker",
    )
    memory_sub.add_parser("status", help="Show current memory provider config")
    memory_sub.add_parser("off", help="Disable external provider (built-in only)")
    _score_parser = memory_sub.add_parser(
        "score",
        help="Score episodic memory importance for recent turns (#752)",
    )
    _score_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="How many recent events to show (default 10)",
    )

    # Staleness detection (#797): run MemoryStalenessDetector over the current
    # memory corpus and print a markdown report of flagged/consolidable notes.
    _stale_parser = memory_sub.add_parser(
        "stale",
        help="Detect stale, duplicated, and low-quality memory notes",
    )
    _stale_parser.add_argument(
        "--max-age-days",
        type=int,
        default=None,
        help="Override the age threshold (days). Older notes are flagged AGE.",
    )
    _stale_parser.add_argument(
        "--min-content-length",
        type=int,
        default=None,
        help="Override the minimum content length (chars). Shorter notes are "
        "flagged LOW_QUALITY.",
    )
    _stale_parser.add_argument(
        "--duplicate-jaccard-threshold",
        type=float,
        default=None,
        help="Override the Jaccard similarity threshold for DUPLICATE flags.",
    )

    # Conflict detection (#908): flag pairs of notes that claim different
    # values for the same topic instead of silently letting the agent trust
    # whichever one it read last.
    _conflicts_parser = memory_sub.add_parser(
        "conflicts",
        help="Detect memory notes with contradictory claims about the same topic",
    )
    _conflicts_parser.add_argument(
        "--topic-similarity-threshold",
        type=float,
        default=None,
        help="Override the Jaccard similarity threshold for 'same topic' (default 0.6).",
    )
    _conflicts_parser.add_argument(
        "--value-similarity-threshold",
        type=float,
        default=None,
        help="Override the Jaccard similarity threshold below which values "
        "count as 'different' (default 0.5).",
    )
    _reset_parser = memory_sub.add_parser(
        "reset",
        help="Erase all built-in memory (MEMORY.md and USER.md)",
    )
    _reset_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip confirmation prompt",
    )
    _reset_parser.add_argument(
        "--target",
        choices=["all", "memory", "user"],
        default="all",
        help="Which store to reset: 'all' (default), 'memory', or 'user'",
    )
    memory_parser.set_defaults(func=cmd_memory)
