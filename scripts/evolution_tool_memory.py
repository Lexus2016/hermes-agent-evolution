#!/usr/bin/env python3
"""Tool-memory store — persistent capability/failure records (issue #1218, child of #1178).

JSON storage under ~/.hermes/evolution/tool-memory/<tool>.json. Schema: tool
name, capability, failure boundaries, composition partners, last-verified,
verified count, failure examples. Read/write/query functions. No LLM.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "ToolMemoryRecord",
    "ToolMemoryStore",
    "FailureExample",
    "load_store",
    "main",
]


@dataclass
class FailureExample:
    scenario: str
    error: str = ""
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scenario": self.scenario,
            "error": self.error,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "FailureExample":
        return cls(
            scenario=d.get("scenario", ""),
            error=d.get("error", ""),
            timestamp=d.get("timestamp", ""),
        )


@dataclass
class ToolMemoryRecord:
    tool_name: str
    capability: str = ""
    failure_boundaries: List[str] = field(default_factory=list)
    composition_partners: List[str] = field(default_factory=list)
    last_verified: str = ""
    verified_count: int = 0
    failure_examples: List[FailureExample] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "capability": self.capability,
            "failure_boundaries": list(self.failure_boundaries),
            "composition_partners": list(self.composition_partners),
            "last_verified": self.last_verified,
            "verified_count": self.verified_count,
            "failure_examples": [f.to_dict() for f in self.failure_examples],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ToolMemoryRecord":
        return cls(
            tool_name=d.get("tool_name", ""),
            capability=d.get("capability", ""),
            failure_boundaries=list(d.get("failure_boundaries", [])),
            composition_partners=list(d.get("composition_partners", [])),
            last_verified=d.get("last_verified", ""),
            verified_count=d.get("verified_count", 0),
            failure_examples=[
                FailureExample.from_dict(f)
                for f in d.get("failure_examples", [])
                if isinstance(f, dict)
            ],
        )


class ToolMemoryStore:
    """Persistent store of tool records, one JSON file per tool."""

    def __init__(self, storage_dir: Path) -> None:
        self.storage_dir = storage_dir
        self._records: Dict[str, ToolMemoryRecord] = {}

    @classmethod
    def load(cls, d: Path) -> "ToolMemoryStore":
        store = cls(d)
        if not d.is_dir():
            return store
        for p in sorted(d.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                r = ToolMemoryRecord.from_dict(data)
                if r.tool_name:
                    store._records[r.tool_name] = r
        return store

    def get(self, name: str) -> Optional[ToolMemoryRecord]:
        return self._records.get(name)

    def upsert(self, r: ToolMemoryRecord) -> None:
        self._records[r.tool_name] = r

    def save_record(self, name: str) -> Optional[Path]:
        r = self._records.get(name)
        if r is None:
            return None
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        p = self.storage_dir / f"{name.replace('/', '_').replace(':', '_')}.json"
        p.write_text(
            json.dumps(r.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
        return p

    def save_all(self) -> List[Path]:
        return [p for p in (self.save_record(n) for n in self._records) if p]

    def query(
        self, capability: str = "", composition_partner: str = ""
    ) -> List[ToolMemoryRecord]:
        cap, part = capability.lower(), composition_partner.lower()
        results = []
        for r in self._records.values():
            if cap and cap not in r.capability.lower():
                continue
            if part and not any(part in p.lower() for p in r.composition_partners):
                continue
            results.append(r)
        results.sort(key=lambda r: r.tool_name)
        return results

    def list_tools(self) -> List[str]:
        return sorted(self._records.keys())

    def count(self) -> int:
        return len(self._records)

    def record_failure(self, name: str, scenario: str, error: str = "") -> None:
        r = self._records.get(name)
        if r is None:
            r = ToolMemoryRecord(tool_name=name)
            self._records[name] = r
        r.failure_examples.append(FailureExample(scenario=scenario, error=error))

    def verify(self, name: str, capability: str = "") -> None:
        r = self._records.get(name)
        if r is None:
            r = ToolMemoryRecord(tool_name=name)
            self._records[name] = r
        if capability:
            r.capability = capability
        r.last_verified = datetime.now(timezone.utc).isoformat()
        r.verified_count += 1


def _default_dir() -> Path:
    env = os.environ.get("EVOLUTION_PROFILE_DIR", "").strip()
    if env:
        return Path(env) / "tool-memory"
    hh = os.environ.get("HERMES_HOME", "").strip()
    return (
        Path(hh) / "evolution" / "tool-memory"
        if hh
        else Path.home() / ".hermes" / "evolution" / "tool-memory"
    )


def load_store(d: Optional[Path] = None) -> ToolMemoryStore:
    return ToolMemoryStore.load(d or _default_dir())


def main(argv: List[str]) -> int:
    args = argv[1:]
    if not args or args[0] not in ("list", "query", "show", "verify"):
        print("usage: evolution_tool_memory.py {list|query|show|verify} [options]")
        return 2
    sd = _default_dir()
    if "--dir" in args and args.index("--dir") + 1 < len(args):
        sd = Path(args[args.index("--dir") + 1])
    mode = args[0]
    if mode == "list":
        s = ToolMemoryStore.load(sd)
        print(json.dumps({"count": s.count(), "tools": s.list_tools()}, indent=2))
        return 0
    if mode == "query":
        cap = (
            args[args.index("--capability") + 1]
            if "--capability" in args and args.index("--capability") + 1 < len(args)
            else ""
        )
        part = (
            args[args.index("--partner") + 1]
            if "--partner" in args and args.index("--partner") + 1 < len(args)
            else ""
        )
        s = ToolMemoryStore.load(sd)
        r = s.query(capability=cap, composition_partner=part)
        print(
            json.dumps({"count": len(r), "records": [x.to_dict() for x in r]}, indent=2)
        )
        return 0
    if mode == "show":
        if "--tool" not in args or args.index("--tool") + 1 >= len(args):
            print("usage: show --tool NAME", file=sys.stderr)
            return 2
        s = ToolMemoryStore.load(sd)
        r = s.get(args[args.index("--tool") + 1])
        if r is None:
            print(f"no record for {args[args.index('--tool') + 1]}", file=sys.stderr)
            return 1
        print(json.dumps(r.to_dict(), indent=2))
        return 0
    if mode == "verify":
        if "--tool" not in args or args.index("--tool") + 1 >= len(args):
            print("usage: verify --tool NAME [--capability DESC]", file=sys.stderr)
            return 2
        name = args[args.index("--tool") + 1]
        cap = (
            args[args.index("--capability") + 1]
            if "--capability" in args and args.index("--capability") + 1 < len(args)
            else ""
        )
        s = ToolMemoryStore.load(sd)
        s.verify(name, capability=cap)
        p = s.save_record(name)
        print(f"verified {name}, saved to {p}")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
