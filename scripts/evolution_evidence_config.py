#!/usr/bin/env python3
"""Pinned evidence-gate thresholds (#3518 slice 2a, #3567).

``evolution_replay_gate``, ``evolution_evidence_span_gate`` and
``evolution_evidence_type_gate`` judge proposals against thresholds and markers.
Those used to be constants inside the tree the agent commits to, so a self-edit
could weaken the evidence gate that governs it. They now live as data in
``evolution_evidence_thresholds.json`` and the default load reads the PINNED
copy outside the checkout (see ``evolution_pinned_surface``): a rewritten
in-tree copy cannot override it, and a missing or shadowed surface fails closed.

No network, no clock. The only IO is reading the one thresholds file.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from evolution_pinned_surface import install, load_json

_FILENAME = "evolution_evidence_thresholds.json"
_OVERRIDE_ENV = "EVOLUTION_PINNED_EVIDENCE"
_SHADOW_NAMES = (
    "evolution_evidence_thresholds.in-tree.json",
    "evolution_evidence_thresholds.override.json",
)
_WHAT = "evidence-gate"

_REQUIRED = ("replay", "span", "type")


def seed_path() -> Path:
    """The in-tree seed, next to this module."""
    return Path(__file__).resolve().parent / _FILENAME


def load_thresholds(path: Optional[Path] = None) -> dict[str, Any]:
    """Load the thresholds the evidence gates judge against.

    An explicit ``path`` is read as-is (tests, audits). The default load reads
    the pinned surface and fails closed when it is missing or shadowed.
    """
    data, source = load_json(
        filename=_FILENAME,
        override_env=_OVERRIDE_ENV,
        shadow_names=_SHADOW_NAMES,
        what=_WHAT,
        path=path,
    )
    missing = [key for key in _REQUIRED if not isinstance(data.get(key), dict)]
    if missing:
        raise ValueError(
            f"pinned evidence-gate surface {source} missing sections: {missing}"
        )
    data["source"] = str(source)
    return data


def install_pinned_thresholds(*, force: bool = False) -> Path:
    """Seed the pinned evidence-gate surface from the in-tree file."""
    return install(_FILENAME, _OVERRIDE_ENV, seed_path(), force=force)


def _compile(pattern: str, flags: int = 0) -> re.Pattern[str]:
    return re.compile(pattern, flags)


def span_patterns(thresholds: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Compiled patterns + limits for the evidence-span gate."""
    span = (thresholds or load_thresholds())["span"]
    return {
        "max_span": int(span["max_span"]),
        "url": _compile(span["url_pattern"]),
        "header": _compile(span["header_pattern"], re.MULTILINE),
        "quote": _compile(span["quote_pattern"], re.MULTILINE),
    }


def type_patterns(thresholds: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Compiled markers + the claim→evidence map for the evidence-type gate."""
    section = (thresholds or load_thresholds())["type"]
    return {
        "diff": _compile(section["diff_markers"], re.MULTILINE),
        "test_result": _compile(section["test_result_markers"], re.MULTILINE),
        "command_output": _compile(section["command_output_markers"], re.MULTILINE),
        "expected": {k: set(v) for k, v in section["expected_evidence"].items()},
        "closure_overrides": {
            k: set(v) for k, v in section["closure_overrides"].items()
        },
    }


def replay_config(thresholds: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Verdict map + certifiable grades for the recorded-input replay gate."""
    replay = (thresholds or load_thresholds())["replay"]
    return {
        "record_path": str(replay["record_path"]),
        "verdicts": dict(replay["verdicts"]),
        "certifiable": tuple(replay["certifiable"]),
    }
