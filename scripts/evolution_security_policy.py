#!/usr/bin/env python3
"""Pinned warn/block policy for the evolution security rules (#3568, #3518 2b).

The stakes ladder, the fail-safe default, the ``enforce_at`` floor, the scanned
text/path fields and the high-stakes path fragments were code inside the tree the
agent commits to, so a self-edit of ``evolution_invariant_filter`` could loosen
the policy that judges it. They live in ``evolution_security_policy.json`` and are
read from a copy pinned OUTSIDE the checkout (``$HERMES_HOME/evolution/pinned/``,
override ``$EVOLUTION_PINNED_SECURITY_POLICY``) that fails closed when missing or
shadowed -- like the invariant rulebook (#3560) and the evidence gates (#3567).
Deploys seed it through ``evolution_invariant_filter.install_pinned_seed``.
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
_STRING_KEYS = ("default_stakes", "default_enforce_at")
_LIST_KEYS = (
    "stakes_order",
    "default_text_fields",
    "default_path_fields",
    "high_stakes_path_fragments",
)


def seed_path() -> Path:
    """In-tree seed shipped with the checkout (deploy-time source)."""
    return Path(__file__).resolve().parent / _FILENAME


def pinned_policy_path() -> Path:
    override = os.environ.get(_ENV, "").strip()
    if override:
        return Path(override)
    from evolution_invariant_filter import _pinned_dir

    return _pinned_dir() / _FILENAME


def install_pinned_rules(force: bool = False) -> Path:
    """Copy the in-tree seed onto the pinned surface; idempotent, never silent."""
    seed, target = seed_path(), pinned_policy_path()
    if target.is_file() and not force:
        if target.read_bytes() == seed.read_bytes():
            return target
        raise ValueError(
            f"pinned security-policy surface at {target} differs from the in-tree "
            f"seed {seed}; pass force=True to replace it"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(seed.read_bytes())
    return target


def load_policy(path: Optional[Path] = None) -> dict[str, Any]:
    """Pinned warn/block policy; fail closed when the surface is unusable."""
    p = Path(path) if path is not None else pinned_policy_path()
    if path is None:
        for name in _SHADOW_NAMES:
            if (p.parent / name).is_file():
                raise ValueError(
                    f"pinned security-policy surface shadowed by in-tree copy "
                    f"{p.parent / name}"
                )
        if not p.is_file():
            raise ValueError(
                f"pinned security-policy surface missing at {p} - run "
                "'python scripts/evolution_invariant_filter.py --install-seed' on "
                f"this host (or set ${_ENV})"
            )
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read security policy {p}: {exc}") from exc
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("version"), int)
        or data["version"] < 1
    ):
        raise ValueError(f"security policy {p} missing a positive integer 'version'")
    for key in _STRING_KEYS:
        if not isinstance(data.get(key), str) or not data[key]:
            raise ValueError(f"security policy {p}: '{key}' must be a non-empty string")
    for key in _LIST_KEYS:
        value = data.get(key)
        if not isinstance(value, list) or not value:
            raise ValueError(f"security policy {p}: '{key}' must be a non-empty list")
        if not all(isinstance(v, str) and v for v in value):
            raise ValueError(f"security policy {p}: '{key}' must hold non-empty strings")
    for key in _STRING_KEYS:
        if data[key] not in data["stakes_order"]:
            raise ValueError(
                f"security policy {p}: {key}={data[key]!r} is not on the stakes ladder"
            )
    data["policy_source"] = str(p)
    return data
