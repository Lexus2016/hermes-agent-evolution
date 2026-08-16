# -*- coding: utf-8 -*-
"""C-A-F loop — Context-Action-Feedback (Issue #2258, Slice B, parent #2247).

Sandbox-test candidates against a verifier, accumulate execution-grounded
experience, update the #2257 RoutingTable from sandbox feedback; cumulative
regret (pass-rate gap to the per-dimension best model) is the quality metric.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

Verifier = Union[Callable[[str, str], bool], str, Path]


def _default_base_dir() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home()
    except Exception:
        return Path.home() / ".hermes"


def default_experience_path() -> Path:
    return _default_base_dir() / "evolution" / "caf" / "experience.jsonl"


def default_routing_table_path() -> Path:
    return _default_base_dir() / "evolution" / "routing" / "table.json"


@dataclass
class CafRecord:
    """One execution-grounded observation: did *model* pass on *task_dim*?"""

    task_dim: str
    model: str
    passed: bool
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CafRecord":
        return cls(
            str(d.get("task_dim", "")), str(d.get("model", "")),
            bool(d.get("passed", False)), str(d.get("timestamp", "")),
        )


class CafSandboxVerifier:
    """Run an answer against a verifier; binary verdict (B1). ``verifier`` is
    a callable ``(task, answer) -> bool`` or a script path reading
    ``{"task", "answer"}`` JSON on stdin, exit 0 = pass (mirrors
    ``SandboxValidator`` in ``tool_synthesis.py``, kept simpler)."""

    timeout_seconds: float = 30.0

    def verify(self, task: str, answer: str, verifier: Verifier) -> bool:
        if callable(verifier):
            try:
                return bool(verifier(task, answer))
            except Exception as exc:
                logger.warning("C-A-F verifier callable failed: %s", exc)
                return False
        try:
            proc = subprocess.run(
                [sys.executable, str(verifier)],
                input=json.dumps({"task": task, "answer": answer}),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
            return proc.returncode == 0
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("C-A-F verifier script failed: %s", exc)
            return False


class CafExperience:
    """Append-only JSONL store of pass/fail records (B3), fail-open on read."""

    def __init__(self, path: Optional[Union[str, Path]] = None) -> None:
        self.path = Path(path) if path is not None else default_experience_path()

    def record(self, task_dim: str, model: str, passed: bool) -> CafRecord:
        rec = CafRecord(task_dim=task_dim, model=model, passed=passed)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
        return rec

    def load(self) -> List[CafRecord]:
        """Load records; corrupt/blank lines are skipped, never fatal."""
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        records: List[CafRecord] = []
        for line in lines:
            try:
                records.append(CafRecord.from_dict(json.loads(line)))
            except (json.JSONDecodeError, ValueError):
                continue
        return records


def cumulative_regret(records: List[CafRecord], model: str) -> float:
    """Regret of *model* vs the per-dimension best model (B2, pure function):
    trial-weighted average of ``p_best - p_model`` per dimension — 0.0 for
    the best model (or no/unknown records)."""

    by_dim: Dict[str, Dict[str, List[bool]]] = {}
    for r in records:
        by_dim.setdefault(r.task_dim, {}).setdefault(r.model, []).append(r.passed)
    total_trials = 0
    regret_sum = 0.0
    for models in by_dim.values():
        trials = models.get(model)
        if not trials:
            continue
        p_model = sum(trials) / len(trials)
        p_best = max(sum(t) / len(t) for t in models.values())
        regret_sum += (p_best - p_model) * len(trials)
        total_trials += len(trials)
    if total_trials == 0:
        return 0.0
    return regret_sum / total_trials


def run_caf_cycle(
    task_dim: str,
    task: str,
    candidates: Dict[str, str],
    verifier: Verifier,
    table: Any,
    experience: Optional[CafExperience] = None,
) -> Dict[str, Any]:
    """Sandbox-test each candidate (model → answer); record outcomes in the
    experience store AND ``table.record_outcome``. Returns
    ``{"task_dim", "results", "best_model", "regret"}``."""

    exp = experience if experience is not None else CafExperience()
    sandbox = CafSandboxVerifier()
    results: List[Dict[str, Any]] = []
    for model, answer in candidates.items():
        passed = sandbox.verify(task, answer, verifier)
        exp.record(task_dim=task_dim, model=model, passed=passed)
        table.record_outcome(model=model, dimension=task_dim, success=passed)
        results.append({"model": model, "passed": passed})
    all_records = exp.load()
    return {
        "task_dim": task_dim,
        "results": results,
        "best_model": table.best_model(task_dim),
        "regret": {m: cumulative_regret(all_records, m) for m in candidates},
    }


def save_routing_table(table: Any, path: Optional[Union[str, Path]] = None) -> Path:
    """Persist a RoutingTable as JSON at *path* (default: profile-aware)."""
    p = Path(path) if path is not None else default_routing_table_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(table.to_json(), encoding="utf-8")
    return p


def load_routing_table(path: Optional[Union[str, Path]] = None) -> Any:
    """Load a RoutingTable from *path*; fail-open to an empty table."""
    from tools.model_routing_table import RoutingTable
    p = Path(path) if path is not None else default_routing_table_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return RoutingTable.from_dict(data)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.debug("C-A-F routing table load failed (using empty table): %s", exc)
        return RoutingTable(models=[])
