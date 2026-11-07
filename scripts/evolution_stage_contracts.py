#!/usr/bin/env python3
"""Pinned stage contracts for closed-loop evolution (#3569, #3518 slice 2c).

The edit stage ("what must an edit declare, and which edits may be reverted")
and the skill stage ("what is replayed, against which baseline") used to be
constants in ``evolution_edit_contracts`` / ``evolution_skill_contracts`` --
code inside the tree the agent commits to, so a self-edit could rewrite the
contract it is judged by. Both read ``evolution_stage_contracts.json`` from a
copy pinned OUTSIDE the checkout (``$HERMES_HOME/evolution/pinned/``, override
``$EVOLUTION_PINNED_STAGE_CONTRACTS``), failing closed when the surface is
missing or shadowed, like the rulebook (#3560), the evidence gates (#3567) and
the security policy (#3568). Deploys seed it via
``evolution_invariant_filter.install_pinned_seed``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

_FILENAME = "evolution_stage_contracts.json"
_ENV = "EVOLUTION_PINNED_STAGE_CONTRACTS"
_SHADOW_NAMES = (
    "evolution_stage_contracts.in-tree.json",
    "evolution_stage_contracts.override.json",
)
_EDIT_LISTS = ("required_fields", "predicted_impact_keys", "safe_revert_edit_types")
_SKILL_LISTS = ("scanned_roots",)
_SKILL_STRINGS = ("baseline", "skill_filename")


def seed_path() -> Path:
    """In-tree seed shipped with the checkout (deploy-time source)."""
    return Path(__file__).resolve().parent / _FILENAME


def pinned_contracts_path() -> Path:
    override = os.environ.get(_ENV, "").strip()
    if override:
        return Path(override)
    from evolution_invariant_filter import _pinned_dir

    return _pinned_dir() / _FILENAME


def install_pinned_rules(force: bool = False) -> Path:
    """Copy the in-tree seed onto the pinned surface; idempotent, never silent."""
    seed, target = seed_path(), pinned_contracts_path()
    if target.is_file() and not force:
        if target.read_bytes() == seed.read_bytes():
            return target
        raise ValueError(
            f"pinned stage-contract surface at {target} differs from the in-tree "
            f"seed {seed}; pass force=True to replace it"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(seed.read_bytes())
    return target


def _require_lists(section: dict[str, Any], keys: tuple[str, ...], where: str) -> None:
    for key in keys:
        value = section.get(key)
        if not isinstance(value, list) or not value:
            raise ValueError(f"stage contracts {where}: '{key}' must be a non-empty list")
        if not all(isinstance(v, str) and v for v in value):
            raise ValueError(f"stage contracts {where}: '{key}' must hold non-empty strings")


def load_stage_contracts(path: Optional[Path] = None) -> dict[str, Any]:
    """Pinned stage contracts; fail closed when the surface is unusable."""
    p = Path(path) if path is not None else pinned_contracts_path()
    if path is None:
        for name in _SHADOW_NAMES:
            if (p.parent / name).is_file():
                raise ValueError(
                    f"pinned stage-contract surface shadowed by in-tree copy {p.parent / name}"
                )
        if not p.is_file():
            raise ValueError(
                f"pinned stage-contract surface missing at {p} - run "
                "'python scripts/evolution_invariant_filter.py --install-seed' on this host "
                f"(or set ${_ENV})"
            )
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read stage contracts {p}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("version"), int) or data["version"] < 1:
        raise ValueError(f"stage contracts {p} missing a positive integer 'version'")
    edit = data.get("edit_contract")
    if not isinstance(edit, dict):
        raise ValueError(f"stage contracts {p}: 'edit_contract' must be an object")
    _require_lists(edit, _EDIT_LISTS, f"{p} (edit_contract)")
    skill = data.get("skill_contract")
    if not isinstance(skill, dict):
        raise ValueError(f"stage contracts {p}: 'skill_contract' must be an object")
    _require_lists(skill, _SKILL_LISTS, f"{p} (skill_contract)")
    for key in _SKILL_STRINGS:
        if not isinstance(skill.get(key), str) or not skill[key]:
            raise ValueError(f"stage contracts {p}: skill_contract.{key} must be a non-empty string")
    if not isinstance(skill.get("refuse_symlinks"), bool):
        raise ValueError(f"stage contracts {p}: skill_contract.refuse_symlinks must be a bool")
    data["contracts_source"] = str(p)
    return data
