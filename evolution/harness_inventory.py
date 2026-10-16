#!/usr/bin/env python3
"""Harness component inventory CLI (issue #39, slice 1).

Prints the file-level inventory of editable harness components (skills,
system-prompt assembly, tool modules, config schema) as markdown. The
inventory is the "component observability" foundation for the forge
pipeline: every entry is a file path, so edits are diffable and revertible
at single-file granularity.

Usage:
    python evolution/harness_inventory.py [ROOT]

ROOT defaults to the repository root (discovered from this file's path).

Same convention as ``evolution/detect_mode.py`` — a plain, runnable script
wrapping a ``evolution/lib`` module.
"""

from evolution.lib.harness_inventory import main

if __name__ == "__main__":
    raise SystemExit(main())
