#!/usr/bin/env python3
"""Test that evolution_funnel wires in the trajectory logger (#1215).

Verifies that a trajectory JSON file is written to
``<EVOLUTION_PROFILE_DIR>/trajectories/`` when ``evolution_funnel.main()``
runs, and that the trajectory contains an entry with ``tool='evolution_funnel'``.
Also tests resilience: missing stage modules must not crash the funnel.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make scripts/ importable so `from evolution_trajectory_logger import ...`
# (used inside evolution_funnel.main) and `import evolution_funnel` both work.
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import evolution_funnel as ef  # noqa: E402


def _write_stage_reports(evolution_dir: Path, date: str) -> None:
    """Write minimal analysis + integration stage reports for ``date``.

    compute_funnel reads issues/<date>.json, analysis/<date>.json,
    integration/<date>.json, and introspection/<date>.json — all optional and
    all default to empty/0. We provide analysis + integration so the funnel
    record has non-trivial selected/merged/skipped fields.
    """
    (evolution_dir / "analysis").mkdir(parents=True, exist_ok=True)
    (evolution_dir / "integration").mkdir(parents=True, exist_ok=True)
    (evolution_dir / "analysis" / f"{date}.json").write_text(
        json.dumps({
            "selected_for_implementation": [
                {"issue_number": 1215, "selected_reason": "test"}
            ],
            "rejected": [{"reason_code": "dup"}],
        }),
        encoding="utf-8",
    )
    (evolution_dir / "integration" / f"{date}.json").write_text(
        json.dumps({"merged": [{"issue_number": 1215}], "skipped": []}),
        encoding="utf-8",
    )


def test_trajectory_wiring(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """main() writes a trajectory JSON containing an evolution_funnel entry."""
    date = "2099-01-02"
    monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
    monkeypatch.setenv("EVOLUTION_FUNNEL_DATE", date)
    # Prevent any real GitHub network calls: _gh_pr_list_merged -> None makes
    # merged_evolution_issue_ids return None (fail-open to self-report).
    monkeypatch.setattr(ef, "_gh_pr_list_merged", lambda *a, **k: None, raising=False)

    _write_stage_reports(tmp_path, date)

    rc = ef.main(["funnel", date])
    assert rc == 0, f"evolution_funnel.main returned {rc}"

    traj_dir = tmp_path / "trajectories"
    traj_files = list(traj_dir.glob("*.json"))
    assert traj_files, f"no trajectory JSON written under {traj_dir}"

    # The funnel writes <date>_funnel.json (session_id='funnel').
    found_entry = False
    for f in traj_files:
        data = json.loads(f.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        for entry in data.get("entries", []):
            if entry.get("tool") == "evolution_funnel":
                found_entry = True
                assert entry.get("result_status") == "success"
                break
        if found_entry:
            break
    assert found_entry, (
        f"no trajectory entry with tool='evolution_funnel' in {traj_files}"
    )


def test_funnel_resilience_missing_modules(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Missing optional modules must not crash the funnel — lazy imports are
    wrapped in try/except/pass, so a broken/absent trajectory logger (or any
    other sidecar) leaves main() returning 0 and the metrics line intact."""
    date = "2099-01-03"
    monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
    monkeypatch.setenv("EVOLUTION_FUNNEL_DATE", date)
    monkeypatch.setattr(ef, "_gh_pr_list_merged", lambda *a, **k: None, raising=False)
    # Sabotage the trajectory logger import path so the lazy import inside
    # main() raises ImportError — the try/except/pass must swallow it.
    monkeypatch.setitem(
        sys.modules,
        "evolution_trajectory_logger",
        None,  # type: ignore[arg-type]
    )

    _write_stage_reports(tmp_path, date)

    rc = ef.main(["funnel", date])
    assert rc == 0, "funnel must not crash when a sidecar module is unavailable"

    # The core metrics.jsonl must still be written even if sidecars fail.
    metrics = tmp_path / "metrics.jsonl"
    assert metrics.exists(), "metrics.jsonl missing despite sidecar failure"
    rec = json.loads(metrics.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec["date"] == date
