#!/usr/bin/env python3
"""Shared pinned-surface contract for evolution gates (#3518, #3567).

A pinned surface is versioned data the gate judges against, living OUTSIDE the
agent-writable checkout (``$HERMES_HOME/evolution/pinned/``) so a self-edit
cannot weaken the checks that govern it. The default load fails closed when the
surface is missing or shadowed by an in-tree copy next to it; ``install`` seeds
it from the in-tree file at deploy time, idempotently, refusing divergence
unless forced. No IO beyond the one file, no network, no clock.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional


def pinned_dir() -> Path:
    """``$HERMES_HOME/evolution/pinned``, else ``~/.hermes/evolution/pinned``."""
    home = os.environ.get("HERMES_HOME", "").strip() or str(Path.home() / ".hermes")
    return Path(home) / "evolution" / "pinned"


def _path(filename: str, override_env: str) -> Path:
    override = os.environ.get(override_env, "").strip()
    return Path(override) if override else pinned_dir() / filename


def resolve(
    filename: str, override_env: str, shadow_names: tuple[str, ...], what: str
) -> Path:
    """Pinned file for the default load; fail closed when missing or shadowed."""
    pinned = _path(filename, override_env)
    for name in shadow_names:
        shadow = pinned.parent / name
        if shadow.is_file():
            raise ValueError(f"pinned {what} surface shadowed by in-tree copy {shadow}")
    if not pinned.is_file():
        raise ValueError(
            f"pinned {what} surface missing at {pinned} - run "
            f"'python scripts/evolution_invariant_filter.py --install-seed' on this host "
            f"(or set ${override_env})"
        )
    return pinned


def install(
    filename: str, override_env: str, seed: Path, *, force: bool = False
) -> Path:
    """Copy one in-tree seed onto its pinned surface. Idempotent; refuses divergence."""
    if not seed.is_file():
        raise ValueError(f"in-tree {filename} seed missing at {seed}")
    target = _path(filename, override_env)
    if target.is_file() and not force and target.read_bytes() != seed.read_bytes():
        raise ValueError(
            f"pinned surface at {target} differs from the in-tree seed {seed}; "
            f"pass force=True to replace it"
        )
    if not target.is_file() or force:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(seed.read_bytes())
    return target


def load_json(
    *,
    filename: str,
    override_env: str,
    shadow_names: tuple[str, ...],
    what: str,
    path: Optional[Path] = None,
) -> tuple[dict, Path]:
    """Load a pinned JSON surface. An explicit ``path`` is read as-is; default fails closed."""
    source = (
        Path(path)
        if path is not None
        else resolve(filename, override_env, shadow_names, what)
    )
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"pinned {what} surface {source} is unreadable: {exc}"
        ) from exc
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("version"), int)
        or data["version"] < 1
    ):
        raise ValueError(
            f"pinned {what} surface {source} missing a positive integer 'version'"
        )
    return data, source
