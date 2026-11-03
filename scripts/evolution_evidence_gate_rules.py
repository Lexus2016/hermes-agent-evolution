#!/usr/bin/env python3
"""Pinned thresholds and markers for the evolution evidence gates (#3567).

Child of #3518. The gates carried their thresholds and markers as code inside the
tree the agent commits to, so a self-edit could weaken the gate that judges it.
The versioned data file (``evolution_evidence_gate_rules.json``) is now read from
a copy pinned OUTSIDE the checkout (``$HERMES_HOME/evolution/pinned/``, override
``$EVOLUTION_PINNED_EVIDENCE_RULES``), failing closed when that surface is missing
or shadowed, exactly like ``evolution_invariant_rules.json`` since #3560/#3563.
Deploys seed it through ``evolution_invariant_filter.install_pinned_seed``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

_FILENAME = "evolution_evidence_gate_rules.json"
_ENV = "EVOLUTION_PINNED_EVIDENCE_RULES"
_SHADOW_NAMES = (
    "evolution_evidence_gate_rules.in-tree.json",
    "evolution_evidence_gate_rules.override.json",
)


def seed_path() -> Path:
    """In-tree seed shipped with the checkout (deploy-time source)."""
    return Path(__file__).resolve().parent / _FILENAME


def pinned_rules_path() -> Path:
    override = os.environ.get(_ENV, "").strip()
    if override:
        return Path(override)
    from evolution_invariant_filter import _pinned_dir

    return _pinned_dir() / _FILENAME


def _resolve() -> Path:
    """Pinned surface, or a refusal: a missing/shadowed surface is not a pass."""
    pinned = pinned_rules_path()
    for name in _SHADOW_NAMES:
        shadow = pinned.parent / name
        if shadow.is_file():
            raise ValueError(f"pinned evidence-gate surface shadowed by in-tree copy {shadow}")
    if not pinned.is_file():
        raise ValueError(
            f"pinned evidence-gate surface missing at {pinned} - run "
            "'python scripts/evolution_invariant_filter.py --install-seed' on this host "
            f"(or set ${_ENV})"
        )
    return pinned


def install_pinned_rules(force: bool = False) -> Path:
    """Copy the in-tree seed onto the pinned surface; idempotent, never silent."""
    seed, target = seed_path(), pinned_rules_path()
    if target.is_file() and not force:
        if target.read_bytes() == seed.read_bytes():
            return target
        raise ValueError(
            f"pinned evidence-gate surface at {target} differs from the in-tree seed "
            f"{seed}; pass force=True to replace it"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(seed.read_bytes())
    return target


def load_rules(path: Optional[Path] = None) -> dict[str, Any]:
    """Pinned thresholds/markers; fail closed when the surface is unusable."""
    p = Path(path) if path is not None else _resolve()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read evidence-gate rules {p}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("version"), int) or data["version"] < 1:
        raise ValueError(f"evidence-gate rules {p} missing a positive integer 'version'")
    data["rules_source"] = str(p)
    return data
