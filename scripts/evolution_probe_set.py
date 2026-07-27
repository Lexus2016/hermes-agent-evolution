#!/usr/bin/env python3
"""Regression probe-set format + storage convention (#1355, child of #1308).

AgentDevel (arXiv:2601.04620) shows that aggregate "did the score go up?" promotion
decisions ship regressions: a total can rise while specific previously-working cases
break. The fix is **example-level flip tracking** — re-run a skill/plugin against a
*fixed regression probe set* and compute per-example P→F (pass→fail = regression) and
F→P (fail→pass = fix) deltas. Promotion is gated on F→P gains dominating and P→F
staying below threshold.

This module defines the **data layer** for that mechanism:

* ``Probe`` / ``ProbeSet`` — typed dataclasses describing a fixed regression probe set.
* JSON serialization (``to_dict`` / ``from_dict`` / ``save`` / ``load``).
* Filesystem storage convention: ``~/.hermes/evolution/probes/{skill_name}.json``.

It deliberately does NOT implement the flip-gate engine (Child B), merge wiring
(Child C), or version metadata enforcement (Child D) — those are separate slices.

Design mirrors the other ``evolution_*.py`` modules: pure, typed, import-safe
functions + a thin deterministic CLI, with no network calls and no side effects
beyond filesystem I/O on explicit ``save``.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = "1.0"

# Default storage directory. Override via ``store_dir`` parameter on save/load.
DEFAULT_PROBE_DIR = Path.home() / ".hermes" / "evolution" / "probes"


@dataclass
class Probe:
    """A single regression probe — one example to re-run after a skill change.

    ``expected_behavior`` is a *behavioral description* (not a hard assertion)
    because regression probes are meant for model-graded comparison, not exact
    string matching. The grader (Child B) will compare the old vs new output
    against this description.
    """

    id: str
    input: str
    expected_behavior: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Probe":
        return cls(
            id=str(data.get("id", "")),
            input=str(data.get("input", "")),
            expected_behavior=str(data.get("expected_behavior", "")),
        )


@dataclass
class ProbeSet:
    """A fixed regression probe set for one skill/plugin.

    The set is *frozen* once created: the same probes must be re-run against
    every version of the skill so that P→F / F→P deltas are meaningful. Version
    bumps (adding/removing probes) create a NEW file, not a mutation of the old
    one — the old file is the baseline for comparison.
    """

    skill_name: str
    probes: List[Probe] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        # Normalize any raw dicts in ``probes`` into ``Probe`` objects so callers
        # can pass ``[{"id": ..., "input": ...}]`` directly.
        normalized: List[Probe] = []
        for p in self.probes:
            if isinstance(p, Probe):
                normalized.append(p)
            elif isinstance(p, dict):
                normalized.append(Probe.from_dict(p))
        self.probes = normalized

    @property
    def probe_ids(self) -> List[str]:
        return [p.id for p in self.probes]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "schema_version": self.schema_version,
            "probe_count": len(self.probes),
            "probes": [p.to_dict() for p in self.probes],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProbeSet":
        if not isinstance(data, dict):
            raise ValueError("ProbeSet.from_dict requires a dict")
        skill_name = str(data.get("skill_name", ""))
        if not skill_name:
            raise ValueError("ProbeSet requires 'skill_name'")
        raw_probes = data.get("probes", [])
        if not isinstance(raw_probes, list):
            raw_probes = []
        return cls(
            skill_name=skill_name,
            probes=[Probe.from_dict(p) for p in raw_probes if isinstance(p, dict)],
            schema_version=str(data.get("schema_version", SCHEMA_VERSION)),
        )


# ---------------------------------------------------------------------------
# Filesystem storage convention
# ---------------------------------------------------------------------------


def _probe_path(skill_name: str, store_dir: Optional[Path] = None) -> Path:
    """Resolve the canonical filesystem path for a skill's probe set."""
    base = store_dir or DEFAULT_PROBE_DIR
    safe_name = skill_name.replace("/", "_").replace(" ", "_")
    return base / f"{safe_name}.json"


def save_probe_set(
    probe_set: ProbeSet,
    store_dir: Optional[Path] = None,
) -> Path:
    """Persist a ``ProbeSet`` to ``{store_dir}/{skill_name}.json``.

    Creates ``store_dir`` if it does not exist. Returns the path written.
    """
    path = _probe_path(probe_set.skill_name, store_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(probe_set.to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


def load_probe_set(
    skill_name: str,
    store_dir: Optional[Path] = None,
) -> Optional[ProbeSet]:
    """Load a ``ProbeSet`` from the conventional path.

    Returns ``None`` when no probe set exists for ``skill_name`` — the caller
    (flip-gate engine, Child B) treats this as "no baseline, cannot gate."
    """
    path = _probe_path(skill_name, store_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return ProbeSet.from_dict(data)


# ---------------------------------------------------------------------------
# CLI — minimal, deterministic, no network
# ---------------------------------------------------------------------------


def _usage() -> str:
    return (
        "Usage:\n"
        "  evolution_probe_set.py show <skill_name>   # print probe set as JSON\n"
        "  evolution_probe_set.py list                # list stored probe sets\n"
    )


def main(argv: List[str]) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(_usage())
        return 0 if len(argv) >= 2 else 2

    cmd = argv[1]
    if cmd == "list":
        probe_dir = DEFAULT_PROBE_DIR
        if not probe_dir.exists():
            print("[]")
            return 0
        files = sorted(probe_dir.glob("*.json"))
        names = [f.stem for f in files]
        print(json.dumps(names, indent=2))
        return 0

    if cmd == "show":
        if len(argv) < 3:
            print("Error: 'show' requires a <skill_name>", file=sys.stderr)
            return 2
        skill_name = argv[2]
        ps = load_probe_set(skill_name)
        if ps is None:
            print(
                f"No probe set found for '{skill_name}' in {DEFAULT_PROBE_DIR}",
                file=sys.stderr,
            )
            return 1
        print(json.dumps(ps.to_dict(), indent=2, sort_keys=True))
        return 0

    print(f"Unknown command: {cmd}", file=sys.stderr)
    print(_usage(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
