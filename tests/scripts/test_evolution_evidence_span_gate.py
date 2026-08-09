"""Tests for scripts/evolution_evidence_span_gate.py."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import evolution_evidence_span_gate as gate  # noqa: E402

SAMPLE_MD = """\
# Evolution Research Digest

## Key Findings

### ACE achieves improvement
ACE matches top agents, with a "10.6% improvement over baseline" on AppWorld.
See https://arxiv.org/abs/2510.04618 for details.

### SWE-bench results
SWE-bench-Verified hit 75.6% with frozen harness. No URL here.

### Bare claim
Just a claim with no evidence.

## Source-by-Source Details
Other section.
"""


def test_find_latest_report(tmp_path):
    d = tmp_path / "research"
    d.mkdir()
    (d / "2026-01-01.md").write_text("# R")
    (d / "2026-03-15.md").write_text("# R")
    assert gate.find_latest_report(d).name == "2026-03-15.md"
    assert gate.find_latest_report(tmp_path / "nonexistent") is None


def test_extract_findings_count():
    assert len(gate.extract_findings(SAMPLE_MD)) == 3


def test_finding_with_quote_and_url():
    fs = gate.extract_findings(SAMPLE_MD)
    ace = [f for f in fs if "ACE" in f["header"]][0]
    assert ace["has_quote"] and ace["has_url"] and ace["compliant"]
    assert "10.6%" in ace["quote"]


def test_finding_url_no_quote():
    fs = gate.extract_findings(SAMPLE_MD)
    swe = [f for f in fs if "SWE-bench" in f["header"]][0]
    assert not swe["has_quote"] and not swe["compliant"]


def test_finding_bare_claim():
    fs = gate.extract_findings(SAMPLE_MD)
    bare = [f for f in fs if "Bare" in f["header"]][0]
    assert not bare["has_quote"] and not bare["has_url"] and not bare["compliant"]


def test_evaluate_compliance():
    res = gate.evaluate(gate.extract_findings(SAMPLE_MD))
    assert res["total_findings"] == 3 and res["compliant"] == 1


def test_write_sidecar(tmp_path):
    res = gate.evaluate(gate.extract_findings(SAMPLE_MD))
    out = gate.write_sidecar(res, tmp_path)
    assert json.loads(out.read_text())["total_findings"] == 3


def test_run_full(tmp_path):
    d = tmp_path / "research"
    d.mkdir()
    (d / "2026-05-01.md").write_text(SAMPLE_MD)
    old = gate.RESEARCH_DIR
    gate.RESEARCH_DIR = d
    try:
        res = gate.run()
    finally:
        gate.RESEARCH_DIR = old
    assert res["total_findings"] == 3 and (d / "evidence_spans.json").exists()


def test_run_no_report(tmp_path):
    old = gate.RESEARCH_DIR
    gate.RESEARCH_DIR = tmp_path
    try:
        assert "skipped" in gate.run()
    finally:
        gate.RESEARCH_DIR = old
