"""CLI wiring tests for the research claim audit (#3521).

Proves the script the ``evolution-issues`` stage runs behaves as the stage
expects: a clean draft exits 0, a drifted figure exits 1 with the offending
line named, a missing evidence sidecar skips instead of blocking the pipeline,
and the result sidecar lands next to the report.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_claim_audit import EXIT_BAD_INPUT, EXIT_FAILED, EXIT_OK, main

URL = "https://arxiv.org/abs/2610.31372"
_SOURCE = "The harness reports 85.2% F1 on 60 trajectories (submitted 2026-10-22)."


def _write_report(tmp_path: Path, figure: str = "85.2") -> Path:
    report = tmp_path / "2026-10-26.md"
    report.write_text(
        "# Research Report - 2026-10-26\n\n"
        "### [IMPROVEMENT] Claim audit\n"
        f"- **Source**: {URL}\n\n"
        f"It reports {figure}% F1 on 60 trajectories, submitted 2026-10-22.\n",
        encoding="utf-8",
    )
    (tmp_path / "2026-10-26.sources.json").write_text(
        json.dumps({URL: _SOURCE}), encoding="utf-8"
    )
    return report


def test_clean_report_passes_and_writes_sidecar(tmp_path, capsys):
    report = _write_report(tmp_path)
    assert main(["evolution_claim_audit.py", str(report)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "unverified=0" in out
    payload = json.loads(
        (tmp_path / "2026-10-26.claim_audit.json").read_text(encoding="utf-8")
    )
    assert payload["passed"] is True
    assert payload["counts"]["verified"] >= 3


def test_drifted_figure_fails_the_stage_with_the_line_named(tmp_path, capsys):
    report = _write_report(tmp_path, figure="85.9")
    assert main(["evolution_claim_audit.py", str(report)]) == EXIT_FAILED
    out = capsys.readouterr().out
    assert "UNVERIFIED 85.9" in out and "It reports 85.9% F1" in out


def test_no_sidecar_skips_and_keeps_the_stage_alive(tmp_path):
    report = _write_report(tmp_path)
    (tmp_path / "2026-10-26.sources.json").unlink()
    assert main(["evolution_claim_audit.py", str(report)]) == EXIT_OK
    payload = json.loads(
        (tmp_path / "2026-10-26.claim_audit.json").read_text(encoding="utf-8")
    )
    assert payload["skipped"] == "no evidence sidecar"
    assert payload["passed"] is None


def test_missing_report_is_bad_input(tmp_path):
    assert (
        main(["evolution_claim_audit.py", str(tmp_path / "absent.md")])
        == EXIT_BAD_INPUT
    )


def test_explicit_sources_path_is_honoured(tmp_path):
    report = tmp_path / "draft.md"
    report.write_text(f"- **Source**: {URL} reports 85.2%\n", encoding="utf-8")
    sources = tmp_path / "elsewhere.json"
    sources.write_text(json.dumps({URL: _SOURCE}), encoding="utf-8")
    assert (
        main(["evolution_claim_audit.py", str(report), "--sources", str(sources)])
        == EXIT_OK
    )
