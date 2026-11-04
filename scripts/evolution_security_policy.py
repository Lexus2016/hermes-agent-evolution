#!/usr/bin/env python3
"""Pinned warn/block policy for the evolution security rules (#3568, #3518 slice 2b).

Pinned OUTSIDE the checkout ($HERMES_HOME/evolution/pinned/, override
$EVOLUTION_PINNED_SECURITY_POLICY); fails closed when missing or shadowed.
Seeded via evolution_invariant_filter.install_pinned_seed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

_FILENAME = "evolution_security_policy.json"
_ENV = "EVOLUTION_PINNED_SECURITY_POLICY"
_SHADOW_NAMES = (
    "evolution_security_policy.in-tree.json",
    "evolution_security_policy.override.json",
)


def pinned_policy_path() -> Path:
    override = os.environ.get(_ENV, "").strip()
    if override:
        return Path(override)
    from evolution_invariant_filter import _pinned_dir

    return _pinned_dir() / _FILENAME


def install_pinned_rules(force: bool = False) -> Path:
    """Seed the pinned surface; idempotent, never silent."""
    seed, target = Path(__file__).resolve().parent / _FILENAME, pinned_policy_path()
    if target.is_file() and not force:
        if target.read_bytes() == seed.read_bytes():
            return target
        raise ValueError(
            f"pinned security-policy surface {target} differs from seed {seed}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(seed.read_bytes())
    return target


def load_policy(path: Optional[Path] = None) -> dict[str, Any]:
    """Pinned warn/block policy; fail closed when missing or shadowed."""
    p = Path(path) if path is not None else pinned_policy_path()
    if path is None:
        if any((p.parent / n).is_file() for n in _SHADOW_NAMES):
            raise ValueError(f"pinned security-policy surface shadowed in {p.parent}")
        if not p.is_file():
            raise ValueError(
                f"pinned security-policy surface missing at {p} (set ${_ENV})"
            )
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read security policy {p}: {exc}") from exc
    bad = (
        not isinstance(data, dict)
        or not isinstance(data.get("version"), int)
        or data["version"] < 1
        or not (
            data.get("stakes_order")
            and data.get("default_stakes")
            and data.get("default_enforce_at")
        )
    )
    if bad:
        raise ValueError(
            f"security policy {p} missing version or the warn/block fields"
        )
    data["policy_source"] = str(p)
    return data
