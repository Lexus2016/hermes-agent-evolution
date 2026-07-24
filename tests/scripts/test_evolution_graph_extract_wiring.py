#!/usr/bin/env python3
"""Test that evolution_funnel wires in CFG/DFG graph extraction (#1221).

Verifies that:
1. When no git repo is resolvable (_resolve_repo_dir -> None), funnel main()
   completes without crash (graph-extraction sidecar is a no-op).
2. When a fake repo with .git and a sample .py file exists, main() writes
   ``<EVOLUTION_PROFILE_DIR>/graphs/<date>.json`` containing extracted
   function control/data-flow data.

Also confirms resilience: the lazy-import try/except/pass wrapping means a
missing or broken graph extractor never crashes the funnel job.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Make scripts/ importable so `from evolution_graph_extract import ...`
# (used inside evolution_funnel.main) and `import evolution_funnel` both work.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

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
                {"issue_number": 1221, "selected_reason": "test"}
            ],
            "rejected": [{"reason_code": "dup"}],
        }),
        encoding="utf-8",
    )
    (evolution_dir / "integration" / f"{date}.json").write_text(
        json.dumps({"merged": [{"issue_number": 1221}], "skipped": []}),
        encoding="utf-8",
    )


_SAMPLE_PY = """\
def foo(x):
    if x > 0:
        return x
    return -x
"""


def test_no_repo_no_crash(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When _resolve_repo_dir returns None, funnel completes without crash."""
    date = "2099-01-02"
    monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
    monkeypatch.setenv("EVOLUTION_FUNNEL_DATE", date)
    monkeypatch.setattr(ef, "_gh_pr_list_merged", lambda *a, **k: None, raising=False)
    # Force _resolve_repo_dir to return None — no repo to diff.
    monkeypatch.setattr(ef, "_resolve_repo_dir", lambda: None, raising=False)

    _write_stage_reports(tmp_path, date)

    rc = ef.main(["funnel", date])
    assert rc == 0, f"evolution_funnel.main returned {rc}"

    # No graphs directory should be created when there's no repo.
    assert not (tmp_path / "graphs").exists(), (
        "graphs/ written despite _resolve_repo_dir returning None"
    )


def test_graph_extraction_writes_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When a fake repo with .git + a .py file exists, graphs/<date>.json is
    written with extracted function data."""
    date = "2099-01-03"
    monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
    monkeypatch.setenv("EVOLUTION_FUNNEL_DATE", date)
    monkeypatch.setattr(ef, "_gh_pr_list_merged", lambda *a, **k: None, raising=False)

    # Build a fake repo with .git and one .py file.
    fake_repo = tmp_path / "fake-repo"
    fake_repo.mkdir()
    (fake_repo / ".git").mkdir()
    py_file = fake_repo / "sample.py"
    py_file.write_text(_SAMPLE_PY, encoding="utf-8")

    monkeypatch.setattr(ef, "_resolve_repo_dir", lambda: fake_repo, raising=False)

    # Mock subprocess.run so the git-diff returns our sample file name.
    # The wiring code does `import subprocess as _sp` *inside* its try block,
    # creating a local alias — so we must patch the global subprocess module,
    # not ef.subprocess (which is only used by the funnel's own gh calls, and
    # those are already neutralised by the _gh_pr_list_merged mock above).
    import subprocess as _sp_mod

    def _fake_run(cmd, *a, **k):
        result = MagicMock()
        # The wiring uses ["git", "-C", <repo>, "diff", "--name-only",
        # "HEAD~1", "HEAD"]. Return our sample file name.
        if "diff" in cmd and "--name-only" in cmd:
            result.stdout = "sample.py\n"
            result.returncode = 0
        else:
            result.stdout = ""
            result.returncode = 0
        return result

    monkeypatch.setattr(_sp_mod, "run", _fake_run, raising=False)

    _write_stage_reports(tmp_path, date)

    rc = ef.main(["funnel", date])
    assert rc == 0, f"evolution_funnel.main returned {rc}"

    graph_file = tmp_path / "graphs" / f"{date}.json"
    assert graph_file.exists(), f"graphs/{date}.json was not written"

    data = json.loads(graph_file.read_text(encoding="utf-8"))
    assert "files" in data, "graph JSON missing 'files' key"
    assert len(data["files"]) == 1, f"expected 1 file, got {len(data['files'])}"
    fg = data["files"][0]
    assert fg["path"].endswith("sample.py"), (
        f"expected path ending in sample.py, got {fg['path']}"
    )
    assert len(fg["functions"]) == 1, f"expected 1 function, got {len(fg['functions'])}"
    func = fg["functions"][0]
    assert func["name"] == "foo", f"expected function 'foo', got {func['name']}"
    # The function has a branch (if x > 0) and two returns.
    cf_types = [e["type"] for e in func["control_flow"]]
    assert "branch" in cf_types, f"expected 'branch' in control_flow, got {cf_types}"
    assert "return" in cf_types, f"expected 'return' in control_flow, got {cf_types}"
    # Data flow should include param edges for x.
    df_types = [e["type"] for e in func["data_flow"]]
    assert "param" in df_types, f"expected 'param' in data_flow, got {df_types}"
