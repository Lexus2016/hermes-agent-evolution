"""Tests for the tool-memory store wiring into evolution_funnel (#1218).

Verifies that evolution_funnel.main() writes a tool-memory JSON record each
cycle as a live pipeline consumer, not dead code. Covers:
- record written to <tmp>/tool-memory/evolution_funnel.json
- tool_name and verified_count fields
- failure example recorded when merged=0 and selected>0
- no failure example when merged>0
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import evolution_funnel as ef  # noqa: E402


def _write(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj), encoding="utf-8")


def _run_main(tmp_path: Path, date: str, merged_prs) -> int:
    """Run the funnel main() with hermetic stage reports and a patched GH."""
    _write(
        tmp_path / "issues" / f"{date}.json",
        {"issues_created": [{"number": 1}, {"number": 2}]},
    )
    _write(
        tmp_path / "analysis" / f"{date}.json",
        {
            "selected_for_implementation": [
                {"issue_number": 1, "selected_reason": "score"},
                {"issue_number": 2, "selected_reason": "score"},
            ],
            "rejected": [],
        },
    )
    monkeypatch_holder = pytest.MonkeyPatch()
    monkeypatch_holder.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
    monkeypatch_holder.setenv("EVOLUTION_GH_REPO", "o/r")
    monkeypatch_holder.setattr(ef, "_gh_pr_list_merged", lambda *a, **k: merged_prs)
    try:
        return ef.main(["evolution_funnel.py", date])
    finally:
        monkeypatch_holder.undo()


def test_writes_tool_memory_record(tmp_path):
    d = "2026-07-23"
    # No GitHub merges -> merged=0, selected=2 -> failure example expected.
    rc = _run_main(tmp_path, d, merged_prs=None)
    assert rc == 0
    rec_file = tmp_path / "tool-memory" / "evolution_funnel.json"
    assert rec_file.exists(), "tool-memory record JSON was not written"
    data = json.loads(rec_file.read_text(encoding="utf-8"))
    assert data["tool_name"] == "evolution_funnel"
    assert data["verified_count"] >= 1
    assert data["capability"] == "per-cycle funnel metrics aggregation"
    # merged=0 and selected=2 -> a failure example should be recorded
    assert len(data["failure_examples"]) >= 1
    fe = data["failure_examples"][0]
    assert fe["scenario"] == "selected issues but none merged this cycle"
    assert fe["error"] == "merged=0"


def test_no_failure_when_merged(tmp_path):
    d = "2026-07-24"
    # Two evolution/issue-* PRs merged that day -> merged>0 -> no failure example.
    merged_prs = [
        {
            "number": 900,
            "headRefName": "evolution/issue-100-a",
            "mergedAt": f"{d}T10:00:00Z",
        },
        {
            "number": 901,
            "headRefName": "evolution/issue-101-b",
            "mergedAt": f"{d}T11:00:00Z",
        },
    ]
    rc = _run_main(tmp_path, d, merged_prs=merged_prs)
    assert rc == 0
    rec_file = tmp_path / "tool-memory" / "evolution_funnel.json"
    assert rec_file.exists()
    data = json.loads(rec_file.read_text(encoding="utf-8"))
    assert data["tool_name"] == "evolution_funnel"
    assert data["verified_count"] >= 1
    assert data["failure_examples"] == []
